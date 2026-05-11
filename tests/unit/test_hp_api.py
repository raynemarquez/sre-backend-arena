import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from src.services.hp_api import HPApiClient, _AsyncTTLCache, _TokenBucketRateLimiter
from pybreaker import CircuitBreakerError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def hp_api_client( ):
    return HPApiClient()

# ---------------------------------------------------------------------------
# Testes de Lógica de Negócio (Enrichment)
# ---------------------------------------------------------------------------
# _calculate_power_score - Lógica real: base=50, +20 se wizard, +15 se house preenchida, +15 se wand preenchida, max 100
# ---------------------------------------------------------------------------
class TestCalculatePowerScore:
    def test_full_score_wizard_with_house_and_wand(self, hp_api_client):
        char = {"wizard": True, "house": "Gryffindor", "wand": {"wood": "Holly", "core": "Phoenix"}}
        assert hp_api_client._calculate_power_score(char) == 100
 
    def test_wizard_with_house_no_wand(self, hp_api_client):
        # 50 + 20 + 15 = 85 (wand vazio não pontua)
        char = {"wizard": True, "house": "Gryffindor", "wand": {"wood": "", "core": ""}}
        assert hp_api_client._calculate_power_score(char) == 85
 
    def test_wizard_no_house_no_wand(self, hp_api_client):
        # 50 + 20 = 70
        char = {"wizard": True, "house": "", "wand": {}}
        assert hp_api_client._calculate_power_score(char) == 70
 
    def test_non_wizard_no_house_no_wand(self, hp_api_client):
        # base = 50
        char = {"wizard": False, "house": "", "wand": {}}
        assert hp_api_client._calculate_power_score(char) == 50
 
    def test_non_wizard_with_house_and_wand(self, hp_api_client):
        # 50 + 15 + 15 = 80
        char = {"wizard": False, "house": "Gryffindor", "wand": {"wood": "Oak", "core": "Dragon"}}
        assert hp_api_client._calculate_power_score(char) == 80

    def test_score_capped_at_100(self, hp_api_client):
        char = {"wizard": True, "house": "Slytherin", "wand": {"wood": "Yew", "core": "Phoenix"}}
        assert hp_api_client._calculate_power_score(char) <= 100

# ---------------------------------------------------------------------------
# _determine_loyalty
# ---------------------------------------------------------------------------
class TestDetermineLoyalty:
    def test_gryffindor_human(self, hp_api_client):
        assert hp_api_client._determine_loyalty({"species": "human", "house": "Gryffindor"}) == "high"

    def test_hufflepuff_human(self, hp_api_client):
        assert hp_api_client._determine_loyalty({"species": "human", "house": "Hufflepuff"}) == "very_high"

    def test_slytherin_human(self, hp_api_client):
        assert hp_api_client._determine_loyalty({"species": "human", "house": "Slytherin"}) == "self_serving"

    def test_human_no_house(self, hp_api_client):
        assert hp_api_client._determine_loyalty({"species": "human", "house": ""}) == "variable"

    def test_house_elf(self, hp_api_client):
        assert hp_api_client._determine_loyalty({"species": "house-elf", "house": ""}) == "unconditional"

    def test_goblin(self, hp_api_client):
        assert hp_api_client._determine_loyalty({"species": "goblin", "house": ""}) == "transactional"

    def test_hippogriff(self, hp_api_client):
        assert hp_api_client._determine_loyalty({"species": "hippogriff", "house": ""}) == "respect-based"

    def test_werewolf(self, hp_api_client):
        assert hp_api_client._determine_loyalty({"species": "werewolf", "house": ""}) == "volatile"

    def test_centaur(self, hp_api_client):
        assert hp_api_client._determine_loyalty({"species": "centaur", "house": ""}) == "species-loyal"

    def test_ghost(self, hp_api_client):
        assert hp_api_client._determine_loyalty({"species": "ghost", "house": ""}) == "neutral"

    def test_half_giant(self, hp_api_client):
        assert hp_api_client._determine_loyalty({"species": "half-giant", "house": ""}) == "reliable"

    def test_unknown_species(self, hp_api_client):
        assert hp_api_client._determine_loyalty({"species": "dragon", "house": ""}) == "instinctive"


# ---------------------------------------------------------------------------
# Testes de Cache - _AsyncTTLCache  (retorna tupla de 3: value, is_valid, is_stale)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_async_ttl_cache_set_and_get():
    cache = _AsyncTTLCache(maxsize=10, ttl=60)
    await cache.set("harry", {"name": "Harry Potter"})
    value, is_valid, is_stale = await cache.get("harry")
    assert is_valid is True
    assert is_stale is False
    assert value["name"] == "Harry Potter"
 
 
@pytest.mark.asyncio
async def test_async_ttl_cache_miss():
    cache = _AsyncTTLCache(maxsize=10, ttl=60)
    value, is_valid, is_stale = await cache.get("nonexistent")
    assert value is None
    assert is_valid is False
    assert is_stale is False
 
 
@pytest.mark.asyncio
async def test_async_ttl_cache_expired_returns_stale():
    cache = _AsyncTTLCache(maxsize=10, ttl=-1)  # TTL negativo → já expirado
    await cache.set("draco", {"name": "Draco Malfoy"})
    value, is_valid, is_stale = await cache.get("draco")
    assert value["name"] == "Draco Malfoy"
    assert is_valid is False
    assert is_stale is True  # dado existe mas expirou
 
 
@pytest.mark.asyncio
async def test_async_ttl_cache_eviction():
    cache = _AsyncTTLCache(maxsize=1, ttl=60)
    await cache.set("k1", "v1")
    await cache.set("k2", "v2")  # k1 deve ser evictado
    _, hit_k1, _ = await cache.get("k1")
    _, hit_k2, _ = await cache.get("k2")
    assert hit_k1 is False
    assert hit_k2 is True


# ---------------------------------------------------------------------------
# _TokenBucketRateLimiter
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rate_limiter_acquires_without_wait():
    limiter = _TokenBucketRateLimiter(rate=10.0, capacity=10.0)
    await limiter.acquire()
    assert limiter._tokens == 9.0
 
 
@pytest.mark.asyncio
async def test_rate_limiter_waits_when_empty():
    limiter = _TokenBucketRateLimiter(rate=100.0, capacity=1.0)
    with patch("src.services.hp_api.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await limiter.acquire()  # consome o único token — bucket vai a 0
        await limiter.acquire()  # bucket vazio → deve chamar sleep
        mock_sleep.assert_awaited_once()


# ---------------------------------------------------------------------------
# _fetch_all_characters
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_all_characters_success(hp_api_client):
    mock_data = [{"name": "Harry Potter", "house": "Gryffindor"}]
    with (
        patch("src.services.hp_api._rate_limiter.acquire", new_callable=AsyncMock),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
    ):
        mock_get.return_value.status_code = 200
        mock_get.return_value.raise_for_status = MagicMock()
        mock_get.return_value.json = MagicMock(return_value=mock_data)
        result = await hp_api_client._fetch_all_characters()
        assert result == mock_data
 
 
@pytest.mark.asyncio
async def test_fetch_all_characters_request_error(hp_api_client):
    with (
        patch("src.services.hp_api._rate_limiter.acquire", new_callable=AsyncMock),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
    ):
        mock_get.side_effect = httpx.RequestError("timeout")
        with pytest.raises(httpx.RequestError):
            await hp_api_client._fetch_all_characters()
 
 
@pytest.mark.asyncio
async def test_fetch_all_characters_http_status_error(hp_api_client):
    with (
        patch("src.services.hp_api._rate_limiter.acquire", new_callable=AsyncMock),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
    ):
        mock_get.return_value.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "503", request=httpx.Request("GET", "http://test"), response=httpx.Response(503)
            )
        )
        with pytest.raises(httpx.HTTPStatusError):
            await hp_api_client._fetch_all_characters()


# ---------------------------------------------------------------------------
# _fetch_with_circuit_breaker
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_with_circuit_breaker_success(hp_api_client):
    """Circuit breaker fechado: delega para _fetch_all_characters e retorna dados."""
    mock_data = [{"name": "Dumbledore"}]
    with patch.object(
        hp_api_client, "_fetch_all_characters", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = mock_data
        result = await hp_api_client._fetch_with_circuit_breaker()
        assert result == mock_data
        mock_fetch.assert_awaited_once()

@pytest.mark.asyncio
async def test_fetch_with_circuit_breaker_open_raises(hp_api_client):
    """Circuit breaker aberto: deve levantar CircuitBreakerError sem chamar a API."""
    with patch("src.services.hp_api.circuit_breaker") as mock_cb:
        mock_cb.current_state = "open"
        with pytest.raises(CircuitBreakerError):
            await hp_api_client._fetch_with_circuit_breaker()

# ---------------------------------------------------------------------------
# get_character_data
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_character_data_cache_hit(hp_api_client):
    cached = {"name": "Harry Potter", "house": "Gryffindor", "species": "human", "wizard": True, "powerScore": 100, "loyalty": "high"}
    with patch("src.services.hp_api._cache") as mock_cache:
        mock_cache.get = AsyncMock(return_value=(cached, True, False))
        result, from_cache = await hp_api_client.get_character_data("Harry Potter")
        assert from_cache is True
        assert result["name"] == "Harry Potter"

@pytest.mark.asyncio
async def test_get_character_data_cache_miss_api_success(hp_api_client):
    mock_api = [{"name": "Harry Potter", "house": "Gryffindor", "species": "human",
                 "wizard": True, "wand": {"wood": "holly", "core": "phoenix feather"}}]
    with (
        patch("src.services.hp_api._cache") as mock_cache,
        patch.object(hp_api_client, "_fetch_with_circuit_breaker", new_callable=AsyncMock) as mock_fetch,
    ):
        mock_cache.get = AsyncMock(return_value=(None, False, False))
        mock_cache.set = AsyncMock()
        mock_fetch.return_value = mock_api
        result, from_cache = await hp_api_client.get_character_data("Harry Potter")
        assert from_cache is False
        assert "powerScore" in result
        assert "loyalty" in result
 
 
@pytest.mark.asyncio
async def test_get_character_data_not_found(hp_api_client):
    with (
        patch("src.services.hp_api._cache") as mock_cache,
        patch.object(hp_api_client, "_fetch_with_circuit_breaker", new_callable=AsyncMock) as mock_fetch,
    ):
        mock_cache.get = AsyncMock(return_value=(None, False, False))
        mock_cache.set = AsyncMock()
        mock_fetch.return_value = [{"name": "Hermione Granger"}]
        result, from_cache = await hp_api_client.get_character_data("Ron Weasley")
        assert result == {}
 
 
@pytest.mark.asyncio
async def test_get_character_data_invalid_api_response(hp_api_client):
    """
    Testa o cenário onde o cache está vazio e a API externa retorna um dado 
    que causa erro no processamento (ex: não é uma lista).
    """
    # 1. Mockamos o cache para simular Miss total (L1 e L2 vazios/inválidos)
    # Retorno do cache.get: (value, is_valid, is_stale)
    with patch("src.services.hp_api._cache") as mock_cache:
        mock_cache.get = AsyncMock(return_value=(None, False, False))
        mock_cache.set = AsyncMock()
        
        # 2. Mockamos a chamada da API para retornar algo que quebraria o loop (ex: None ou String)
        # O HPApiClient agora usa o _l2_refresh_lock, então o mock_fetch deve ser AsyncMock
        with patch.object(
            hp_api_client, 
            "_fetch_with_circuit_breaker", 
            new_callable=AsyncMock
        ) as mock_fetch:
            
            # Simulando que a API retornou algo inválido (não iterável como lista de dicts)
            mock_fetch.return_value = None 
            
            # O código deve lançar uma exceção (TypeError ou AttributeError) ao tentar 
            # iterar no 'None', ou você pode capturar o erro esperado.
            # Se o seu objetivo é que ele retorne {} em caso de erro de parsing, 
            # o código precisaria de um try/except interno no refresh_index.
            
            with pytest.raises(Exception): # Esperamos que quebre pois o dado é inválido
                await hp_api_client.get_character_data("Harry Potter")

@pytest.mark.asyncio
async def test_get_character_data_api_failure_with_stale_fallback(hp_api_client):
    """
    Testa o SRE Achievement: Chaos Survivor.
    Valida se, com a API fora, o sistema serve o dado STALE (expirado) do cache.
    """
    stale_data = {"name": "Harry Potter", "powerScore": 85}
    
    # Precisamos patchear o cache que é importado dentro do módulo hp_api
    with patch("src.services.hp_api._cache") as mock_cache:
        # Definimos o comportamento para as múltiplas chamadas de .get()
        # 1ª chamada: get("harry potter") -> retorna dado expirado (is_valid=False, is_stale=True)
        # 2ª chamada: get("__all_characters__") -> retorna vazio (simulando que o L2 também expirou)
        # 3ª chamada (dentro do lock): get("__all_characters__") -> re-checa se alguém atualizou
        mock_cache.get = AsyncMock(side_effect=[
            (stale_data, False, True),  # L1 Stale
            (None, False, False),       # L2 Miss
            (None, False, False),       # L2 Re-check após Lock
        ])
        mock_cache.set = AsyncMock()

        # Mock da API externa para simular falha (Circuit Breaker ou Network Error)
        with patch.object(
            hp_api_client, 
            "_fetch_with_circuit_breaker", 
            new_callable=AsyncMock,
            side_effect=Exception("API Down")
        ):
            # Execução
            result, is_hit = await hp_api_client.get_character_data("Harry Potter")

            # Asserções
            assert result["name"] == "Harry Potter"
            assert result["powerScore"] == 85
            assert is_hit is True  # Consideramos hit pois serviu do cache (mesmo stale)
            
            # Verifica se ele tentou buscar na API antes de decidir pelo fallback
            assert hp_api_client._fetch_with_circuit_breaker.called
 
 
@pytest.mark.asyncio
async def test_get_character_data_request_error(hp_api_client):
    with (
        patch("src.services.hp_api._cache") as mock_cache,
        patch.object(hp_api_client, "_fetch_with_circuit_breaker", new_callable=AsyncMock) as mock_fetch,
    ):
        mock_cache.get = AsyncMock(return_value=(None, False, False))
        mock_fetch.side_effect = httpx.RequestError("connection refused")
        with pytest.raises(httpx.RequestError):
            await hp_api_client.get_character_data("Harry Potter")
 
 
@pytest.mark.asyncio
async def test_get_character_data_circuit_breaker_open(hp_api_client):
    with (
        patch("src.services.hp_api._cache") as mock_cache,
        patch.object(hp_api_client, "_fetch_with_circuit_breaker", new_callable=AsyncMock) as mock_fetch,
    ):
        mock_cache.get = AsyncMock(return_value=(None, False, False))
        mock_fetch.side_effect = CircuitBreakerError("open")
        with pytest.raises(CircuitBreakerError):
            await hp_api_client.get_character_data("Harry Potter")
 
 
@pytest.mark.asyncio
async def test_get_character_data_fallback_stale(hp_api_client):
    """Quando API falha mas há dados stale, deve retornar o fallback."""
    stale_data = {"name": "Harry Potter", "powerScore": 90}
    with (
        patch("src.services.hp_api._cache") as mock_cache,
        patch.object(hp_api_client, "_fetch_with_circuit_breaker", new_callable=AsyncMock) as mock_fetch,
    ):
        mock_cache.get = AsyncMock(return_value=(stale_data, False, True))  # stale!
        mock_fetch.side_effect = Exception("API Down")
        result, from_cache = await hp_api_client.get_character_data("Harry Potter")
        assert from_cache is True
        assert result["name"] == "Harry Potter"