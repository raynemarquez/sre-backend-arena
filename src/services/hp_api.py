import asyncio
import time
import httpx
import structlog
from typing import Any
from pybreaker import CircuitBreaker, CircuitBreakerError, CircuitBreakerListener
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Circuit Breaker
# Abre após 5 falhas consecutivas; tenta reset após 10 s
# Ajustando para um cenário de alta concorrência
# ---------------------------------------------------------------------------


class LogListener(CircuitBreakerListener):
    def state_change(self, _cb, old_state, new_state):
        logger.warning(
            "circuit_breaker_state_changed",
            old_state=old_state.name,
            new_state=new_state.name,
        )


circuit_breaker = CircuitBreaker(
    fail_max=5,
    reset_timeout=10,
    listeners=[LogListener()],
)

# ---------------------------------------------------------------------------
# Configuração do Cache: Resiliência de dados e performance
# Cache async-safe com TTL
# ---------------------------------------------------------------------------
_CACHE_TTL = 300  # 5 minutos
_ALL_CHARACTERS_KEY = "__all_characters__"


class _AsyncTTLCache:
    """Cache in-memory thread-safe para uso com asyncio."""

    def __init__(self, maxsize: int, ttl: float) -> None:
        self._maxsize = maxsize
        self._ttl = ttl
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> tuple[Any, bool, bool]:
        """
        Retorna (valor, hit_valido, hit_stale).
        hit_valido: dado dentro do TTL.
        hit_stale: dado existe mas expirou (útil para fallback).
        """
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None, False, False

            value, expires_at = entry
            is_expired = time.monotonic() > expires_at

            if is_expired:
                return value, False, True

            return value, True, False

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            if len(self._store) >= self._maxsize and key not in self._store:
                oldest = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest]
            self._store[key] = (value, time.monotonic() + self._ttl)

    async def invalidate(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)


_cache = _AsyncTTLCache(maxsize=200, ttl=_CACHE_TTL)


# ---------------------------------------------------------------------------
# Rate Limiter client-side para a HP-API (token bucket)
# A HP-API não publica um rate limit oficial, mas é uma API pública/free tier.
# Limitamos a 5 req/s como margem de segurança — ajuste conforme necessário.
# ---------------------------------------------------------------------------
class _TokenBucketRateLimiter:
    """Token bucket assíncrono para controlar chamadas a APIs externas."""

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Bloqueia até haver um token disponível."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self._capacity,
                self._tokens + elapsed * self._rate,
            )
            self._last_refill = now

            if self._tokens >= 1:
                self._tokens -= 1
                wait_time = 0.0
            else:
                wait_time = (1 - self._tokens) / self._rate

        if wait_time > 0:
            # Logamos ANTES de dormir para indicar o throttling
            logger.info("rate_limit_throttling", wait_time=round(wait_time, 4))
            await asyncio.sleep(wait_time)

            # Após acordar, precisamos decrementar o token que "esperamos" para ganhar
            async with self._lock:
                self._tokens = max(0.0, self._tokens - 1)


# _rate_limiter = _TokenBucketRateLimiter(rate=5.0, capacity=10.0)

_rate_limiter = _TokenBucketRateLimiter(rate=10.0, capacity=20.0)

# ---------------------------------------------------------------------------
# Contadores internos do circuit breaker (sem tocar em API privada)
# Usamos um wrapper simples: o CB registra falhas via call() síncrono.
# ---------------------------------------------------------------------------
_cb_fail_count = 0
_cb_fail_lock = asyncio.Lock()


async def _cb_record_failure() -> None:
    """
    Registra uma falha no circuit breaker de forma segura para asyncio.

    pybreaker é síncrono e thread-safe mas não async-safe.
    Chamamos circuit_breaker.call() com uma função que lança exceção —
    isso aciona o mecanismo interno de contagem sem tocar em atributos privados.
    """

    def _fail():
        raise RuntimeError("recorded failure")

    try:
        circuit_breaker.call(_fail)
    except (RuntimeError, CircuitBreakerError):
        pass  # esperado


async def _cb_record_success() -> None:
    """Registra sucesso para resetar o contador de falhas."""

    def _ok():
        return True

    try:
        circuit_breaker.call(_ok)
    except CircuitBreakerError:
        pass  # CB aberto — ok, não há sucesso para registrar


# ---------------------------------------------------------------------------
# Cliente HP-API
# ---------------------------------------------------------------------------
class HPApiClient:
    BASE_URL = "https://hp-api.onrender.com/api"
    MAX_POWER_SCORE = 100

    # Timeout granular para diferenciar problemas de rede (connect) de lentidão da API (read)
    def __init__(self) -> None:
        # Connection pool persistente — reutiliza conexões TCP entre requests
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=5.0),
            limits=httpx.Limits(
                max_connections=100, max_keepalive_connections=50
            ),  # Aumentado para suportar carga
        )
        self._index: dict[str, dict[str, Any]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Busca externa (retry + circuit breaker)
    # ------------------------------------------------------------------
    @retry(
        stop=stop_after_attempt(3),  # Retry up to 3 times
        wait=wait_exponential(
            multiplier=1, min=2, max=10
        ),  # Exponential backoff: 2s, 4s, 8s
        retry=retry_if_exception_type(
            httpx.RequestError
        ),  # Retry apenas em erros de rede/timeout — não em 4xx/5xx
        reraise=True,
    )
    async def _fetch_all_characters(self) -> list[dict[str, Any]]:
        """Chama a HP-API respeitando rate limiter. Retry automático em falhas de rede."""
        await _rate_limiter.acquire()

        logger.info("hp_api_fetch_start", url=f"{self.BASE_URL}/characters")
        try:
            response = await self._client.get(f"{self.BASE_URL}/characters")
            response.raise_for_status()
            data = response.json()

            logger.info("hp_api_fetch_success", count=len(data))
            return data

        except httpx.RequestError as exc:
            logger.error("hp_api_request_error", error=str(exc))
            raise
        except httpx.HTTPStatusError as exc:
            logger.error(
                "hp_api_http_error",
                status_code=exc.response.status_code,
                error=str(exc),
            )
            raise

    async def _fetch_with_circuit_breaker(self) -> list[dict[str, Any]]:
        """
        Envolve _fetch_all_characters com o circuit breaker.

        Usa apenas a API pública do pybreaker:
          - circuit_breaker.current_state para verificar se está aberto
          - circuit_breaker.call() para registrar sucesso/falha

        Não depende de _state_storage, _inc_counter ou outros atributos
        privados — garantindo compatibilidade com versões futuras.
        """
        if circuit_breaker.current_state == "open":
            raise CircuitBreakerError("Circuit breaker is open")

        try:
            result = await self._fetch_all_characters()
            await _cb_record_success()
            return result
        except CircuitBreakerError:
            raise
        except Exception:
            await _cb_record_failure()
            raise

    # ------------------------------------------------------------------
    # Warmup de cache na inicialização
    # ------------------------------------------------------------------
    async def warmup_cache(self) -> None:
        logger.info("cache_warmup_start")

        try:
            # O problema está aqui: se a API falhar, o código abaixo não executa
            data = await self._fetch_with_circuit_breaker()

            await _cache.set(_ALL_CHARACTERS_KEY, data)

            self._index = {}

            for c in data:
                name = c.get("name", "").lower()
                if not name:
                    continue

                enriched = dict(c)
                enriched["powerScore"] = self._calculate_power_score(enriched)
                enriched["loyalty"] = self._determine_loyalty(enriched)

                self._index[name] = c
                await _cache.set(name, enriched)

            logger.info("cache_warmup_done", size=len(self._index))

        except Exception as e:
            # Se der erro, logamos e deixamos o Python seguir adiante
            # O @property is_cache_ready retornará False, o que é o comportamento correto
            logger.error("cache_warmup_failed", error=str(e))
            logger.info("resuming_startup_without_cache")

    @property
    def is_cache_ready(self) -> bool:
        # Se o try/except acima capturou um erro, o self._index estará vazio
        # e o seu Readiness Probe saberá que ainda não pode receber tráfego,
        # mas o pod FICARÁ VIVO (Running).
        return bool(getattr(self, "_index", {}))

    # ------------------------------------------------------------------
    # Interface pública
    # ------------------------------------------------------------------
    async def get_character_data(self, name: str) -> tuple[dict[str, Any], bool]:
        """
        Retorna (character_dict, from_cache).

        Estratégia de cache em dois níveis:
        1. L1: Cache por nome de personagem (hit mais comum)
        2. L2: Cache da lista completa (evita múltiplas chamadas à API)

        Apenas 1 chamada HTTP acontece por TTL (~5 min), independente do
        número de nomes diferentes recebidos.
        """
        cache_key = name.lower().strip()

        # Nível 1: personagem já cacheado individualmente
        cached_value, is_valid, is_stale = await _cache.get(cache_key)
        if is_valid:
            logger.debug("cache_hit_valid", wizard=name)
            return cached_value, True

        logger.info("cache_miss", wizard=name)

        # Nível 2: lista completa já cacheada
        all_chars, all_valid, all_stale = await _cache.get(_ALL_CHARACTERS_KEY)

        if not all_valid:
            try:
                all_chars = await self._fetch_with_circuit_breaker()
                await _cache.set(_ALL_CHARACTERS_KEY, all_chars)
            except (CircuitBreakerError, Exception) as exc:
                # Fallback: dado stale do personagem
                if is_stale:
                    logger.warning(
                        "fallback_to_stale_cache", wizard=name, reason=str(exc)
                    )
                    return cached_value, True
                # Fallback: lista stale
                if all_stale and all_chars:
                    logger.warning(
                        "fallback_to_stale_list", wizard=name, reason=str(exc)
                    )
                else:
                    logger.error("api_failure_no_fallback", wizard=name, error=str(exc))
                    raise

        if not isinstance(all_chars, list):
            return {}, False

        # Filtra pelo nome (operação in-memory, O(n) mas sem I/O)
        found = next(
            (c for c in all_chars if c.get("name", "").lower() == cache_key),
            None,
        )

        if not found:
            await _cache.set(cache_key, {})
            return {}, False

        found = dict(found)
        found["powerScore"] = self._calculate_power_score(found)
        found["loyalty"] = self._determine_loyalty(found)

        await _cache.set(cache_key, found)
        return found, False

    # ------------------------------------------------------------------
    # Lógica de negócio
    # ------------------------------------------------------------------
    def _calculate_power_score(self, character: dict[str, Any]) -> int:
        """
        Calcula powerScore baseado nos atributos reais da HP-API.
        Máximo: 100 pontos.
        """
        score = 50
        if character.get("wizard"):
            score += 20
        if character.get("house"):
            score += 15
        wand = character.get("wand", {})
        if wand.get("wood") or wand.get("core"):
            score += 15
        return min(score, self.MAX_POWER_SCORE)

    def _determine_loyalty(self, character: dict[str, Any]) -> str:
        """Determina lealdade baseada em espécie e casa — dados reais da HP-API."""
        species = character.get("species", "").lower()

        loyalty_by_species = {
            "house-elf": "unconditional",  # Lealdade mágica/servidão (Dobby, Kreacher)
            "hippogriff": "respect-based",  # Exige respeito mútuo (Bicuço)
            "werewolf": "volatile",  # Depende da transformação/indivíduo (Lupin)
            "centaur": "species-loyal",  # Leais aos seus próprios costumes (Firenze)
            "goblin": "transactional",  # Lealdade baseada em acordos/ouro (Griphook)
            "ghost": "neutral",  # Observadores do tempo (Nick Quase Sem Cabeça)
            "half-giant": "reliable",  # Geralmente protetores se bem tratados
            "giant": "reliable",  # Geralmente protetores se bem tratados
        }
        if species in loyalty_by_species:
            return loyalty_by_species[species]

        if species == "human":
            # Refinamento para humanos baseado na Casa
            house = character.get("house", "").lower()
            if house == "hufflepuff":
                return "very_high"
            if house == "gryffindor":
                return "high"
            if house == "slytherin":
                return "self_serving"
            return "variable"  # Outras casas ou sem casa

        return "instinctive"  # Para corujas, gatos, acromântulas, etc.
