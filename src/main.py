import time
import uuid
import contextvars
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException, status, Response, Request
from src.models.wizard import WizardResponse
from src.services.hp_api import HPApiClient, circuit_breaker
from src.observability.tracing import setup_tracing
from pybreaker import CircuitBreakerError

from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

# ---------------------------------------------------------------------------
# Correlation ID + Trace ID — variável de contexto por requisição
# ---------------------------------------------------------------------------
request_id_context: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id_context", default=None
)


def _inject_trace_context(logger, method, event_dict):
    """
    Injeta correlation_id e trace_id no log para rastreabilidade.

    trace_id e otel_trace_id são o mesmo valor aqui (o X-Request-ID).
    Quando o backend OTLP (Tempo) for habilitado em cloud, o otel_trace_id
    será sobrescrito pelo trace real do OpenTelemetry via:
      opentelemetry-instrumentation-fastapi + BatchSpanProcessor
    A instrumentação está pronta em src/tracing.py — só precisa de:
      OTEL_EXPORTER_OTLP_ENDPOINT=http://<tempo>:4318
    """
    request_id = request_id_context.get()
    if request_id:
        event_dict["correlation_id"] = request_id
        event_dict["trace_id"] = request_id  # requisito do challenge
        event_dict["otel_trace_id"] = (
            request_id  # campo para correlação futura com Tempo
        )
    return event_dict


# ---------------------------------------------------------------------------
# Structlog — JSON estruturado
# ---------------------------------------------------------------------------
structlog.configure(
    processors=[
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _inject_trace_context,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Métricas Prometheus
# ---------------------------------------------------------------------------
REQUEST_COUNT = Counter(
    "api_requests_total",
    "Total de requisições recebidas",
    ["method", "endpoint", "http_status"],
)
ERROR_COUNT = Counter(
    "api_errors_total",
    "Total de erros por tipo",
    ["type"],
)
CACHE_HITS = Counter(
    "cache_hits_total",
    "Cache hits no lookup de personagens",
)
CACHE_MISSES = Counter(
    "cache_misses_total",
    "Cache misses no lookup de personagens",
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Latência das requisições HTTP em segundos",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0),
)
CIRCUIT_BREAKER_STATE = Gauge(
    "circuit_breaker_open",
    "1 quando o circuit breaker está aberto, 0 quando fechado",
)


# Cliente HP-API (singleton)
hp_api_client = HPApiClient()


# ---------------------------------------------------------------------------
# Lifespan — garante fechamento correto do HTTP client (connection pool)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("startup", message="Starting Wizard Intelligence Network")

    # Warmup
    try:
        await hp_api_client.warmup_cache()
        logger.info("cache_ready", ready=True)
    except Exception as e:
        logger.error("cache_warmup_failed", error=str(e))

    yield

    logger.info("shutdown", message="Closing HTTP client")
    await hp_api_client.aclose()


# ---------------------------------------------------------------------------
# Aplicação FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Wizard Intelligence Network",
    version="1.0.0",
    lifespan=lifespan,
    # Desabilita docs em produção para reduzir overhead
    docs_url=None,
    redoc_url=None,
)

# Tracing OpenTelemetry — no-op se OTEL_EXPORTER_OTLP_ENDPOINT não estiver definido
setup_tracing(app)


# ---------------------------------------------------------------------------
# Middleware: correlation ID + logging + métricas de duração
# ---------------------------------------------------------------------------
@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    # Propaga X-Request-ID do caller ou gera um novo
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request_id_context.set(request_id)

    start = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - start

    # Registrar Métricas
    path = request.url.path
    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=path,
        http_status=response.status_code,
    ).inc()
    REQUEST_LATENCY.labels(method=request.method, endpoint=path).observe(duration)

    # Atualiza o gauge do circuit breaker a cada request (custo zero)
    CIRCUIT_BREAKER_STATE.set(1 if circuit_breaker.current_state == "open" else 0)

    response.headers["X-Request-ID"] = request_id
    return response


# ---------------------------------------------------------------------------
# Endpoints de infraestrutura
# ---------------------------------------------------------------------------


@app.get("/health", status_code=status.HTTP_200_OK, tags=["infra"])
async def health():
    """Liveness/Readiness probe — inclui estado do circuit breaker."""
    return {
        "status": "ok" if circuit_breaker.current_state != "open" else "degraded",
        "circuit_breaker": circuit_breaker.current_state,
        "cache_ready": bool(hp_api_client._index),
    }


@app.get("/metrics", tags=["infra"])
async def metrics():
    """Scrape endpoint para VictoriaMetrics."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


# ---------------------------------------------------------------------------
# Endpoint de negócio
# ---------------------------------------------------------------------------
@app.get("/wizard/{name}", response_model=WizardResponse, tags=["wizard"])
async def get_wizard(name: str):
    """
    Retorna inteligência sobre um bruxo.
    Cache L1 (por nome) + L2 (lista completa) garantem resposta em memória
    para 99%+ das requisições após o primeiro warm-up.
    """
    try:
        # Busca os dados (hp_api_client já faz o enriquecimento de powerScore e loyalty)
        data, from_cache = await hp_api_client.get_character_data(name)

        # 1. Registrar métricas de Cache
        if from_cache:
            CACHE_HITS.inc()
        else:
            CACHE_MISSES.inc()

        # 2. Validar se o personagem foi encontrado
        if not data:
            ERROR_COUNT.labels(type="not_found").inc()
            raise HTTPException(status_code=404, detail="Wizard not found")

        # 3. Limpeza de campos internos (Loyalty é usado apenas para lógica, não para resposta)
        # Criamos uma cópia para não afetar o dado no cache se necessário
        data = dict(data)
        data.pop("loyalty", None)

        # 4. Retornar usando o modelo WizardResponse (garante a serialização correta)
        return WizardResponse(
            name=data.get("name", name),
            house=data.get("house", "Unknown"),
            species=data.get("species", "Unknown"),
            wizard=data.get("wizard", False),
            powerScore=data.get("powerScore", 0),
        )

    except CircuitBreakerError:
        ERROR_COUNT.labels(type="circuit_breaker").inc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Circuit breaker is open. External API is currently unavailable.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        ERROR_COUNT.labels(type="internal_error").inc()
        logger.error("unexpected_error", wizard=name, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal Server Error",
        )
