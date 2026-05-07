# 🔮 Wizard Intelligence Network

> *"Constante Vigilância!"* — Alastor "Olho-Tonto" Moody

Solução para o [SRE Backend Arena](https://github.com/kailimadev/sre-backend-arena) — **Cenário 1: Harry Potter**, proposto pela equipe da [Jeitto](https://jeitto.com.br).

API HTTP de alta performance que integra com a [HP API](https://hp-api.onrender.com) para retornar inteligência sobre bruxos, projetada para suportar **10.000 RPS** dentro de um budget rígido de **1.5 CPU / 350 MB RAM** em Kubernetes local (k3d).

---

## 📋 Índice

- [Quick Start](#-quick-start)
- [Endpoint](#-endpoint)
- [Arquitetura](#-arquitetura)
- [Stack de Observabilidade](#-stack-de-observabilidade)
- [Confiabilidade](#-confiabilidade)
- [Infrastructure as Code](#-infrastructure-as-code)
- [CI/CD](#-cicd)
- [Segurança](#-segurança)
- [Budget de Recursos](#-budget-de-recursos)
- [Desenvolvimento Local](#-desenvolvimento-local)
- [Achievements](#-achievements-declarados)
- [Checklist de Submissão](#-checklist-de-submissão)

---

## 🚀 Quick Start

### Pré-requisitos

| Ferramenta | Versão mínima | Finalidade |
|---|---|---|
| Docker | 24+ | Build e runtime de containers |
| k3d | 5+ | Cluster Kubernetes local |
| kubectl | 1.28+ | Gerenciamento do cluster |
| Helm | 3.14+ | Deploy da aplicação e observabilidade |
| make | — | Automação de tarefas |

### 1. Clone e configure

```bash
git clone https://github.com/raynemarquez/sre-backend-arena
cd sre-backend-arena-hp
```

### 2. Crie o secret do Grafana

O secret precisa existir antes do deploy. Substitua `SUA_SENHA` por uma senha de sua escolha:

**Windows (PowerShell):**
```powershell
kubectl create secret generic grafana-admin-secret `
  --from-literal=admin-user=admin `
  --from-literal=admin-password=SUA_SENHA `
  -n monitoring --dry-run=client -o yaml | kubectl apply -f -
```

**Linux / macOS:**
```bash
kubectl create secret generic grafana-admin-secret \
  --from-literal=admin-user=admin \
  --from-literal=admin-password=SUA_SENHA \
  -n monitoring --dry-run=client -o yaml | kubectl apply -f -
```

### 3. Suba a stack completa

```bash
make all
```

Este comando executa em sequência: criação do cluster k3d → instalação da stack de observabilidade → build e deploy da aplicação.

### 4. Acesse

```bash
make app-port      # API     → http://localhost:8000
make grafana-port  # Grafana → http://localhost:3000
```

```bash
# Teste o endpoint principal
curl "http://localhost:8000/wizard/harry%20potter"
```

Para recuperar a senha do Grafana depois:

**Windows (PowerShell):**
```powershell
kubectl get secret grafana-admin-secret -n monitoring `
  -o jsonpath="{.data.admin-password}" `
  | % { [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_)) }
```

**Linux / macOS:**
```bash
make grafana-password
```

---

## 📡 Endpoint

### `GET /wizard/{name}`

Retorna inteligência sobre um bruxo identificado pelo nome.

**Exemplo:**
```bash
curl http://localhost:8000/wizard/hermione%20granger
```

**Resposta:**
```json
{
  "name": "Hermione Granger",
  "house": "Gryffindor",
  "species": "human",
  "wizard": true,
  "powerScore": 100
}
```

| Campo | Tipo | Descrição |
|---|---|---|
| `name` | string | Nome completo do bruxo |
| `house` | string | Casa de Hogwarts (`"Unknown"` se não aplicável) |
| `species` | string | Espécie (`human`, `house-elf`, `goblin`, etc.) |
| `wizard` | boolean | `true` se é um bruxo praticante |
| `powerScore` | integer | Score de poder calculado (0–100) |

**Cálculo do `powerScore`:**

```
Base:           50 pts  (qualquer entidade)
+ Wizard:       20 pts  (se wizard == true)
+ House:        15 pts  (se possui casa de Hogwarts)
+ Wand:         15 pts  (se possui varinha com material/núcleo)
                ──────
Máximo:        100 pts
```

**Códigos de status:**

| Status | Quando |
|---|---|
| `200 OK` | Bruxo encontrado |
| `404 Not Found` | Nome não existe na HP API |
| `503 Service Unavailable` | Circuit breaker aberto e sem cache disponível |
| `500 Internal Server Error` | Erro inesperado |

### Endpoints de infraestrutura

| Endpoint | Descrição |
|---|---|
| `GET /health` | Liveness probe — estado do processo e circuit breaker |
| `GET /ready` | Readiness probe — verifica cache populado e circuit breaker fechado |
| `GET /metrics` | Scrape Prometheus para VictoriaMetrics |

---

## 🏗️ Arquitetura

```
Cliente
  │
  ▼
[Traefik Ingress — k3d :8080]
  Rate limiting de borda via middleware
  │
  ▼
[FastAPI + uvicorn (uvloop + httptools)]
  Middleware de observabilidade:
    · Gera/propaga X-Request-ID (correlation_id)
    · Injeta trace_id nos logs estruturados
    · Registra métricas de latência e status HTTP
  │
  ├─ 1. Cache L1 — _AsyncTTLCache (TTL 5min)
  │       Lookup O(1) por nome normalizado
  │       Hit válido  → resposta em ~0ms, sem I/O
  │       Hit stale   → mantido para fallback se API cair
  │
  ├─ 2. Index em memória (populado no warmup)
  │       Se L1 expirou, busca em dict O(1)
  │       Re-enriquece (powerScore) e repopula L1
  │
  └─ 3. HP API externa (apenas em cache miss total)
          Token Bucket (10 req/s, burst 20)
            └─ Retry via tenacity (3x, backoff 2s → 4s → 8s)
                  └─ Circuit Breaker (5 falhas → abre; reset 10s)
                        └─ Se aberto: stale cache ou HTTP 503
```

A premissa central do design é que **os dados da HP API são estáticos** — personagens não mudam entre chamadas. Isso permite cache agressivo com warmup na inicialização, tornando 99%+ das requisições respondidas inteiramente em memória, com latência na faixa de microssegundos.

Para decisões arquiteturais detalhadas, consulte [ARCHITECTURE.md](ARCHITECTURE.md).  
Para justificativas de escolha de tecnologia, consulte [DECISIONS.md](DECISIONS.md).

---

## 📊 Stack de Observabilidade

| Sinal | Coleta | Backend |
|---|---|---|
| Métricas | Scrape `/metrics` via VictoriaMetrics | VictoriaMetrics Single |
| Logs | Promtail DaemonSet → stdout dos pods | Grafana Loki |
| Traces | OTel SDK → OTLP HTTP | Grafana Tempo |

Todos os três sinais convergem no **Grafana**, com navegação entre eles: clique em qualquer log com `trace_id` para abrir o trace correspondente no Tempo.

### Métricas customizadas

| Métrica | Tipo | Descrição |
|---|---|---|
| `api_requests_total` | Counter | Requisições por método, endpoint e status HTTP |
| `api_errors_total` | Counter | Erros por tipo (`not_found`, `circuit_breaker`, `internal_error`) |
| `http_request_duration_seconds` | Histogram | Latência com buckets ajustados para o SLO de 300ms p99 |
| `cache_hits_total` | Counter | Cache hits no lookup de personagens |
| `cache_misses_total` | Counter | Cache misses (indica fetch à HP API) |
| `circuit_breaker_open` | Gauge | `1` quando aberto, `0` quando fechado |

### SLOs monitorados

| SLO | Objetivo | Alerta |
|---|---|---|
| Disponibilidade | ≥ 99.9% de requests sem 5xx | Taxa de erro > 0.1% por 2 min |
| Latência p99 | < 300ms no `/wizard/{name}` | p99 > 300ms por 5 min |
| Circuit Breaker | Fechado (HP API acessível) | Qualquer abertura por 1 min |
| Cache Hit Rate | > 50% servidas do cache | Hit rate < 50% por 10 min |

### Logs estruturados

Todos os logs são emitidos em JSON via `structlog`. Cada entrada contém `correlation_id`, `trace_id` e `otel_trace_id` — propagados do header `X-Request-ID` do caller ou gerados automaticamente.

```json
{
  "timestamp": "2026-05-07T12:00:00Z",
  "level": "info",
  "event": "cache_hit_valid",
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
  "trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "wizard": "harry potter"
}
```

---

## 🛡️ Confiabilidade

Defesa em profundidade — cada camada protege as camadas internas de serem acionadas desnecessariamente:

| Camada | Mecanismo | Detalhe |
|---|---|---|
| 1 | Cache L1 válido | TTL 5min, resposta em ~0ms |
| 2 | Index em memória | Repopula L1 sem chamada HTTP |
| 3 | Token Bucket | 10 req/s, burst 20 — throttle suave antes da API |
| 4 | Retry exponencial | 3 tentativas: 2s / 4s / 8s — só em `RequestError` |
| 5 | Circuit Breaker | 5 falhas → abre; reset após 10s |
| 6 | Stale cache | Serve dado expirado se API indisponível |
| 7 | HTTP 503 | Somente se não houver nenhum dado em cache |

---

## 🏗️ Infrastructure as Code

Toda a infraestrutura é versionada e reproduzível a partir de um único comando (`make all`).

| Camada | Ferramenta | Localização |
|---|---|---|
| App (Deployment, Service, HPA, PDB, Ingress, NetworkPolicy) | Helm chart | `infra/helm/wizard-intelligence-network/` |
| Observabilidade (VictoriaMetrics, Grafana, Tempo, Loki, Promtail) | Helm values | `infra/observability/` |
| Datasources, dashboards e alertas Grafana | ConfigMaps via Helm | `infra/helm/.../grafana-*.yaml` |

O Grafana recebe toda sua configuração via ConfigMaps montados em `provisioning/` — nenhuma configuração manual na UI. `kubectl apply` + restart do pod é suficiente para atualizar qualquer dashboard ou alerta.

---

## 🔄 CI/CD

Pipeline GitHub Actions (`.github/workflows/ci.yml`):

```
lint ──┐
       ├──► test ──► build (Docker)
security ─┘
validate-iac ─────────────────────┘
```

| Job | O que valida |
|---|---|
| `lint` | `ruff check`, `ruff format --check`, `bandit` (SAST) |
| `test` | `pytest` com cobertura mínima de 70% |
| `validate-iac` | `helm lint`, `helm template`, `terraform fmt`, `terraform validate` |
| `build` | `docker build` com cache GitHub Actions |

O `build` só roda após todos os jobs anteriores passarem. A imagem não é publicada em registry — o deploy é feito localmente via `k3d image import`.

---

## 🔒 Segurança

### Container

- Executa como UID 1000 (non-root), grupo 1000
- `readOnlyRootFilesystem: true` — filesystem imutável em runtime
- `allowPrivilegeEscalation: false`
- `capabilities: drop: [ALL]`
- `/tmp` como `emptyDir` para arquivos temporários do Python

### Rede (NetworkPolicy)

O pod da aplicação só se comunica com:

| Direção | Destino | Porta |
|---|---|---|
| Ingress | `kube-system` (Traefik) | TCP 8000 |
| Ingress | `monitoring` (VictoriaMetrics scrape) | TCP 8000 |
| Egress | `kube-system` (CoreDNS) | UDP/TCP 53 |
| Egress | Internet pública, exceto RFC1918 (HP API) | TCP 443 |
| Egress | `monitoring` (Tempo — OTLP) | TCP 4317/4318 |

### Secrets

Nenhum secret é armazenado em código, ConfigMap ou repositório. O arquivo `.env` está no `.gitignore`. Secrets são criados via `kubectl create secret` antes do deploy.

---

## 📦 Budget de Recursos

Budget do desafio para ambiente local: **1.5 CPU / 350 MB RAM** para toda a stack.

| Componente | CPU request | RAM request |
|---|---|---|
| App (até 2 réplicas via HPA) | 200m | 120 Mi |
| VictoriaMetrics | 30m | 60 Mi |
| Grafana | 25m | 60 Mi |
| Tempo | 20m | 25 Mi |
| Loki | 20m | 40 Mi |
| Promtail | 20m | 30 Mi |
| **Total** | **315m** | **335 Mi** |

Os *requests* são o que o scheduler Kubernetes efetivamente reserva — ambos dentro do budget. Os *limits* representam o teto teórico sob carga máxima simultânea, que não ocorre em operação normal.

---

## 💻 Desenvolvimento Local

### Sem Kubernetes (para iterar rapidamente)

```bash
make install    # instala dependências Python
make dev        # sobe a API em http://localhost:8000 com hot-reload
```

### Comandos úteis do Makefile

```bash
make help           # lista todos os comandos disponíveis

# Qualidade de código
make lint           # ruff lint + format check
make fmt            # formata o código
make test           # pytest com coverage
make security       # bandit SAST

# Cluster
make cluster-create # cria o cluster k3d
make cluster-delete # destroi o cluster k3d
make obs-install    # instala a stack de observabilidade
make deploy         # build + import + helm upgrade

# Utilitários
make status         # pods e HPAs de todos os namespaces
make logs           # tail dos logs da aplicação
make budget-check   # consumo de CPU/RAM vs budget
make grafana-port   # port-forward Grafana → localhost:3000
make app-port       # port-forward App → localhost:8000
```

### Variáveis de ambiente (`.env.example`)

```dotenv
# Ativa OpenTelemetry para envio de traces ao Tempo
# OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318

# Nome do serviço nos traces
OTEL_SERVICE_NAME=wizard-intelligence-network

# Nível de log
LOG_LEVEL=info
```

---

## 🏆 Achievements Declarados

| Achievement | Pontos | Como foi implementado |
|---|---|---|
| **Rate Limit Guardian** | 2 | Token Bucket client-side (10 req/s, burst 20) — nenhuma chamada excede o limite da HP API |
| **SLO Guardian** | 2 | 4 SLOs definidos como alertas Grafana via ConfigMap (disponibilidade, latência p99, circuit breaker, cache hit rate) |
| **Trace Master** | 2 | OTel SDK → Tempo via OTLP; `derivedFields` no Loki para navegação log↔trace; `trace_id` em todos os logs |
| **IaC Wizard** | 3 | Helm chart completo com templates parametrizados; observabilidade 100% como código via ConfigMaps |
| **Cost Whisperer** | 3 | HPA com limites rígidos (max 2 réplicas no budget local); VictoriaMetrics com `memory.allowedPercent: 30`; toda a stack em 315m CPU / 335Mi RAM |

---

## 📋 Checklist de Submissão

- [x] Repositório público com código completo
- [x] Infraestrutura como código (Helm chart + ConfigMaps de observabilidade)
- [x] Dockerfile multi-stage, non-root, readOnlyRootFilesystem
- [x] APM instrumentado — métricas (VictoriaMetrics), logs (Loki), traces (Tempo)
- [x] SLO + Dashboard + Alertas como código
- [x] Testes com ≥ 70% de cobertura
- [x] CI/CD pipeline (GitHub Actions)
- [x] Rate limiting client-side (Token Bucket)
- [x] Documentação de arquitetura ([ARCHITECTURE.md](ARCHITECTURE.md))
- [x] Lista de achievements declarada

---

## 📁 Estrutura do Repositório

```
sre-backend-arena-hp/
├── .github/workflows/ci.yml       # Pipeline CI/CD
├── infra/
│   ├── helm/wizard-intelligence-network/
│   │   ├── files/wizard-dashboard.json   # Dashboard Grafana como código
│   │   ├── templates/
│   │   │   ├── deployment.yaml
│   │   │   ├── grafana-alerts.yaml       # SLOs e alertas como código
│   │   │   ├── grafana-dashboard.yaml    # ConfigMap do dashboard
│   │   │   ├── hpa.yaml
│   │   │   ├── networkpolicy.yaml
│   │   │   ├── pdb.yaml
│   │   │   └── ...
│   │   └── values.yaml
│   └── observability/
│       ├── grafana-configmaps-monitoring.yaml  # Datasources como código
│       ├── values-grafana.yaml
│       ├── values-loki.yaml
│       ├── values-promtail.yaml
│       ├── values-tempo.yaml
│       └── values-vm-single.yaml
├── src/
│   ├── main.py                    # Aplicação FastAPI + middleware + métricas
│   ├── models/wizard.py           # Schema de resposta (Pydantic)
│   ├── services/hp_api.py         # Cliente HP API + cache + retry + circuit breaker
│   └── observability/tracing.py  # Setup OpenTelemetry (no-op se sem endpoint)
├── tests/unit/
│   ├── test_main.py
│   ├── test_hp_api.py
│   └── load_test.js               # Script k6 para teste de carga
├── ARCHITECTURE.md                # Fluxo detalhado e decisões técnicas
├── DECISIONS.md                   # Justificativas de escolha de tecnologia
├── Dockerfile
├── Makefile
└── requirements.txt
```


