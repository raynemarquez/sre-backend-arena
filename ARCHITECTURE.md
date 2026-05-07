# ARCHITECTURE.md — Wizard Intelligence Network

## 1. Visão Geral

API HTTP que retorna inteligência sobre bruxos do universo Harry Potter, integrando com a [HP API](https://hp-api.onrender.com). Construída para suportar **10.000 RPS** dentro de um budget rígido de **1.5 CPU / 350 MB RAM** para a stack completa em Kubernetes local (k3d).

O design parte de uma premissa central: **os dados da HP API são estáticos**. Personagens não mudam entre chamadas. Isso permite uma estratégia agressiva de cache in-memory com warmup na inicialização, eliminando praticamente todo I/O externo em operação normal e tornando a latência dominada pelo processamento local — na faixa de microssegundos.

---

## 2. Fluxo de uma Requisição

```
GET /wizard/{name}
      │
      ▼
[Traefik Ingress — k3d porta 8080]
  Rate limiting de borda via IngressRoute middleware
      │
      ▼
[FastAPI + uvicorn (uvloop + httptools) — 1 worker por pod]
  Middleware de observabilidade:
    · Gera ou propaga X-Request-ID (correlation_id)
    · Injeta trace_id e otel_trace_id nos logs estruturados
    · Registra métricas de latência e status HTTP
    · Atualiza Gauge do circuit breaker
      │
      ├─ 1. Cache L1 — _AsyncTTLCache (in-memory, TTL 5min)
      │       Lookup O(1) por nome normalizado (lowercase + strip)
      │       Hit válido  → retorna imediatamente (~0ms, sem I/O)
      │       Hit stale   → guardado para fallback se a API falhar
      │
      ├─ 2. Index em memória (pré-carregado no warmup)
      │       Se L1 expirou, busca em dict _index — O(1)
      │       Re-enriquece (powerScore + loyalty) e repopula L1
      │
      └─ 3. HP API externa (apenas em cache miss total)
              Token Bucket (10 req/s, burst 20)
                └─ Retry via tenacity (3x, backoff 2s → 4s → 8s)
                      └─ Circuit Breaker pybreaker
                              5 falhas consecutivas → abre
                              reset automático após 10s
                            └─ Se aberto: stale cache ou HTTP 503
```

### Cache warmup na inicialização

Antes de aceitar tráfego, o `lifespan` do FastAPI chama `warmup_cache()`, que busca todos os personagens da HP API, calcula `powerScore` e `loyalty` para cada um, e popula o cache L1 e o `_index` em memória. O `startupProbe` no K8s aponta para `/ready` — o pod só entra no balanceamento após o cache estar populado.

Isso garante que a primeira requisição de qualquer bruxo seja tão rápida quanto a milésima.

---

## 3. Endpoints

| Método | Path | Descrição |
|---|---|---|
| `GET` | `/wizard/{name}` | Retorna inteligência sobre um bruxo |
| `GET` | `/health` | Liveness probe — verifica se o processo está vivo |
| `GET` | `/ready` | Readiness probe — verifica cache + circuit breaker |
| `GET` | `/metrics` | Scrape endpoint Prometheus para VictoriaMetrics |

**Separação liveness / readiness**: `/health` é leve e sempre responde 200 enquanto o processo Python estiver vivo. `/ready` retorna 503 se o cache não estiver populado ou se o circuit breaker estiver aberto — sinalizando ao K8s para retirar o pod do balanceamento sem reiniciá-lo desnecessariamente.

O `startupProbe` aponta para `/ready` com `failureThreshold: 60` (até 10 minutos), aguardando o warmup antes de liberar tráfego. O `livenessProbe` aponta para `/health` e reinicia o pod apenas se o processo Python travar.

---

## 4. Cálculo do powerScore

A HP API não fornece um campo de "poder". O score é calculado deterministicamente a partir de atributos reais de cada personagem:

```
powerScore = 50  (base para qualquer entidade)
           + 20  (se wizard == true)
           + 15  (se house != "")
           + 15  (se wand.wood != "" ou wand.core != "")
           ────
           máximo: 100
```

O cálculo é **determinístico** — a mesma entrada sempre produz o mesmo resultado. Isso é essencial para consistência entre réplicas que não compartilham cache: dois pods retornam o mesmo `powerScore` para o mesmo personagem.

---

## 5. Estratégia de Cache

### Por que cache in-memory em vez de Redis?

Redis adicionaria ~30 MB de RAM, latência de rede por request (~1ms), um ponto de falha extra e um secret de senha — tudo isso sem benefício real, dado que os dados da HP API **não mudam**. O cache in-memory entrega latência ~0ms e simplicidade operacional.

A única desvantagem — cache não compartilhado entre réplicas — é mitigada pelo warmup automático: cada pod popula seu próprio cache na inicialização. Com dados estáticos, um miss eventual numa réplica gera no máximo uma chamada à HP API por TTL, controlada pelo rate limiter.

### Por que implementação manual em vez de cachetools ou aiocache?

`cachetools` não é async-safe. `aiocache` adiciona abstração desnecessária e dificulta o padrão stale-while-revalidate. A implementação manual com `asyncio.Lock()` tem ~60 linhas, é completamente auditável e expõe exatamente o comportamento necessário: TTL configurável, maxsize com eviction do item mais antigo, e distinção entre *hit válido* e *hit stale* para fallback.

---

## 6. Rate Limiting Client-side

A HP API não publica limites oficiais — é um serviço público free tier hospedado no Render. A estratégia é conservadorismo explícito:

- **Algoritmo**: Token Bucket assíncrono com `asyncio.Lock()`
- **Taxa**: 10 tokens/s, burst de 20 (absorve picos do retry)
- **Comportamento**: bloqueia (aguarda token) em vez de rejeitar — nenhuma chamada à API externa é descartada, apenas atrasada
- **Impacto real**: em operação normal após warmup, zero chamadas chegam à HP API por TTL de 5 minutos, portanto o rate limiter raramente é acionado

O rate limiter funciona como uma **fila**, não como uma porta. Isso evita que retries sob falha gerem burst acima do limite da API.

---

## 7. Resiliência — Camadas de Defesa

O sistema implementa defesa em profundidade: cada camada protege as camadas internas de serem acionadas desnecessariamente.

| Camada | Mecanismo | Comportamento |
|---|---|---|
| 1ª | Cache L1 válido (TTL ativo) | Retorna em ~0ms, sem I/O |
| 2ª | Index em memória | Re-enriquece e retorna, sem chamada HTTP |
| 3ª | Rate limiter (token bucket) | Throttle suave antes de chamar a API |
| 4ª | Retry com backoff exponencial | 3 tentativas: 2s / 4s / 8s — apenas em `RequestError` |
| 5ª | Circuit Breaker | Para após 5 falhas; reset em 10s |
| 6ª | Stale cache fallback | Serve dado expirado se API indisponível |
| 7ª | HTTP 503 | Somente se não houver nenhum dado em cache |

### Decisão: retry apenas em `RequestError`, não em `HTTPStatusError`

`RequestError` indica falha de rede ou timeout — situações transitórias onde retry faz sentido. `HTTPStatusError` (4xx, 5xx) indica que a API respondeu com erro — tentar de novo imediatamente não muda o resultado, só gera mais carga. A distinção é implementada via `retry_if_exception_type(httpx.RequestError)`.

### Integração do pybreaker com asyncio

`pybreaker` é uma biblioteca síncrona. Para integrá-la num event loop assíncrono sem usar APIs internas (que poderiam quebrar em versões futuras), o sucesso e a falha são notificados via funções síncronas chamadas após a resolução da coroutine — sem tocar em `_state_storage`, `_inc_counter` ou outros atributos privados.

---

## 8. Budget de Recursos

Budget do desafio para ambiente local: **1.5 CPU / 350 MB RAM** para toda a stack.

| Componente | CPU request | CPU limit | RAM request | RAM limit |
|---|---|---|---|---|
| App (2 réplicas — HPA max local) | 200m | 1000m | 120 Mi | 360 Mi |
| VictoriaMetrics | 30m | 150m | 60 Mi | 100 Mi |
| Grafana | 25m | 100m | 60 Mi | 150 Mi |
| Tempo | 20m | 100m | 25 Mi | 60 Mi |
| Loki | 20m | 50m | 40 Mi | 60 Mi |
| Promtail | 20m | 50m | 30 Mi | 50 Mi |
| **Total requests** | **315m** | — | **335 Mi** | — |

**Requests** são o que o scheduler Kubernetes reserva — **315m CPU e 335 Mi RAM**, dentro do budget. **Limits** são o teto teórico se todos os componentes estiverem sob carga máxima simultânea, o que não ocorre em operação normal.

Ajuste crítico para caber no budget: `memory.allowedPercent: 30` no VictoriaMetrics limita o cache TSDB a ~30 Mi, evitando que o processo suba para 200 Mi+.

---

## 9. Observabilidade

### Stack e fluxo de dados

```
App (structlog) ──stdout──────────► Promtail ──► Loki (logs)
App (prometheus-client) ──/metrics──► VictoriaMetrics (métricas)
App (opentelemetry-sdk) ──OTLP HTTP──► Tempo (traces)
                                            │
                                        Grafana
                                    ┌──────────────┐
                                    │ VictoriaMetrics│
                                    │ Loki           │
                                    │ Tempo          │
                                    └──────────────┘
```

### Métricas customizadas

| Métrica | Tipo | Labels | Descrição |
|---|---|---|---|
| `api_requests_total` | Counter | `method`, `endpoint`, `http_status` | Total de requisições recebidas |
| `api_errors_total` | Counter | `type` | Erros por tipo |
| `http_request_duration_seconds` | Histogram | `method`, `endpoint` | Latência com buckets para SLO p99 |
| `cache_hits_total` | Counter | — | Cache hits no lookup de personagens |
| `cache_misses_total` | Counter | — | Cache misses |
| `circuit_breaker_open` | Gauge | — | 1 quando aberto, 0 quando fechado |

Endpoints de infraestrutura (`/health`, `/ready`, `/metrics`) são excluídos das métricas de latência para não distorcer os percentis do SLO.

### Logs estruturados

Todos os logs são emitidos em JSON via `structlog`. Cada log de requisição contém:

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

O `correlation_id` é propagado do header `X-Request-ID` enviado pelo caller, ou gerado automaticamente se ausente, e devolvido no response via `X-Request-ID`. O Promtail extrai `level`, `trace_id` e `correlation_id` como labels Loki via pipeline `json`.

O Grafana tem `derivedFields` configurado no datasource Loki para transformar o `trace_id` em link direto para o trace correspondente no Tempo — sem nenhuma ação manual.

### SLOs e alertas

Todos os alertas são provisionados como código via ConfigMaps do Helm — sem configuração manual no Grafana.

| SLO | Objetivo | Alerta |
|---|---|---|
| Disponibilidade | 99.9% das requests sem 5xx | Taxa de erro > 0.1% por 2 min |
| Latência p99 | < 300ms no `/wizard/{name}` | p99 > 300ms por 5 min |
| Circuit Breaker | Fechado (HP API acessível) | Qualquer abertura por 1 min |
| Cache Hit Rate | > 50% servidas do cache | Hit rate < 50% por 10 min |

### Traces (OpenTelemetry)

O tracing é ativado pela variável de ambiente `OTEL_EXPORTER_OTLP_ENDPOINT`. Se não estiver definida, o SDK opera em modo **no-op** — sem overhead, sem conexão, sem erro. Em cluster com o Tempo instalado, o endpoint aponta para `http://tempo.monitoring.svc:4318` via OTLP HTTP.

A instrumentação via `FastAPIInstrumentor` e `HTTPXClientInstrumentor` gera spans automaticamente para cada request recebido e cada chamada à HP API, permitindo visualizar a latência end-to-end no Grafana.

Usar OpenTelemetry SDK em vez de um SDK proprietário garante que, se o backend de traces mudar (Jaeger, Zipkin, Datadog), apenas o `OTEL_EXPORTER_OTLP_ENDPOINT` e o exporter precisam ser trocados — sem alterar o código da aplicação.

---

## 10. Infrastructure as Code

Toda a infraestrutura é versionada e reproduzível a partir de um único comando:

```bash
make all   # cria cluster k3d + instala observabilidade + faz deploy da app
```

| Camada | Ferramenta | Localização |
|---|---|---|
| App (Deployment, Service, Ingress, HPA, PDB, NetworkPolicy) | Helm chart | `infra/helm/wizard-intelligence-network/` |
| Observabilidade (VictoriaMetrics, Grafana, Tempo, Loki, Promtail) | Helm values | `infra/observability/` |
| Datasources, dashboards e alertas Grafana | ConfigMaps via Helm | `infra/helm/.../grafana-*.yaml` + `grafana-configmaps-monitoring.yaml` |
| CI/CD | GitHub Actions | `.github/workflows/ci.yml` |

O Grafana recebe datasources, dashboards e regras de alerta **exclusivamente via ConfigMaps** montados em `provisioning/` — nenhuma configuração é feita manualmente na UI. Isso garante que o estado do Grafana seja sempre o do código, mesmo após restart do pod.

### Por que Helm e não Kustomize ou manifests puros?

Os valores diferem entre ambiente local (k3d) e cloud — `pullPolicy: Never` vs `IfNotPresent`, `maxReplicas: 2` vs `3`, endpoint OTEL diferente. Helm permite `values.yaml` por ambiente sem duplicar manifests. As ferramentas de observabilidade são distribuídas como Helm charts oficiais — usar Helm mantém consistência de ferramenta em toda a stack.

### Por que k3d e não minikube ou kind?

k3d inicia em ~20 segundos, usa menos memória que minikube e vem com Traefik como ingress controller por padrão. O `make cluster-create` mapeia a porta 8080 do host para a porta 80 do LoadBalancer, simulando o ingress de produção localmente sem configuração adicional.

---

## 11. CI/CD

O pipeline GitHub Actions executa na ordem:

```
lint ──────┐
           ├──► test ──► build (Docker)
security ──┘
validate-iac ──────────────────────────┘
```

| Job | O que valida |
|---|---|
| `lint` | `ruff check`, `ruff format --check`, `bandit -r src/ -ll` (SAST) |
| `test` | `pytest` com cobertura mínima de 70%, relatório XML como artifact |
| `validate-iac` | `helm lint -f values.yaml`, `helm template`, `terraform fmt -check`, `terraform validate` |
| `build` | `docker build` com cache GitHub Actions, exporta tarball como artifact |

O `helm lint` e o `helm template` são executados com `-f values.yaml` para validar os values reais — sem isso, o CI usaria defaults e poderia mascarar referências quebradas.

O `build` só roda após todos os jobs anteriores passarem. A imagem não é publicada em registry — o deploy local é feito via `k3d image import`.

---

## 12. Segurança

### Container

- Executa como UID 1000 (non-root), grupo 1000
- `readOnlyRootFilesystem: true` — filesystem imutável em runtime
- `/tmp` montado como `emptyDir` para arquivos temporários do Python/uvicorn
- `allowPrivilegeEscalation: false`
- `capabilities: drop: [ALL]`

### Rede (NetworkPolicy)

O pod da aplicação só pode se comunicar com:

| Direção | Destino | Porta |
|---|---|---|
| Ingress | `kube-system` (Traefik) | TCP 8000 |
| Ingress | `monitoring` (VictoriaMetrics scrape) | TCP 8000 |
| Egress | `kube-system` (CoreDNS) | UDP/TCP 53 |
| Egress | Internet pública, exceto RFC1918 (HP API) | TCP 443 |
| Egress | `monitoring` (Tempo — OTLP) | TCP 4317/4318 |

A exclusão de RFC1918 no egress bloqueia tentativas de SSRF (Server-Side Request Forgery) para endereços internos do cluster ou da rede do host.

Restringir ingress ao namespace `kube-system` garante que **todo tráfego de negócio passa pelo Traefik** — nenhum pod pode contornar os rate limits do ingress chamando diretamente a porta do serviço.

### Secrets

Nenhum secret é armazenado em código, ConfigMap ou repositório. O secret do Grafana é criado via `kubectl create secret` antes do deploy. O arquivo `.env` está no `.gitignore`. O SAST via `bandit` roda em cada PR com `-ll` (medium e high severity).

---

## 13. Performance — Runtime

### Python + FastAPI + uvicorn

Python 3.11 com FastAPI, uvicorn rodando com `--loop uvloop --http httptools`, 1 worker por pod.

O uvloop é uma reimplementação do event loop do asyncio em Cython sobre libuv (a mesma base do Node.js), entregando throughput significativamente maior em benchmarks de I/O. O httptools é um parser HTTP em C. Juntos, reduzem o overhead de parsing e scheduling — crítico quando o objetivo é 10k RPS dentro de um budget de CPU apertado.

### Por que 1 worker por pod?

Múltiplos workers competem pelo GIL e criam contenção na memória compartilhada do cache. Com 1 worker por pod, o cache in-memory é exclusivo do processo — sem lock contention, sem invalidação cruzada. A escala horizontal é feita pelo HPA adicionando pods, não aumentando workers. Isso é mais previsível e mais fácil de observar.

O flag `uvloop` só funciona em Linux — no Windows o uvicorn cai automaticamente para o asyncio padrão, o que não afeta o desenvolvimento local.
