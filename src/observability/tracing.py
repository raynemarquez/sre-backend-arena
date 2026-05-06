"""
OpenTelemetry setup — traces exportados via OTLP HTTP para Tempo.

Ativação controlada por variável de ambiente:
  OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo.monitoring.svc:4318

Se a variável não estiver definida, o tracing fica no-op (sem overhead).
Isso garante que a app sobe normalmente em dev local sem Tempo rodando.
"""

import os
import logging

logger = logging.getLogger(__name__)


def setup_tracing(app) -> None:
    """
    Instrumenta a aplicação FastAPI com OpenTelemetry.
    Auto-instrumenta httpx e garante propagação de contexto.
    """
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

    if not endpoint:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set — tracing disabled (no-op)")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        resource = Resource.create(
            {
                SERVICE_NAME: os.getenv(
                    "OTEL_SERVICE_NAME", "wizard-intelligence-network"
                ),
                SERVICE_VERSION: os.getenv("APP_VERSION", "1.0.0"),
                "deployment.environment": os.getenv("APP_ENV", "local"),
            }
        )

        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(
            endpoint=f"{endpoint.rstrip('/')}/v1/traces",
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=provider,
            excluded_urls="/health,/metrics",  # não criar spans para probes
        )
        HTTPXClientInstrumentor().instrument(tracer_provider=provider)

        logger.info(f"OpenTelemetry tracing enabled → {endpoint}")

    except ImportError as e:
        logger.warning(
            f"OpenTelemetry packages not installed — tracing disabled: {e}. "
            "Install: opentelemetry-sdk opentelemetry-exporter-otlp-proto-http "
            "opentelemetry-instrumentation-fastapi opentelemetry-instrumentation-httpx"
        )
