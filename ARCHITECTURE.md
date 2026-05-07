# ARCHITECTURE.md — Wizard Intelligence Network

## 1. Visão geral

API HTTP que retorna inteligência sobre bruxos do universo Harry Potter, integrando com a [HP API](https://hp-api.onrender.com). Construída para suportar **10.000 RPS** dentro de um budget de **1.5 CPU / 350MB RAM** para a stack completa em Kubernetes local (k3d).

O design parte de uma premissa central: **os dados da HP API são estáticos**. Personagens não mudam entre chamadas. Isso viabiliza uma estratégia agressiva de cache in-memory com warmup na inicialização, eliminando praticamente todo I/O externo em operação normal.

---

## 2. Fluxo de uma requisição

```
GET /wizard/{name}
      │
      ▼
[Traefik Ingress — k3d porta 8080]
  Middleware: RateLimit (10k avg RPS, burst 20k)
      │
      ▼
[FastAPI + uvicorn — uvloop + httptools — 1 worker por pod]
  Middleware de observabilidade:
    · Propaga / gera X-Request-ID (correlation_id)
    · Injeta correlation_id, trace_id no contextvars do structlog
    · Registra REQUEST_COUNT, REQUEST_LATENCY por endpoint
    · Atualiza gauge CIRCUIT_BREAKER_STATE a cada request
      │
      ├─ L1: _AsyncTTLCache (in-memory, asyncio.Lock, TTL 5 min)
      │       Lookup por nome normalizado (lowercase + strip) — O(1)
      │       Hit válido  → retorna ~0ms, header X-Cache: HIT
      │       Hit stale   → guardado para fallback se API falhar
      │
      ├─ L2: index em memória (_index dict, populado no warmup)
      │       Re-enriquece (powerScore + loyalty) e repopula L1
      │
      └─ HP API externa (apenas em cache miss total)
              _TokenBucketRateLimiter (10 req/s, burst 20)
                └─ @retry tenacity (3x, backoff 2s→4s→8s)
                      Retry apenas em httpx.RequestError (rede/timeout)
                      Não retenta em HTTPStatusError (4xx/5xx)
                          └─ _fetch_with_circuit_breaker
                                circuit_breaker (pybreaker)
                                fail_max=5, reset_timeout=10s
                                API pública apenas (sem atributos privados)
                                    └─ Stale cache ou HTTP 503
```

---

## 3. Componentes da aplicação

### `src/main.py`

Contém a aplicação FastAPI, o middleware de observabilidade, as métricas Prometheus e os três endpoints de infraestrutura (`/health`, `/ready`, `/metrics`) além do endpoint de negócio (`/wizard/{name}`).

O tracing OpenTelemetry é ativado condicionalmente via `ENABLE_TRACING=true`. Quando a variável não está definida, o bloco `setup_tracing()` não é chamado e não há overhead algum.

### `src/services/hp_api.py`

Toda a lógica de resiliência vive aqui:

- `_AsyncTTLCache` — cache in-memory com `asyncio.Lock()`, TTL configurável, maxsize com eviction LRU, retorno triplo `(valor, is_valid, is_stale)` para implementar o padrão stale-while-revalidate
- `_TokenBucketRateLimiter` — token bucket assíncrono com refill contínuo, bloqueia até ter token disponível em vez de rejeitar a chamada
- `_cb_record_failure` / `_cb_record_success` — wrappers que notificam o `pybreaker` via `circuit_breaker.call()` sem tocar em `_state_storage` ou `_inc_counter`
- `HPApiClient.warmup_cache()` — chamado no lifespan do FastAPI, popula `_index` e o cache L1 com todos os personagens enriquecidos antes de aceitar tráfego

### `src/observability/tracing.py`

Setup do OpenTelemetry com `FastAPIInstrumentor` e `HTTPXClientInstrumentor`. Exporta via OTLP HTTP para o Tempo quando `OTEL_EXPORTER_OTLP_ENDPOINT` está definido. Implementado e testado, mas **desabilitado por padrão** no `values.yaml` (`otel.enabled: false`) para manter o budget de RAM local.

### `src/models/wizard.py`

Pydantic v2 model com os cinco campos da resposta. Garante serialização correta e validação de tipo em tempo de inicialização.

---

## 4. Separação liveness / readiness / startup

| Probe | Endpoint | O que verifica | Comportamento em falha |
|-------|----------|----------------|------------------------|
| Startup | `/ready` | cache pronto + CB fechado | K8s aguarda até `failureThreshold × periodSeconds` (10 min) antes de reiniciar |
| Liveness | `/health` | processo vivo | K8s reinicia o pod |
| Readiness | `/ready` | cache pronto + CB fechado | K8s retira o pod do Service sem reiniciá-lo |

O `startupProbe` aponta para `/ready` (e não `/health`) porque o pod não deve receber tráfego antes do warmup terminar. Com `failureThreshold: 60` e `periodSeconds: 10`, o K8s aguarda até 10 minutos — tempo mais que suficiente para o warmup da HP API em condições adversas.

O `/ready` verifica `hp_api_client._index` (não vazio = warmup concluído) e `circuit_breaker.current_state != "open"`. O `/health` retorna 200 sempre que o processo Python está vivo — sem verificação de dependências.

---

## 5. Cálculo do powerScore

A HP API não fornece um campo de "poder". O score é calculado deterministicamente a partir de atributos reais:

```
powerScore = 50  (base)
           + 20  (wizard == true)
           + 15  (house != "")
           + 15  (wand.wood != "" ou wand.core != "")
           ────
           máximo: 100
```

O cálculo é determinístico — a mesma entrada sempre produz o mesmo resultado. Isso garante consistência entre réplicas que não compartilham cache: dois pods retornam o mesmo `powerScore` para o mesmo personagem.

---

## 6. Métricas expostas

| Métrica | Tipo | Labels | Descrição |
|---------|------|--------|-----------|
| `api_requests_total` | Counter | `method`, `endpoint`, `http_status` | Requisições totais (exceto `/health`, `/ready`, `/metrics`) |
| `api_errors_total` | Counter | `type` | Erros por tipo: `not_found`, `circuit_breaker`, `internal_error` |
| `cache_hits_total` | Counter | — | Lookups servidos do cache |
| `cache_misses_total` | Counter | — | Lookups que precisaram da HP API |
| `http_request_duration_seconds` | Histogram | `method`, `endpoint` | Latência com buckets de 5ms a 1s |
| `circuit_breaker_open` | Gauge | — | `1` quando aberto, `0` quando fechado |

Os buckets do histograma foram ajustados para os SLOs: `0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0`. Isso permite calcular p99 < 300ms e p99 < 150ms com precisão no Grafana.

---

## 7. Budget de recursos

Budget do desafio (ambiente local): **1.5 CPU / 350MB RAM** para toda a stack.

| Componente | CPU request | CPU limit | RAM request | RAM limit |
|------------|-------------|-----------|-------------|-----------|
| App (2 réplicas — HPA max) | 2 × 100m = **200m** | 2 × 500m = 1000m | 2 × 60Mi = **120Mi** | 2 × 110Mi = 220Mi |
| VictoriaMetrics | **30m** | 150m | **60Mi** | 120Mi |
| Grafana | **25m** | 100m | **60Mi** | 100Mi |
| Loki | **20m** | 100m | **40Mi** | 80Mi |
| Promtail | **20m** | 50m | **30Mi** | 50Mi |
| Tempo *(desabilitado)* | — | — | — | — |
| **TOTAL requests** | **295m** | 1400m | **310Mi** | 570Mi |

Os **requests** são o que o scheduler Kubernetes reserva e são o número relevante para o budget: **295m CPU e 310Mi RAM**, dentro dos limites de 1500m e 350Mi.

Os **limits** representam o teto teórico com todos os componentes no pico simultâneo — o que não ocorre em operação normal. A RAM limit da app por réplica foi reduzida para 110Mi (vs 180Mi em iterações anteriores) após observar que o processo Python fica em ~70–80Mi em carga.

**Por que o Tempo está desabilitado por padrão**: Tempo + suas dependências adicionariam ~25Mi de RAM request e 60Mi de limit, forçando o limite total de RAM requests para ~335Mi — ainda dentro do budget mas sem margem. Como o tracing é opcional para o desafio e requer `ENABLE_TRACING=true` na aplicação, a decisão foi deixá-lo documentado e fácil de ativar, mas fora do `make all`.

---

## 8. Infraestrutura como código

Toda a infraestrutura é reproduzível a partir de um único comando (`make all`):

| Camada | Ferramenta | Localização |
|--------|-----------|-------------|
| App: Deployment, Service, Ingress, HPA, PDB, NetworkPolicy, ServiceAccount | Helm chart | `infra/helm/wizard-intelligence-network/` |
| Observabilidade: VictoriaMetrics, Grafana, Loki, Promtail | Helm values | `infra/observability/` |
| Datasource, dashboard e alertas Grafana | ConfigMaps via Helm + kubectl apply | `grafana-alerts.yaml`, `grafana-dashboard.yaml`, `grafana-configmaps-monitoring.yaml` |

O Grafana recebe datasources, dashboards e alertas exclusivamente via arquivos montados em `/etc/grafana/provisioning/` — sem configuração manual na UI e sem dependência de sidecars.

---

## 9. Observabilidade

### Stack

```
App ──stdout──────────────► Promtail ──► Loki (logs)
App ──/metrics────────────► VictoriaMetrics (métricas)
App ──OTLP HTTP (opcional)► Tempo (traces)
                                │
                            Grafana
                         ┌──────────────┐
                         │ VictoriaMetrics │
                         │ Loki            │
                         │ Tempo           │
                         └──────────────┘
```

### Logs estruturados

Todos os logs são emitidos em JSON via `structlog`. O processor `_inject_trace_context` injeta `correlation_id`, `trace_id` e `otel_trace_id` em cada log a partir do `contextvars` por requisição. O Promtail extrai `level` e `trace_id` como labels Loki via pipeline `json`, permitindo filtrar por trace sem grep.

```json
{
  "timestamp": "2026-05-07T12:00:00Z",
  "level": "info",
  "event": "cache_hit_valid",
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
  "trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "otel_trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "wizard": "harry potter"
}
```

### SLOs e alertas (como código)

| SLO | Objetivo | Alerta | Severidade |
|-----|----------|--------|-----------|
| Disponibilidade | 99.9% das requests != 5xx | Taxa de erro > 0.1% por 2 min | `critical` |
| Latência | p99 < 300ms no `/wizard/{name}` | p99 > 300ms por 5 min | `warning` |
| Circuit Breaker | Fechado (HP API acessível) | Qualquer erro CB por 1 min | `warning` |
| Cache Hit Rate | > 50% das requests do cache | Hit rate < 50% por 10 min | `info` |

---

## 10. CI/CD

```
lint-python ──┐
              ├──► test ──┐
lint-helm   ──┘           └──► build (Docker com cache GHA)
security ─────────────────────►
```

| Job | Ferramentas |
|-----|------------|
| `lint-python` | `ruff check`, `ruff format --check`, `mypy --ignore-missing-imports` |
| `lint-helm` | `helm lint`, `helm template` (renderiza os manifests) |
| `test` | `pytest --cov=src --cov-fail-under=70` |
| `security` | `bandit -r src/ -ll --skip B104` |
| `build` | `docker/build-push-action` com `push: false` |

O deploy não está no pipeline de CI — o k3d local não expõe kubeconfig para o GitHub Actions. Em cloud, o step seria `helm upgrade --install` após o build, com kubeconfig injetado via secret.

---

## 11. Segurança

### Container

- UID/GID 1000 (non-root) criados explicitamente no Dockerfile com `useradd`
- `readOnlyRootFilesystem: true` — filesystem imutável em runtime
- `/tmp` como `emptyDir` — permite arquivos temporários do Python/uvicorn sem abrir o filesystem raiz
- `allowPrivilegeEscalation: false`
- `capabilities: drop: [ALL]`

### NetworkPolicy

| Direção | Origem / Destino | Porta |
|---------|-----------------|-------|
| Ingress | `kube-system` (Traefik) | TCP 8000 |
| Ingress | `monitoring` (VictoriaMetrics scrape) | TCP 8000 |
| Egress | `kube-system` (CoreDNS) | UDP/TCP 53 |
| Egress | Internet pública exceto RFC1918 (HP API) | TCP 443 |
| Egress | `monitoring` (Tempo) | TCP 4317, 4318 |

A regra de egress exclui `10.0.0.0/8`, `172.16.0.0/12` e `192.168.0.0/16` — bloqueia SSRF para endereços internos do cluster e da rede do host.

### Secrets

O secret do Grafana (`grafana-admin-secret`) é criado via `kubectl create secret` antes do `make all` — nunca commitado. O `.env` está no `.gitignore` com entrada explícita para o arquivo sem extensão. O SAST do `bandit` roda em todo PR; a exceção `B104` (bind em `0.0.0.0`) é explícita e documentada.
