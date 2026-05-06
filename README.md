# Desafio Técnico - Jeitto 

# 🏆 SRE Backend Arena — Reliability Challenge
### 🔮 Cenário 1 — Wizard Intelligence Network (Harry Potter)

*"Constante Vigilância!"* — Alastor "Olho-Tonto" Moody

Este repositório foi criado como solução para o desafio proposto pelo @Kailimadev - [SRE Backend Arena](https://github.com/kailimadev/sre-backend-arena) — Cenário 1 (Harry Potter) 

API HTTP de alta performance que integra com a [HP API](https://hp-api.onrender.com) para retornar inteligência sobre bruxos, suportando **10.000 RPS** com **budget de 1.5 CPU / 350MB RAM**.

---

## 🚀 Quick Start (Local)

### Pré-requisitos

- Docker, k3d, kubectl, helm, make

### 1. Crie o secret do Grafana

Antes do primeiro deploy, crie o secret com a senha de sua escolha:

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

Se precisar recuperar a senha depois:

**Windows (PowerShell):**
```powershell
kubectl get secret grafana-admin-secret -n monitoring `
  -o jsonpath="{.data.admin-password}" `
  | % { [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_)) }
```

**Linux / macOS:**
```bash
kubectl get secret grafana-admin-secret -n monitoring \
  -o jsonpath="{.data.admin-password}" | base64 --decode
```

### 2. Suba a stack completa

```bash
make all
```

### 3. Acesse

```bash
make app-port        # API     → http://localhost:8000
make grafana-port    # Grafana → http://localhost:3000 (admin / sua senha)
```

```bash
curl http://localhost:8000/wizard/harry%20potter
```

---

## 📡 Endpoint

```
GET /wizard/{name}
```

**Resposta:**

```json
{
  "name": "Harry Potter",
  "house": "Gryffindor",
  "species": "human",
  "wizard": true,
  "powerScore": 100
}
```

Outros endpoints: `GET /health` e `GET /metrics`

---

## 🏗️ Arquitetura

```
Request → [Traefik] → [FastAPI/uvicorn+uvloop]
                           │
                     Cache L1 (TTL in-memory)
                           │ miss
                     Cache L2 (index warmup)
                           │ miss
                     HP API externa
                     (TokenBucket → Retry → CircuitBreaker)
                           │ falha
                     Stale cache fallback
```

Ver [ARCHITECTURE.md](ARCHITECTURE.md) para decisões detalhadas.

---

## 📊 Observabilidade

Stack: **VictoriaMetrics** + **Loki** + **Tempo** + **Promtail** + **Grafana**

| Sinal    | Coleta               | Backend         |
|----------|----------------------|-----------------|
| Métricas | `/metrics` scrape    | VictoriaMetrics |
| Logs     | Promtail (DaemonSet) | Loki            |
| Traces   | OTel OTLP            | Tempo           |

Logs e traces correlacionados via `trace_id` — clique em qualquer log no Grafana para abrir o trace correspondente no Tempo.

---

## 🛡️ Confiabilidade

| Prática           | Implementação                              |
|-------------------|--------------------------------------------|
| Cache in-memory   | `_AsyncTTLCache` TTL 5min + stale fallback |
| Retry             | tenacity — 3x, backoff exponencial 2s→10s  |
| Timeout           | httpx — connect 2s, read 5s               |
| Circuit Breaker   | pybreaker — abre em 5 falhas, reset 10s   |
| Rate Limit client | Token Bucket — 10 RPS, burst 20           |

---

## 🏆 Achievements Declarados

| Achievement         | Como                                     |
|---------------------|------------------------------------------|
| Rate Limit Guardian | Token Bucket client-side                 |
| IaC Wizard          | Helm chart completo                      |
| Trace Master        | OTel → Tempo, correlação log↔trace       |
| SLO Guardian        | SLO availability + latência como alertas |
| Cost Whisperer      | HPA com limites rígidos no budget        |

---

## 🔒 Segurança

- Container non-root (UID 1000), `readOnlyRootFilesystem`
- `allowPrivilegeEscalation: false`, capabilities `drop: ALL`
- NetworkPolicy restringe egress para HP-API + monitoring
- Secrets via `kubectl create secret` — nunca em código ou ConfigMap

---

## 📋 Checklist

- [x] Repositório público com código completo
- [x] Helm chart (IaC)
- [x] Dockerfile multi-stage, non-root
- [x] VictoriaMetrics + Grafana + Loki + Tempo + Promtail
- [x] Métricas customizadas + logs estruturados + traces
- [x] SLO + Dashboard + Alertas como código
- [x] Testes ≥ 70% coverage
- [x] CI/CD (GitHub Actions)
- [x] Rate limiting client-side
- [x] Documentação de arquitetura
