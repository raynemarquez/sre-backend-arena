# ARCHITECTURE.md — Wizard Intelligence Network

## 1. Visão Geral

API HTTP que retorna inteligência sobre bruxos do universo Harry Potter, integrando com a [HP API](https://hp-api.onrender.com). Construída para suportar **10.000 RPS** dentro de um budget rígido de **1.5 CPU / 350MB RAM** para a stack completa em Kubernetes local (k3d).

O design parte de uma premissa central: **os dados da HP API são estáticos**. Personagens não mudam entre chamadas. Isso permite uma estratégia agressiva de cache in-memory com warmup na inicialização, eliminando praticamente todo I/O externo em operação normal e tornando a latência dominada pelo processamento local — na faixa de microssegundos.

---

## 2. Fluxo de uma Requisição

```
GET /wizard/{name}
      │
      ▼
[Traefik Ingress — k3d porta 8080]
  Rate limit de borda via IngressRouteCRD middleware
      │
      ▼
[FastAPI + uvicorn (uvloop + httptools) — 1 worker por pod]
  Middleware de observabilidade:
    · Gera/propaga X-Request-ID (correlation_id)
    · Injeta trace_id nos logs estruturados
    · Registra métricas de latência e status
      │
      ├─ 1. Cache L1: _AsyncTTLCache (in-memory, TTL 5min)
      │       Lookup O(1) por nome normalizado (lowercase + strip)
      │       Hit válido  → retorna imediatamente, ~0ms
      │       Hit stale   → guardado para fallback se API falhar
      │
      ├─ 2. Index em memória (pré-carregado no warmup)
      │       Se L1 expirou, busca no dict _index (O(1))
      │       Re-enriquece (powerScore + loyalty) e repopula L1
      │
      └─ 3. HP API externa (apenas em cache miss total)
              Token Bucket (10 req/s, burst 20)
                └─ Retry via tenacity (3x, backoff 2s → 4s → 8s)
                      └─ Circuit Breaker pybreaker
                            5 falhas → abre; reset após 10s
                            └─ Se aberto: stale cache ou HTTP 503
```

### Por que cache in-memory em vez de Redis?

Redis adicionaria ~30MB de RAM (o deployment + memória de dados), latência de rede por request (~1ms) e um ponto de falha extra no cluster — tudo isso sem benefício real, dado que os dados da HP API **não mudam**. O cache in-memory entrega latência ~0ms e simplicidade operacional.

A única desvantagem — cache não compartilhado entre réplicas — é mitigada pelo warmup automático: cada pod popula seu próprio cache na inicialização, antes de aceitar tráfego. Com dados estáticos, um miss eventual numa réplica gera no máximo uma chamada à HP API por TTL, controlada pelo rate limiter.

Se o sistema evoluísse para dados dinâmicos ou dezenas de réplicas, a adição natural seria Redis com TTL configurável.

---

## 3. Estratégia de Rate Limit (Client-side)

A HP API não publica limites oficiais — é um serviço público free tier hospedado no Render. Adotamos conservadorismo explícito:

- **Algoritmo**: Token Bucket assíncrono, implementado com `asyncio.Lock()`
- **Taxa**: 10 tokens/s, burst de 20 (absorve picos do retry)
- **Comportamento**: bloqueia (aguarda token) em vez de rejeitar — nenhuma chamada à API externa é descartada, apenas atrasada
- **Impacto real**: em operação normal após warmup, **zero chamadas** chegam à HP API, portanto o rate limiter nunca é acionado

---

## 4. Resiliência — Camadas de Defesa

O sistema implementa defesa em profundidade: cada camada protege as camadas internas de serem acionadas desnecessariamente.

| Camada | Mecanismo | Comportamento |
|--------|-----------|---------------|
| 1ª | Cache L1 válido (TTL ativo) | Retorna em ~0ms, sem I/O |
| 2ª | Index em memória | Re-enriquece e retorna, sem chamada HTTP |
| 3ª | Rate limiter (token bucket) | Throttle suave antes de chamar a API |
| 4ª | Retry com backoff exponencial | 3 tentativas: 2s / 4s / 8s (somente `RequestError`) |
| 5ª | Circuit Breaker | Para completamente após 5 falhas; reset em 10s |
| 6ª | Stale cache fallback | Serve dado expirado se API indisponível |
| 7ª | HTTP 503 | Somente se não houver nenhum dado em cache |

**Decisão de design no Circuit Breaker**: o `pybreaker` é uma biblioteca síncrona. Para integrá-lo corretamente num event loop assíncrono sem tocar em atributos privados, o sucesso e a falha são notificados via `circuit_breaker.call()` com funções síncronas — `_cb_record_success()` e `_cb_record_failure()` — chamadas após a resolução da coroutine. Isso garante compatibilidade com versões futuras da biblioteca.

---

## 5. Endpoints

| Método | Path | Descrição |
|--------|------|-----------|
| `GET` | `/wizard/{name}` | Retorna inteligência sobre um bruxo |
| `GET` | `/health` | Liveness probe — verifica se o processo está vivo |
| `GET` | `/ready` | Readiness probe — verifica cache + circuit breaker |
| `GET` | `/metrics` | Scrape endpoint Prometheus para VictoriaMetrics |

**Separação liveness / readiness**: `/health` é leve e sempre responde 200 enquanto o processo Python estiver vivo. `/ready` retorna 503 se o cache não estiver populado ou se o circuit breaker estiver aberto — sinalizando ao K8s para retirar o pod do balanceamento sem reiniciá-lo.

O `startupProbe` aponta para `/ready` com `failureThreshold: 60` (até 10 minutos), aguardando o warmup do cache antes de liberar tráfego. O `livenessProbe` aponta para `/health` e só reinicia o pod se o processo travar completamente.

---

## 6. Cálculo do powerScore

A HP API não fornece um campo de "poder". O score é calculado deterministicamente a partir de atributos reais de cada personagem:

```
powerScore = 50  (base para qualquer entidade)
           + 20  (se wizard == true)
           + 15  (se house != "")
           + 15  (se wand.wood != "" ou wand.core != "")
           ────
           máximo: 100
```

O cálculo é **determinístico** — a mesma entrada sempre produz o mesmo resultado. Isso é essencial para consistência entre réplicas que não compartilham cache: dois pods diferentes retornam o mesmo `powerScore` para o mesmo personagem.

---

## 7. Budget de Recursos

O budget do desafio para ambiente local é **1.5 CPU / 350MB RAM** para toda a stack.

| Componente | CPU request | CPU limit | RAM request | RAM limit |
|------------|-------------|-----------|-------------|-----------|
| App (2 réplicas — HPA max local) | 2 × 100m = **200m** | 2 × 500m = 1000m | 2 × 60Mi = **120Mi** | 2 × 180Mi = 360Mi |
| VictoriaMetrics | **30m** | 150m | **60Mi** | 100Mi |
| Grafana | **25m** | 100m | **60Mi** | 150Mi |
| Tempo | **20m** | 100m | **25Mi** | 60Mi |
| Loki | **20m** | 50m | **40Mi** | 60Mi |
| Promtail | **20m** | 50m | **30Mi** | 50Mi |
| **TOTAL requests** | **315m** | 1450m | **335Mi** | 780Mi |

**Requests** são o que o scheduler Kubernetes reserva: **315m CPU e 335Mi RAM** — dentro do budget. **Limits** representam o teto teórico se todos os componentes estiverem sob carga máxima simultaneamente, o que não ocorre em operação normal.

Ajuste crítico para caber no budget:

- `memory.allowedPercent: 30` no VictoriaMetrics limita o cache TSDB a ~30Mi, evitando que o processo suba para 200Mi+

---

## 8. Observabilidade

### Stack

```
App (Python/structlog) ──stdout──────────► Promtail ──► Loki (logs)
App (Python/prometheus-client) ──/metrics──► VictoriaMetrics (métricas)
App (Python/opentelemetry-sdk) ──OTLP HTTP──► Tempo (traces)
                                                  │
                                              Grafana (dashboards + alertas)
                                             ╔══════════════════╗
                                             ║  VictoriaMetrics ║
                                             ║  Loki            ║
                                             ║  Tempo           ║
                                             ╚══════════════════╝
```

### Métricas customizadas

| Métrica | Tipo | Descrição |
|---------|------|-----------|
| `api_requests_total` | Counter | Requisições por método, endpoint e status HTTP |
| `api_errors_total` | Counter | Erros por tipo (not_found, circuit_breaker, internal_error) |
| `http_request_duration_seconds` | Histogram | Latência com buckets ajustados para SLO de 300ms p99 |
| `cache_hits_total` | Counter | Cache hits no lookup de personagens |
| `cache_misses_total` | Counter | Cache misses (indica fetch à HP API) |
| `circuit_breaker_open` | Gauge | 1 quando o circuit breaker está aberto, 0 quando fechado |

Endpoints de infra (`/health`, `/ready`, `/metrics`) são excluídos das métricas de latência para não distorcer os percentis do SLO.

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

O `correlation_id` é propagado do header `X-Request-ID` enviado pelo caller (ou gerado internamente), devolvido no response via `X-Request-ID`. O Grafana tem `derivedFields` configurado no datasource Loki para transformar o `trace_id` em link direto para o trace correspondente no Tempo.

O Promtail extrai `level`, `trace_id` e `correlation_id` como labels Loki via pipeline `json`, permitindo filtrar logs por trace sem grep manual.

### SLOs e alertas

| SLO | Objetivo | Alerta |
|-----|----------|--------|
| Disponibilidade | 99.9% das requests com status != 5xx | Taxa de erro > 0.1% por 2 minutos |
| Latência p99 | < 300ms no endpoint `/wizard/{name}` | p99 > 300ms por 5 minutos |
| Circuit Breaker | Fechado (HP API acessível) | Qualquer abertura por 1 minuto |
| Cache Hit Rate | > 50% das requests servidas do cache | Hit rate < 50% por 10 minutos |

Todos os alertas e o dashboard são provisionados como código via ConfigMaps do Helm — sem configuração manual no Grafana.

### Traces (OpenTelemetry)

O tracing é ativado pela variável de ambiente `OTEL_EXPORTER_OTLP_ENDPOINT`. Se não estiver definida, o SDK opera em modo no-op sem overhead. Em produção, o endpoint aponta para o Tempo no namespace `monitoring` via OTLP HTTP (porta 4318).

A instrumentação via `FastAPIInstrumentor` e `HTTPXClientInstrumentor` gera spans automaticamente para cada request HTTP recebido e cada chamada à HP API, permitindo visualizar end-to-end latency no Grafana.

---

## 9. Infraestrutura como Código

Toda a infraestrutura é versionada e reproduzível a partir de um único comando:

```
make all   # cria cluster k3d + instala observabilidade + faz deploy da app
```

| Camada | Ferramenta | Localização |
|--------|-----------|-------------|
| App (Deployment, Service, Ingress, HPA, PDB, NetworkPolicy) | Helm chart | `infra/helm/wizard-intelligence-network/` |
| Observabilidade (VictoriaMetrics, Grafana, Tempo, Loki, Promtail) | Helm values | `infra/observability/` |
| Datasources, dashboards e alertas Grafana | ConfigMaps via Helm | `infra/helm/.../templates/grafana-*.yaml` + `infra/observability/grafana-configmaps-monitoring.yaml` |
| CI/CD | GitHub Actions | `.github/workflows/ci.yml` |

O Grafana recebe datasources, dashboards e regras de alerta exclusivamente via ConfigMaps montados em `provisioning/` — nenhuma configuração é feita manualmente na UI.

---

## 10. CI/CD

O pipeline GitHub Actions executa na ordem:

```
lint-python ──┐
              ├──► test ──┐
lint-helm   ──┘           ├──► build (Docker)
security ─────────────────┘
```

| Job | O que valida |
|-----|-------------|
| `lint-python` | `ruff check`, `ruff format --check`, `mypy` |
| `lint-helm` | `helm lint -f values.yaml`, `helm template -f values.yaml` |
| `test` | `pytest` com cobertura mínima de 70% |
| `security` | `bandit -r src/ -ll` (SAST estático) |
| `build` | `docker build` com cache GitHub Actions |

O `helm lint` e o `helm template` são executados com `-f values.yaml` para validar os values reais — sem isso, o CI usaria defaults e poderia mascarar referências quebradas.

---

## 11. Segurança

### Container

- Executa como UID 1000 (non-root), grupo 1000
- `readOnlyRootFilesystem: true` — filesystem imutável em runtime
- `/tmp` montado como `emptyDir` para permitir arquivos temporários do Python/uvicorn
- `allowPrivilegeEscalation: false`
- `capabilities: drop: [ALL]` — zero capabilities Linux

### Rede (NetworkPolicy)

O pod da aplicação só pode se comunicar com:

| Direção | Destino | Porta |
|---------|---------|-------|
| Ingress | Namespace `kube-system` (Traefik) | TCP 8000 |
| Ingress | Namespace `monitoring` (VictoriaMetrics scrape) | TCP 8000 |
| Egress | Namespace `kube-system` (CoreDNS) | UDP/TCP 53 |
| Egress | Internet pública, exceto RFC1918 (HP API) | TCP 443 |
| Egress | Namespace `monitoring` (Tempo — OTLP) | TCP 4317, 4318 |

Todo tráfego não listado acima é bloqueado por padrão.

### Secrets

Nenhum secret é armazenado em código, ConfigMap ou repositório. O secret do Grafana (`grafana-admin-secret`) é criado via `kubectl create secret` antes do deploy — instrução documentada no `Makefile` (`make obs-install`). O arquivo `.env` está explicitamente no `.gitignore`.

### SAST

O `bandit` roda em cada PR com `-ll` (medium e high severity). A exceção `B104` (binding `0.0.0.0`) é intencional e documentada — containers precisam fazer bind em todas as interfaces para receber tráfego do Kubernetes.
