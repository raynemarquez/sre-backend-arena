# ARCHITECTURE.md — Wizard Intelligence Network

## 1. Visão Geral

API HTTP que retorna inteligência sobre bruxos do universo Harry Potter, integrando com a [HP API](https://hp-api.onrender.com). Projetada para suportar **10.000 RPS** dentro de um budget rígido de **1.5 CPU / 350MB RAM** (stack completa no K8s local).

---

## 2. Fluxo de uma Requisição

```
GET /wizard/{name}
      │
      ▼
[Traefik Ingress]
  Rate limit de borda (traefik-middleware)
      │
      ▼
[FastAPI + uvicorn (uvloop + httptools)]
  Middleware de observabilidade: trace_id, métricas, latência
      │
      ├─ 1. Cache L1: _AsyncTTLCache in-memory
      │       Lookup por nome normalizado (lowercase/strip)
      │       Hit válido → retorna em ~0ms
      │       Hit stale → usado como fallback se API falhar
      │
      ├─ 2. Cache L2: _index (dict pré-carregado no warmup)
      │       Se L1 expirou mas o index tem o dado,
      │       reenriquece e repopula L1
      │
      └─ 3. HP API externa (apenas em cache miss total)
              Token Bucket (10 RPS, burst 20)
                └─ Retry tenacity (3x, exponential backoff 2s→10s)
                      └─ Circuit Breaker pybreaker (5 falhas → abre, reset 10s)
                            └─ Se aberto: stale cache ou HTTP 503
```

### Por que cache in-memory em vez de Redis?

Os dados da HP API são **praticamente estáticos** (personagens não mudam). O cache in-memory oferece latência ~0ms vs ~1ms do Redis e elimina um deployment inteiro do cluster, liberando ~30MB para a observabilidade. A desvantagem (cache não compartilhado entre réplicas) é aceitável porque:

1. O warmup na inicialização garante que toda réplica começa com o cache populado.
2. Com dados estáticos, um cache miss eventual em uma réplica gera no máximo 1 chamada à HP API para repopular — e o rate limiter garante que isso não cause burst.

Se o sistema escalasse para dezenas de réplicas com dados dinâmicos, a evolução natural seria adicionar Redis.

---

## 3. Estratégia de Rate Limit

A HP API não publica um limite oficial, sendo um serviço público free tier. Adotamos conservadorismo:

- **Token Bucket**: 10 tokens/s, burst de 20
- Implementado com `asyncio.Lock()` para ser thread-safe no event loop
- O rate limiter **bloqueia** (aguarda token disponível) em vez de rejeitar — isso garante que nenhuma request para a API externa seja perdida, apenas atrasada
- O cache é a principal defesa: em condições normais, após o warmup, **zero chamadas** chegam à HP API

---

## 4. Resiliência — Camadas de Defesa

| Camada | Mecanismo | Comportamento em falha |
|---|---|---|
| 1ª | Cache L1 válido | Retorna imediatamente |
| 2ª | Cache L2 (index) | Re-enriquece e retorna |
| 3ª | Rate limiter | Throttle suave antes de chamar API |
| 4ª | Retry com backoff | 3 tentativas, 2s/4s/8s |
| 5ª | Circuit Breaker | Para de chamar API após 5 falhas |
| 6ª | Stale fallback | Serve dado expirado se API indisponível |
| 7ª | HTTP 503 | Apenas se não houver nenhum dado em cache |

---

## 5. Cálculo do powerScore

A HP API não fornece um campo de "poder". O score é calculado deterministicamente a partir de atributos reais do personagem:

```
powerScore = 50 (base)
           + 20 (se wizard == true)
           + 15 (se house != "")
           + 15 (se wand.wood != "" ou wand.core != "")
           = máximo 100
```

**Justificativa**: bruxos com varinhas e casa definida têm treinamento formal documentado. O score é determinístico — a mesma entrada sempre produz o mesmo resultado, o que é essencial para consistência entre réplicas sem cache compartilhado.

---

## 6. Budget de Recursos

| Componente | CPU request | CPU limit | RAM request | RAM limit |
|---|---|---|---|---|
| App (máx 2 réplicas) | 2 × 100m = 200m | 2 × 500m = 1000m | 2 × 128Mi = 256Mi | 2 × 180Mi = 360Mi |
| VictoriaMetrics | 50m | 200m | 60Mi | 150Mi |
| Grafana | 25m | 100m | 70Mi | 150Mi |
| Tempo | 20m | 100m | 25Mi | 60Mi |
| Loki | 20m | 50m | 40Mi | 60Mi |
| **TOTAL requests** | **315m** | — | **451Mi** | — |

O HPA está configurado com `maxReplicas: 3` e `targetCPU: 60%`. Em 3 réplicas + observabilidade completa, o consumo estimado é ~1.0 CPU / 330MB — dentro do budget de 1.5 CPU / 350MB.

O `memory.allowedPercent: 30` no VictoriaMetrics é crítico para manter o consumo real dentro do limit.

---

## 7. Observabilidade

### Stack

```
App (Python) ──OTLP HTTP──► OTel SDK ──► Tempo (traces)
App (Python) ──/metrics──► VictoriaMetrics (métricas)
App (Python) ──stdout──► [coleta futura via Promtail/Loki]
```

### Correlação log ↔ trace

Todo log gerado pela aplicação contém `trace_id` — o mesmo ID presente nos spans do OpenTelemetry. O Grafana está configurado com `derivedFields` no datasource Loki para transformar o `trace_id` em link direto para o Tempo.

### SLOs monitorados

| SLO | Alvo | Alerta |
|---|---|---|
| Availability | 99.9% requests com status != 5xx | > 0.1% erros por 2min |
| Latência p99 | < 300ms | > 300ms por 5min |
| Circuit Breaker | Fechado | Qualquer abertura por 1min |
| Cache Hit Rate | > 50% | < 50% por 10min |

---

## 8. Infraestrutura como Código

Toda a infraestrutura é versionada e reproduzível:

- **App**: Helm chart em `infra/helm/wizard-intelligence-network/`
- **Observabilidade**: Helm values em `infra/observability/`
- **ConfigMaps**: Datasources, dashboards e alertas Grafana em `infra/observability/grafana-configmaps-monitoring.yaml`
- **CI/CD**: GitHub Actions em `.github/workflows/ci.yml`

O comando `make all` reproduz o ambiente completo do zero em qualquer máquina com k3d instalado.

---

## 9. Segurança

- Container executa como UID 1000 (non-root) com `readOnlyRootFilesystem`
- `/tmp` montado como `emptyDir` para permitir arquivos temporários do Python/uvicorn
- `allowPrivilegeEscalation: false` e `capabilities: drop: [ALL]`
- NetworkPolicy restringe egress: apenas DNS (53), HP API (443) e namespace monitoring (OTLP 4317/4318)
- Nenhum secret em código, ConfigMap ou repositório — apenas em `kubectl create secret`
- CI inclui análise estática de segurança com `bandit`
