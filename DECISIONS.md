# DECISIONS.md — Justificativas de Escolhas Técnicas

Este documento explica **por que** cada tecnologia foi escolhida, quais alternativas foram consideradas e quais trade-offs foram aceitos conscientemente. É o complemento técnico do `ARCHITECTURE.md`.

---

## 1. Runtime — Python + FastAPI + uvicorn

### Escolha

Python 3.11 com FastAPI, uvicorn rodando com `--loop uvloop --http httptools`, 1 worker por pod.

### Por que Python?

A vaga pede "pelo menos uma linguagem de programação aplicada à automação ou desenvolvimento de ferramentas", com Go, Java e Python como exemplos. Python foi escolhido por três razões concretas:

1. **Ecossistema de observabilidade maduro**: `prometheus-client`, `opentelemetry-sdk`, `structlog` e `httpx` têm manutenção ativa, boa documentação e integração nativa com FastAPI.
2. **asyncio como modelo de concorrência ideal para I/O-bound**: o sistema passa 99% do tempo em cache (sem I/O) ou esperando resposta da HP API (I/O puro). O modelo async do Python elimina overhead de thread context-switching sem a complexidade de Go channels.
3. **Velocidade de desenvolvimento e manutenibilidade**: o código fica legível para qualquer engenheiro do time, o que reduz o custo de onboarding.

### Por que FastAPI e não Flask ou Django?

| Critério | FastAPI | Flask | Django |
|---|---|---|---|
| Performance async nativa | ✅ primeira classe | ⚠️ extensões | ❌ WSGI |
| Validação automática (Pydantic) | ✅ builtin | ❌ manual | ❌ manual |
| OpenAPI automático | ✅ | ❌ | ❌ |
| Overhead de memória | baixo | baixo | alto |

FastAPI é a única opção que combina async nativo com validação de schema e tipagem — o que significa menos código boilerplate e erros de serialização capturados em tempo de inicialização, não em produção.

### Por que uvloop + httptools?

O uvloop é uma reimplementação do event loop do asyncio em Cython sobre libuv (a mesma base do Node.js). Entrega throughput 2x maior que o asyncio padrão em benchmarks de I/O. O httptools é um parser HTTP em C que substitui o parser puro-Python do uvicorn. Juntos, reduzem o overhead de parsing e scheduling — crítico quando o objetivo é 10k RPS dentro de um budget de CPU apertado.

O flag `uvloop` só funciona em Linux — no Windows o uvicorn cai para o asyncio padrão automaticamente, o que não afeta o desenvolvimento local.

### Por que 1 worker por pod?

Múltiplos workers no mesmo pod competem pelo GIL e criam contenção na memória compartilhada do cache. Com 1 worker por pod, o cache in-memory é exclusivo do processo — sem lock contention, sem invalidação cruzada. A escala horizontal é feita pelo HPA adicionando pods, não aumentando workers. Cada pod tem sua própria cópia do cache, populada no warmup. Isso é mais previsível e mais fácil de observar.

---

## 2. Métricas — VictoriaMetrics Single

### Escolha

VictoriaMetrics Single em vez de Prometheus.

### Alternativas consideradas

**Prometheus**: o padrão de mercado para métricas em Kubernetes. Seria a escolha óbvia.

**Datadog Agent**: mencionado na vaga como diferencial. Agente poderoso, mas exige API key e coleta na nuvem — incompatível com o modelo local do desafio e com a política de não armazenar secrets no repositório.

**OpenTelemetry Collector + backend**: mais flexível, mas adiciona um componente extra e complexidade de configuração.

### Por que VictoriaMetrics em vez de Prometheus?

O constraint é **350MB de RAM para toda a stack**. Esse número é o que define a escolha.

| Critério | Prometheus | VictoriaMetrics Single |
|---|---|---|
| RAM em idle (k8s pequeno) | ~120–200MB | ~40–70MB |
| Compressão de séries | gorilla (boa) | muito superior (-70% vs Prometheus) |
| Compatibilidade | nativo | 100% compatível com PromQL |
| Configuração | prometheus.yaml | mesmo formato, mais flags |
| Cardinality limits | sem proteção nativa | `memory.allowedPercent` |

VictoriaMetrics implementa um algoritmo de compressão próprio que usa significativamente menos memória que o Prometheus para o mesmo volume de séries. Com `memory.allowedPercent: 30`, o processo fica em ~60–80MB em operação normal — algo inviável com Prometheus sem configuração extensiva de retenção e compactação.

A compatibilidade com PromQL é 100% — os dashboards e alertas do Grafana usam PromQL sem adaptação. Para um avaliador, a troca é transparente.

**Por que "Single" e não o modo cluster?**

VictoriaMetrics tem um modo cluster para alta disponibilidade. Em ambiente local de avaliação, o single binary é suficiente e elimina dois componentes adicionais (vminsert + vmselect). O mesmo raciocínio se aplica ao Loki (single binary v5.x) e ao Tempo (single binary).

---

## 3. Visualização e Alertas — Grafana

### Escolha

Grafana como única interface de visualização, com datasources e dashboards provisionados como código via ConfigMaps.

### Por que não outra ferramenta?

Grafana é o padrão da indústria para observabilidade open source. Mais importante: é a única ferramenta que conecta os três datasources (VictoriaMetrics, Loki, Tempo) num único painel e permite navegação log ↔ trace com um clique. Nenhuma alternativa open source oferece essa integração pronta.

### Por que provisionamento via ConfigMaps e não via API ou UI?

O desafio avalia "Observabilidade via IaC" como bônus. Mas além do ponto de avaliação, há uma razão operacional real: qualquer dashboard criado manualmente na UI do Grafana é perdido quando o pod reinicia (sem persistência configurada no ambiente local). Provisionamento via `provisioning/` garante que o estado seja sempre o do código, não da memória do pod.

Os ConfigMaps são montados em `/etc/grafana/provisioning/` e `/var/lib/grafana/dashboards/` via `extraConfigmapMounts` no Helm values do Grafana. Isso significa que `kubectl apply -f grafana-configmaps-monitoring.yaml` + restart do pod é suficiente para atualizar qualquer dashboard ou alerta.

---

## 4. Traces — OpenTelemetry SDK + Grafana Tempo

### Escolha

OpenTelemetry SDK (OTLP HTTP) exportando para Grafana Tempo.

### Por que OpenTelemetry e não um SDK proprietário?

OpenTelemetry é o padrão CNCF para instrumentação — agnóstico de backend. O código de instrumentação não muda se o backend trocar de Tempo para Jaeger, Zipkin ou Datadog. Essa separação entre instrumentação e backend é uma decisão de arquitetura que reduz vendor lock-in.

Na prática, isso significa que se a Jeitto usa Datadog, é possível mudar apenas o `OTEL_EXPORTER_OTLP_ENDPOINT` e o exporter — sem tocar no código da aplicação.

### Por que Tempo e não Jaeger ou Zipkin?

| Critério | Jaeger | Zipkin | Grafana Tempo |
|---|---|---|---|
| Integração nativa com Grafana | via plugin | via plugin | nativa |
| Storage backend necessário | Cassandra/ES | MySQL/ES | filesystem ou S3 |
| RAM em ambiente pequeno | ~150MB+ | ~100MB+ | ~25MB |
| Correlação log ↔ trace | manual | manual | automática (derivedFields) |

Tempo é o único que roda com filesystem local sem precisar de Cassandra ou Elasticsearch, e que integra nativamente com o Grafana Explore — permitindo clicar num `trace_id` num log do Loki e abrir o trace correspondente sem nenhuma configuração adicional. Isso é o "Trace Master" achievement do desafio.

### Ativação condicional via variável de ambiente

O tracing é ativado apenas quando `OTEL_EXPORTER_OTLP_ENDPOINT` estiver definido. Se a variável estiver vazia, o SDK opera em modo no-op — sem overhead, sem conexão, sem erro. Isso permite desenvolver localmente sem Tempo rodando, e ativar automaticamente em cluster com a variável definida no `deployment.yaml`.

---

## 5. Logs — structlog + Promtail + Loki

### Escolha

`structlog` na aplicação, Promtail como agente de coleta, Loki como backend.

### Por que structlog e não o `logging` padrão do Python?

O `logging` padrão emite texto livre. `structlog` emite JSON estruturado com campos tipados por padrão. A diferença importa quando os logs chegam no Loki: com JSON estruturado, o Promtail pode extrair `trace_id` e `level` como labels indexadas — o que permite filtrar `{level="error", namespace="wizard"}` em microssegundos em vez de fazer grep linha a linha. Logs em texto livre tornam essa filtragem impossível sem um estágio de parsing custoso.

### Por que Loki e não Elasticsearch?

| Critério | Elasticsearch | Loki |
|---|---|---|
| RAM mínima | 512MB–1GB | ~40MB |
| Indexação | full-text (todos os campos) | apenas labels |
| Query | Lucene / ES Query DSL | LogQL (similar ao PromQL) |
| Integração com Grafana | via plugin | nativa |

Loki foi desenhado especificamente para funcionar junto com Prometheus/VictoriaMetrics e Grafana. Indexa apenas labels (não o conteúdo completo dos logs), o que reduz drasticamente o uso de memória e disco. Para o volume de logs de uma aplicação única, a indexação de labels é suficiente — especialmente quando os logs já são JSON estruturado.

### Por que Promtail e não Fluent Bit ou Logstash?

Promtail é o agente nativo do ecossistema Loki, desenhado para fazer exatamente uma coisa: coletar logs de pods Kubernetes e enviá-los para o Loki com re-labeling. Fluent Bit é mais flexível mas mais complexo de configurar. Logstash consome 200–400MB de RAM — inviável no budget.

O pipeline do Promtail neste projeto faz três coisas: parseia o formato CRI dos logs do Kubernetes, extrai campos JSON (`level`, `trace_id`, `correlation_id`) como labels, e enriquece com metadados do pod (`namespace`, `pod`, `container`).

---

## 6. Resiliência — pybreaker (Circuit Breaker) + tenacity (Retry)

### Escolha

`pybreaker` para circuit breaker, `tenacity` para retry com backoff exponencial.

### Por que separar circuit breaker de retry?

São mecanismos com responsabilidades distintas:

- **Retry** trata falhas **transitórias** — a API estava momentaneamente sobrecarregada, tentamos de novo.
- **Circuit Breaker** trata falhas **persistentes** — a API está completamente indisponível, para de tentar e serve o fallback imediatamente.

Usar só retry sem circuit breaker significa que, se a HP API ficar offline, cada request gera 3 tentativas com total de ~14s de espera antes de falhar — e com 10k RPS, isso significa milhares de threads bloqueadas. O circuit breaker corta isso: após 5 falhas consecutivas, abre e serve o stale cache em ~0ms sem tentar a API.

### Por que o retry só age em `RequestError` e não em `HTTPStatusError`?

`RequestError` indica falha de rede ou timeout — situações transitórias onde retry faz sentido. `HTTPStatusError` (4xx, 5xx) indica que a API respondeu mas com erro — tentar de novo imediatamente não vai mudar o resultado, só vai gerar mais carga. Essa distinção é implementada via `retry_if_exception_type(httpx.RequestError)`.

### A questão da API pública do pybreaker

`pybreaker` é uma biblioteca síncrona. Integrá-la num event loop assíncrono exige cuidado: não é possível usar `call_async()` porque ele tenta executar a coroutine de forma síncrona. A solução adotada foi notificar o circuit breaker de sucesso e falha via `circuit_breaker.call()` com funções síncronas simples — sem tocar em `_state_storage`, `_inc_counter` ou outros atributos privados que poderiam quebrar em versões futuras da biblioteca.

---

## 7. Cache — `_AsyncTTLCache` in-memory (sem Redis)

### Escolha

Cache in-memory com TTL implementado manualmente, sem Redis.

### Por que não Redis?

Redis adicionaria: ~30MB de RAM (o pod), latência de rede por request (~0.5–1ms), um ponto de falha extra, configuração de persistência e um secret de senha. Em troca, ofereceria cache compartilhado entre réplicas.

O compartilhamento entre réplicas só importa se os dados mudam frequentemente e se a inconsistência entre réplicas for um problema. Os dados da HP API **nunca mudam** — Harry Potter sempre terá os mesmos atributos. Portanto, cada réplica pode ter sua própria cópia do cache sem nenhuma inconsistência observável.

### Por que implementar o cache manualmente e não usar `cachetools` ou `aiocache`?

`cachetools` não é async-safe — usar `Lock` do threading num event loop asyncio causa deadlock. `aiocache` suporta async mas adiciona abstração desnecessária para um caso de uso simples e torna mais difícil implementar o padrão stale-while-revalidate que usamos. A implementação manual com `asyncio.Lock()` tem ~60 linhas, é completamente auditável e expõe exatamente o comportamento que queremos: TTL, maxsize com eviction LRU, hit válido vs hit stale para fallback.

---

## 8. Infraestrutura — k3d + Helm (sem Terraform)

### Escolha

k3d para Kubernetes local, Helm para empacotamento da aplicação e das ferramentas de observabilidade.

### Por que k3d e não minikube ou kind?

| Critério | minikube | kind | k3d |
|---|---|---|---|
| Backend | VM ou Docker | Docker | Docker (k3s) |
| Velocidade de criação | lento (~2min) | rápido | muito rápido (~20s) |
| Uso de RAM | alto (VM) | médio | baixo |
| LoadBalancer local | via addon | manual | nativo (Traefik) |
| Ingress | via addon | manual | nativo (Traefik) |

k3d inicia em ~20 segundos, consome menos memória que minikube e vem com Traefik como ingress controller por padrão — exatamente o que este projeto usa. O `make cluster-create` mapeia a porta 8080 do host para a porta 80 do LoadBalancer, o que é suficiente para simular o ingress de produção localmente.

### Por que Helm e não Kustomize ou manifests puros?

Helm foi escolhido por três razões. Primeira: os valores de ambiente local (k3d) e cloud (EKS/GKE) diferem — `pullPolicy: Never` vs `IfNotPresent`, `maxReplicas: 2` vs `3`, endpoint OTEL diferente. Helm permite `values.yaml` por ambiente sem duplicar manifests. Segunda: as ferramentas de observabilidade (VictoriaMetrics, Grafana, Loki, Tempo, Promtail) são distribuídas como Helm charts oficiais — usar Helm mantém consistência de ferramenta em toda a stack. Terceira: `helm lint` e `helm template` integram naturalmente no CI sem ferramentas adicionais.

### Por que não Terraform?

O desafio menciona "remote state gerenciado" como bônus de IaC (+3 pontos). Terraform seria a ferramenta natural para isso em cloud, gerenciando recursos AWS/GCP com state no S3. No ambiente local com k3d, Terraform adicionaria complexidade sem benefício real — o cluster é efêmero e não há recursos de cloud para provisionar. A decisão consciente foi priorizar a completude e corretude da solução Helm em vez de adicionar Terraform superficialmente só para marcar o bônus.

---

## 9. Rate Limiting — Token Bucket assíncrono (sem biblioteca externa)

### Escolha

Token Bucket implementado manualmente com `asyncio.Lock()`.

### Por que não `aiolimiter` ou `ratelimit`?

As bibliotecas disponíveis têm dois problemas para este caso de uso: ou não são async-safe, ou não implementam burst de forma configurável. O token bucket em ~40 linhas de Python resolve exatamente o problema: permite um burst inicial de 20 requisições (útil no warmup) e sustenta 10 req/s depois, sem nenhuma dependência extra.

### Por que "bloquear e aguardar" em vez de "rejeitar e retornar erro"?

Rejeitar (429 Too Many Requests para a HP API interna) significaria perder a chamada e exigir que o caller tente de novo — o que, sob carga, geraria um burst de retries simultâneos. Bloquear (sleep até o próximo token) cria backpressure natural: as chamadas se serializam, a HP API nunca recebe mais do que 10 req/s, e nenhuma informação é perdida. O rate limiter é uma fila, não uma porta.

---

## 10. Segurança — NetworkPolicy restritiva

### Escolha

NetworkPolicy que permite ingress apenas de `kube-system` (Traefik) e `monitoring` (VictoriaMetrics), e egress apenas para DNS, HP API (porta 443) e Tempo (porta 4318).

### Por que essa granularidade?

A abordagem anterior permitia ingress de qualquer namespace (`namespaceSelector: {}`). Isso significa que qualquer pod comprometido no cluster poderia fazer requisições diretas à aplicação, bypassando o Traefik e seus rate limits. A versão atual restringe ingress apenas ao namespace `kube-system` onde o Traefik roda — garantindo que **todo tráfego de negócio passa pelo ingress controller**.

A regra de egress exclui RFC1918 (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) na saída para internet, o que bloqueia tentativas de SSRF (Server-Side Request Forgery) para endereços internos do cluster ou da rede do host. A aplicação só pode alcançar o que explicitamente precisa: DNS, HP API e Tempo.
