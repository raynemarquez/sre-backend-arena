# ---------------------------------------------------------------------------
# Stage 1 — builder
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /app/venv \
    && /app/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm

WORKDIR /app

RUN groupadd --gid 1000 appgroup \
    && useradd --uid 1000 --gid appgroup --no-create-home appuser

COPY --from=builder /app/venv /app/venv
COPY src/ ./src/

RUN chown -R appuser:appgroup /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
  CMD /app/venv/bin/python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# IMPORTANTE: --workers 1 
#
# O cache in-memory (_AsyncTTLCache e _index no HPApiClient) é LOCAL ao processo.
# Com 2 workers (2 processos separados), cada processo teria seu próprio cache —
# duplicando o uso de memória e dobrando as chamadas ao warmup da HP-API.
# Pior: se um worker faz warmup e outro não, os dois recebem tráfego de forma
# imprevisível, resultando em cache misses desnecessários.
#
# Com 1 worker + uvloop, o event loop lida com milhares de conexões concorrentes
# de forma cooperativa — sem GIL, sem contention entre processos.
# A escala horizontal é feita pelo HPA adicionando pods (cada pod = 1 cache isolado
# e completo), não dentro do pod com múltiplos workers.
CMD ["/app/venv/bin/uvicorn", "src.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--loop", "uvloop", \
     "--http", "httptools"]
