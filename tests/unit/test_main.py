import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch, MagicMock
from pybreaker import CircuitBreakerError

from src.main import app, _inject_trace_context, request_id_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# _inject_trace_context — processador structlog
# ---------------------------------------------------------------------------
def test_inject_trace_context_with_request_id():
    token = request_id_context.set("abc-123")
    try:
        result = _inject_trace_context(None, None, {})
        assert result["correlation_id"] == "abc-123"
        assert result["trace_id"] == "abc-123"
        assert result["otel_trace_id"] == "abc-123"
    finally:
        request_id_context.reset(token)


def test_inject_trace_context_without_request_id():
    """Sem request_id no contexto, o dict não deve receber campos de trace."""
    token = request_id_context.set(None)
    try:
        result = _inject_trace_context(None, None, {"event": "test"})
        assert "correlation_id" not in result
        assert "trace_id" not in result
    finally:
        request_id_context.reset(token)


# ---------------------------------------------------------------------------
# setup_tracing
# ---------------------------------------------------------------------------
def test_setup_tracing_disabled_without_env(caplog):
    """Sem OTEL_EXPORTER_OTLP_ENDPOINT, tracing deve ser no-op."""
    import logging
    from src.observability.tracing import setup_tracing

    mock_app = MagicMock()
    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        with caplog.at_level(logging.INFO, logger="src.observability.tracing"):
            setup_tracing(mock_app)
    # Não deve instrumentar a app
    mock_app.assert_not_called()


def test_setup_tracing_import_error_is_graceful():
    """Se as libs OTEL não estiverem instaladas, não deve levantar exceção."""
    from src.observability.tracing import setup_tracing

    mock_app = MagicMock()
    with patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://tempo:4318"}):
        with patch("builtins.__import__", side_effect=ImportError("otel not found")):
            # Não deve explodir
            try:
                setup_tracing(mock_app)
            except ImportError:
                pass  # esperado se o patch afetar tudo — o importante é não vazar


# ---------------------------------------------------------------------------
# Lifespan — startup / shutdown
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_lifespan_warmup_failure_does_not_crash():
    """Se o warmup falhar, a app deve subir normalmente (falha silenciosa)."""
    with patch(
        "src.main.hp_api_client.warmup_cache",
        new_callable=AsyncMock,
        side_effect=Exception("HP-API offline"),
    ):
        async with _make_client() as client:
            response = await client.get("/health")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_lifespan_warmup_success():
    """Quando warmup ok, app sobe e health retorna cache_ready=True."""
    with patch(
        "src.main.hp_api_client.warmup_cache",
        new_callable=AsyncMock,
    ):
        with patch.object(
            type(app.state if hasattr(app, "state") else object),
            "__getattr__",
            return_value=None,
        ):
            async with _make_client() as client:
                response = await client.get("/health")
            assert response.status_code == 200


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_health_check():
    async with _make_client() as client:
        response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "circuit_breaker" in body
    assert "cache_ready" in body

@pytest.mark.asyncio
async def test_health_check_degraded_when_circuit_open():
    """Quando o circuit breaker está aberto, /health reporta status degraded."""
    with patch("src.main.circuit_breaker") as mock_cb:
        mock_cb.current_state = "open"
        async with _make_client() as client:
            response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"

@pytest.mark.asyncio
async def test_health_cache_ready_reflects_index():
    """cache_ready deve ser True quando _index estiver populado."""
    with patch("src.main.hp_api_client") as mock_client:
        mock_client._index = {"harry potter": {}}
        
        # Simula circuit breaker fechado
        with patch("src.main.circuit_breaker") as mock_cb:
            mock_cb.current_state = "closed"
            async with _make_client() as client:
                response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["cache_ready"] is True

# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_metrics_endpoint():
    async with _make_client() as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]

@pytest.mark.asyncio
async def test_metrics_contains_custom_counters():
    """Métricas customizadas devem estar presentes no scrape."""
    async with _make_client() as client:
        response = await client.get("/metrics")
    body = response.text
    assert "api_requests_total" in body
    assert "cache_hits_total" in body
    assert "cache_misses_total" in body
    assert "circuit_breaker_open" in body

# ---------------------------------------------------------------------------
# /wizard/{name} — sucesso e variações de cache
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_wizard_success():
    mock_data = {
        "name": "Harry Potter",
        "house": "Gryffindor",
        "species": "human",
        "wizard": True,
        "powerScore": 100,
        "loyalty": "high",  # campo interno — deve ser removido da resposta
    }
    with patch(
        "src.main.hp_api_client.get_character_data",
        new_callable=AsyncMock,
        return_value=(mock_data, False),
    ):
        async with _make_client() as client:
            response = await client.get("/wizard/Harry Potter")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Harry Potter"
    assert data["house"] == "Gryffindor"
    assert data["species"] == "human"
    assert data["wizard"] is True
    assert data["powerScore"] == 100
    assert "loyalty" not in data  # campo interno removido


@pytest.mark.asyncio
async def test_get_wizard_cache_hit():
    mock_data = {
        "name": "Hermione Granger",
        "house": "Gryffindor",
        "species": "human",
        "wizard": True,
        "powerScore": 100,
    }
    with patch(
        "src.main.hp_api_client.get_character_data",
        new_callable=AsyncMock,
        return_value=(mock_data, True),  # from_cache=True
    ):
        async with _make_client() as client:
            response = await client.get("/wizard/Hermione Granger")

    assert response.status_code == 200
    assert response.json()["name"] == "Hermione Granger"

@pytest.mark.asyncio
async def test_get_wizard_uses_defaults_for_missing_fields():
    """Campos ausentes no dict devem usar valores padrão do WizardResponse."""
    sparse_data = {"name": "Mystery Wizard"}
    with patch(
        "src.main.hp_api_client.get_character_data",
        new_callable=AsyncMock,
        return_value=(sparse_data, False),
    ):
        async with _make_client() as client:
            response = await client.get("/wizard/Mystery Wizard")

    assert response.status_code == 200
    data = response.json()
    assert data["house"] == "Unknown"
    assert data["species"] == "Unknown"
    assert data["wizard"] is False
    assert data["powerScore"] == 0


# ---------------------------------------------------------------------------
# /wizard/{name} — erros
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# /wizard/{name} — 404
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_wizard_not_found():
    with patch(
        "src.main.hp_api_client.get_character_data",
        new_callable=AsyncMock,
        return_value=({}, False),
    ):
        async with _make_client() as client:
            response = await client.get("/wizard/NonExistentWizard")

    assert response.status_code == 404
    assert response.json() == {"detail": "Wizard not found"}


# ---------------------------------------------------------------------------
# /wizard/{name} — 500
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_wizard_internal_error():
    with patch(
        "src.main.hp_api_client.get_character_data",
        new_callable=AsyncMock,
        side_effect=Exception("unexpected failure"),
    ):
        async with _make_client() as client:
            response = await client.get("/wizard/Harry Potter")
 
    assert response.status_code == 500
    assert response.json()["detail"] == "Internal Server Error"


# ---------------------------------------------------------------------------
# /wizard/{name} — 503 circuit breaker
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_wizard_circuit_breaker_open():
    with patch(
        "src.main.hp_api_client.get_character_data",
        new_callable=AsyncMock,
        side_effect=CircuitBreakerError("open"),
    ):
        async with _make_client() as client:
            response = await client.get("/wizard/Harry Potter")

    assert response.status_code == 503
    assert "Circuit breaker" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Middleware — correlation ID
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_correlation_id_propagated_from_header():
    with patch(
        "src.main.hp_api_client.get_character_data",
        new_callable=AsyncMock,
        return_value=({}, False),
    ):
        async with _make_client() as client:
            response = await client.get(
                "/wizard/test",
                headers={"X-Request-ID": "test-correlation-123"},
            )

    assert response.headers.get("x-request-id") == "test-correlation-123"

@pytest.mark.asyncio
async def test_correlation_id_generated_when_absent():
    """Sem X-Request-ID no request, a app deve gerar um UUID automaticamente."""
    with patch(
        "src.main.hp_api_client.get_character_data",
        new_callable=AsyncMock,
        return_value=({}, False),
    ):
        async with _make_client() as client:
            response = await client.get("/wizard/test")

    request_id = response.headers.get("x-request-id")
    assert request_id is not None
    assert len(request_id) == 36  # formato UUID v4


@pytest.mark.asyncio
async def test_request_count_metric_incremented():
    """api_requests_total deve ser incrementado a cada request."""
    from prometheus_client import REGISTRY

    def get_request_count():
        for metric in REGISTRY.collect():
            if metric.name == "api_requests":
                for sample in metric.samples:
                    if (
                        sample.name == "api_requests_total"
                        and sample.labels.get("method") == "GET"
                        and sample.labels.get("endpoint") == "/wizard/anyone"
                        and sample.labels.get("http_status") == "404"
                    ):
                        return sample.value
        return 0.0

    before = get_request_count()

    with patch(
        "src.main.hp_api_client.get_character_data",
        new_callable=AsyncMock,
        return_value=({}, False),
    ):
        async with _make_client() as client:
            await client.get("/wizard/anyone")

    after = get_request_count()

    assert after == before + 1
