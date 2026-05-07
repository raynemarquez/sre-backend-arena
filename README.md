# Desafio Técnico - Jeitto 

# 🏆 SRE Backend Arena — Reliability Challenge
### 🔮 Cenário 1 — Wizard Intelligence Network (Harry Potter)

*"Constante Vigilância!"* — Alastor "Olho-Tonto" Moody

Este repositório foi criado como solução para o desafio proposto pelo @Kailimadev - [SRE Backend Arena](https://github.com/kailimadev/sre-backend-arena) — Cenário 1 (Harry Potter) 

Serviço HTTP de inteligência sobre bruxos do universo Harry Potter, construído como resposta ao **SRE Backend Arena** da Jeitto.

Integra com a [HP API](https://hp-api.onrender.com) e foi projetado para suportar **10.000 RPS** dentro de um budget rígido de **1.5 CPU / 350MB RAM** para a stack completa em Kubernetes local (k3d).

---

## Endpoint

```
GET /wizard/{name}
```

```json
{
  "name": "Harry Potter",
  "house": "Gryffindor",
  "species": "human",
  "wizard": true,
  "powerScore": 85
}
```

---

## Stack

| Camada | Tecnologia |
|--------|-----------|
| Runtime | Python 3.11 + FastAPI + uvicorn (uvloop + httptools) |
| Kubernetes local | k3d + Traefik |
| Empacotamento | Helm 3 |
| Métricas | VictoriaMetrics Single + prometheus-client |
| Logs | structlog (JSON) + Promtail + Loki 5.x |
| Traces | OpenTelemetry SDK + Grafana Tempo *(desabilitado por padrão)* |
| Dashboards e alertas | Grafana (provisionado via ConfigMaps) |
| CI | GitHub Actions |

---

## Pré-requisitos

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows)
- [k3d](https://k3d.io) `>= 5.x`
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm](https://helm.sh) `>= 3.12`
- [k6](https://k6.io) *(opcional — para load test)*

No PowerShell, verifique:

```powershell
docker version
k3d version
kubectl version --client
helm version
```

---

## Setup completo (do zero)

```powershell
# 1. Clone o repositório
git clone <repo-url>
cd sre-backend-arena-hp

# 2. Crie o secret do Grafana (uma única vez)
kubectl create secret generic grafana-admin-secret `
  --from-literal=admin-user=admin `
  --from-literal=admin-password=TROQUE_AQUI `
  -n monitoring --dry-run=client -o yaml | kubectl apply -f -

# 3. Sobe tudo: cluster + observabilidade + app
make all
```

O `make all` executa em sequência:
1. `cluster-create` — cria o cluster k3d `wizard-cluster` com Traefik e portas 8080/8443 mapeadas
2. `obs-install` — instala VictoriaMetrics, Grafana, Loki e Promtail via Helm
3. `deploy` — faz build da imagem, importa para o cluster e instala o Helm chart

---

## Acessos

```powershell
# App
make app-port
# → http://localhost:8000/wizard/harry%20potter

# Grafana
make grafana-port
# → http://localhost:3000  (admin / senha definida no secret)

# Recuperar senha do Grafana
make grafana-password
```

---

## Comandos úteis

```powershell
make help          # lista todos os targets disponíveis
make status        # status dos pods nas namespaces wizard e monitoring
make logs          # tail dos logs da app
make budget-check  # CPU e RAM atual dos pods (requer metrics-server)
make lint          # ruff check + format check
make test          # pytest com cobertura mínima 70%
make security      # bandit SAST
```

---

## Load test (k6)

O load test está em `tests/unit/load_test.js` e suporta quatro cenários:

```powershell
# Pré-requisito: port-forward da app
kubectl port-forward svc/wizard-intelligence-network 8000:8000 -n wizard

# Em outro terminal:
k6 run tests/unit/load_test.js                          # stress (padrão)
k6 run --env SCENARIO=smoke tests/unit/load_test.js     # sanidade
k6 run --env SCENARIO=stress tests/unit/load_test.js    # 10k RPS
k6 run --env SCENARIO=soak tests/unit/load_test.js      # estabilidade prolongada
k6 run --env SCENARIO=spike tests/unit/load_test.js     # burst repentino
```

---

## Estrutura do projeto

```
.
├── src/
│   ├── main.py                  # FastAPI app, middleware, métricas, endpoints
│   ├── models/wizard.py         # Pydantic response model
│   ├── services/hp_api.py       # Cliente HP-API, cache, retry, circuit breaker
│   └── observability/tracing.py # Setup OpenTelemetry (ativado via ENV)
├── tests/unit/
│   ├── test_main.py             # Testes dos endpoints
│   ├── test_hp_api.py           # Testes do cliente e cache
│   └── load_test.js             # Load test k6
├── infra/
│   ├── helm/wizard-intelligence-network/
│   │   ├── Chart.yaml
│   │   ├── values.yaml
│   │   ├── files/wizard-dashboard.json
│   │   └── templates/
│   │       ├── deployment.yaml
│   │       ├── hpa.yaml
│   │       ├── pdb.yaml
│   │       ├── networkpolicy.yaml
│   │       ├── ingress.yaml
│   │       ├── traefik-middleware.yaml
│   │       ├── grafana-alerts.yaml
│   │       └── grafana-dashboard.yaml
│   └── observability/
│       ├── values-vm-single.yaml
│       ├── values-grafana.yaml
│       ├── values-loki.yaml
│       ├── values-promtail.yaml
│       ├── values-tempo.yaml
│       └── grafana-configmaps-monitoring.yaml
├── .github/workflows/ci.yml
├── Dockerfile
├── Makefile
├── requirements.txt
└── pyproject.toml
```

---

## Observabilidade

### Dashboards e alertas

Todos provisionados como código via ConfigMaps montados no Grafana — nenhuma configuração manual necessária. Após o `make all`:

- **Dashboard Wizard Intelligence Network**: RPS, latência p50/p95/p99, cache hit rate, estado do circuit breaker, uso de recursos
- **Alertas SLO**:
  - Disponibilidade < 99.9% (taxa de erro > 0.1% por 2 min) → `critical`
  - Latência p99 > 300ms por 5 min → `warning`
  - Circuit breaker aberto por 1 min → `warning`
  - Cache hit rate < 50% por 10 min → `info`

### Traces (OpenTelemetry)

O tracing está implementado em `src/observability/tracing.py` mas **desabilitado por padrão** para manter a stack dentro do budget local. Para ativar:

```powershell
# Instala o Tempo
helm upgrade --install tempo grafana/tempo `
  -n monitoring `
  -f infra/observability/values-tempo.yaml `
  --wait --timeout 3m

# Habilita no values.yaml:
# otel:
#   enabled: true
#   endpoint: "http://tempo.monitoring.svc:4318"
# E adiciona ENABLE_TRACING=true nas env vars do deployment
make deploy
```

---

## Achievements declarados

| Achievement | Evidência |
|-------------|-----------|
| **Cost Whisperer** | Stack completa em 315m CPU / 335Mi RAM (requests). Documentado em ARCHITECTURE.md §7 |
| **SLO Guardian** | 4 alertas como código no `grafana-alerts.yaml` |
| **Rate Limit Guardian** | Token Bucket 10 req/s no `hp_api.py` + middleware Traefik 10k avg/20k burst |
| **IaC Wizard** | `make all` reproduz toda a stack do zero — Helm chart + observabilidade como código |
| **Trace Master** | OTEL SDK instrumentado com `FastAPIInstrumentor` + `HTTPXClientInstrumentor` → Tempo |
| **Chaos Survivor** | Stale cache fallback + circuit breaker garantem 0 erros 5xx com HP API inacessível |

---

## CI/CD

O pipeline GitHub Actions roda em todo push para `main` e `develop`:

```
lint-python ──┐
              ├──► test ──┐
lint-helm   ──┘           └──► build (Docker)
security ─────────────────────►
```

O deploy é local via `make deploy` — não está no pipeline porque o k3d não possui kubeconfig acessível no GitHub Actions. Em cloud, o step de deploy seria adicionado após o build com `helm upgrade --install`.
