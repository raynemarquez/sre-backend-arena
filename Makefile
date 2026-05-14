.DEFAULT_GOAL := help
SHELL := /bin/bash

APP_NAME      := wizard-intelligence-network
NAMESPACE     := wizard
MONITORING_NS := monitoring
HELM_RELEASE  := wizard
IMAGE_TAG     := latest
K3D_CLUSTER   := wizard-cluster

# ─────────────────────────────────────────────
# Ajuda
# ─────────────────────────────────────────────
.PHONY: help
help: ## Mostra esta ajuda
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ─────────────────────────────────────────────
# Desenvolvimento local
# ─────────────────────────────────────────────
.PHONY: install
install: ## Instala dependências Python
	pip install -r requirements.txt

.PHONY: dev
dev: ## Sobe a app localmente (sem K8s)
	PYTHONPATH=. uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

.PHONY: lint
lint: ## Roda ruff lint + format check
	ruff check src/
	ruff format --check src/

.PHONY: fmt
fmt: ## Formata o código com ruff
	ruff format src/

.PHONY: test
test: ## Roda testes com coverage
	pytest --cov=src --cov-report=term-missing --cov-fail-under=70

.PHONY: security
security: ## Roda bandit (SAST)
	bandit -r src/ -ll --skip B104

# ─────────────────────────────────────────────
# Docker
# ─────────────────────────────────────────────
.PHONY: build
build: ## Build da imagem Docker
	docker build -t $(APP_NAME):$(IMAGE_TAG) .

.PHONY: build-push
build-push: build ## Build e importa imagem para o cluster k3d
	k3d image import $(APP_NAME):$(IMAGE_TAG) -c $(K3D_CLUSTER)

# ─────────────────────────────────────────────
# Cluster local (k3d)
# ─────────────────────────────────────────────
.PHONY: cluster-create
cluster-create: ## Cria cluster k3d local
	k3d cluster create $(K3D_CLUSTER) \
		--port "8080:80@loadbalancer" \
		--port "8443:443@loadbalancer" \
		--agents 1
	kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	kubectl create namespace $(MONITORING_NS) --dry-run=client -o yaml | kubectl apply -f -

.PHONY: cluster-delete
cluster-delete: ## Destroi o cluster k3d local
	k3d cluster delete $(K3D_CLUSTER)

# ─────────────────────────────────────────────
# Observabilidade (instala via Helm)
# ─────────────────────────────────────────────
.PHONY: obs-install_part1
obs-install_part1: ## Instala VictoriaMetrics + Tempo + Loki + Promtail
	helm repo add vm https://victoriametrics.github.io/helm-charts/ --force-update
	helm repo update

	helm upgrade --install victoria-metrics-single vm/victoria-metrics-single \
		-n $(MONITORING_NS) --create-namespace \
		-f infra/observability/values-vm-single.yaml \
		--wait --timeout 3m

#
#	helm upgrade --install tempo grafana/tempo \
#		-n $(MONITORING_NS) \
#		-f infra/observability/values-tempo.yaml \
#		--wait --timeout 3m
#
# v5.x: single binary com filesystem, sem exigir object storage
# v6+ exige S3/GCS mesmo no modo single binary
	helm upgrade --install loki grafana/loki --version 5.47.2 \
		-n $(MONITORING_NS) \
		-f infra/observability/values-loki.yaml \
		--wait --timeout 3m

	helm upgrade --install promtail grafana/promtail --version 6.16.6 \
		-n $(MONITORING_NS) \
		-f infra/observability/values-promtail.yaml \
		--wait --timeout 3m

.PHONY: obs-install_part2
obs-install_part2: ## Instala Grafana - precisa ter instalado a aplicação wizard-intelligence-network antes pois tem dependência no helm chart para criar datasource de métricas
# Crie o secret do Grafana antes de rodar este comando:
#	kubectl create secret generic grafana-admin-secret --from-literal=admin-user=admin --from-literal=admin-password=SUA-SENHA -n monitoring
	helm repo add grafana https://grafana.github.io/helm-charts --force-update
	helm repo update

	helm upgrade --install grafana grafana/grafana \
		-n $(MONITORING_NS) \
		-f infra/observability/values-grafana.yaml \
		--wait --timeout 3m

	kubectl apply -f infra/observability/grafana-configmaps-monitoring.yaml

# ─────────────────────────────────────────────
# Deploy da aplicação
# ─────────────────────────────────────────────
.PHONY: deploy
deploy: build-push ## Deploy completo da app via Helm
	kubectl apply -f infra/observability/grafana-configmaps-monitoring.yaml
	helm upgrade --install $(HELM_RELEASE) infra/helm/wizard-intelligence-network \
		-n $(NAMESPACE) --create-namespace \
		-f infra/helm/wizard-intelligence-network/values.yaml \
		--wait --timeout 3m

.PHONY: obs-configmaps
obs-configmaps: ## Aplica ConfigMaps de observabilidade no namespace monitoring
	kubectl apply -f infra/observability/grafana-configmaps-monitoring.yaml

.PHONY: undeploy
undeploy: ## Remove o deploy da app
	helm uninstall $(HELM_RELEASE) -n $(NAMESPACE)

# ─────────────────────────────────────────────
# Utilitários
# ─────────────────────────────────────────────
.PHONY: logs
logs: ## Tail dos logs da app
	kubectl logs -f -l app.kubernetes.io/name=$(APP_NAME) -n $(NAMESPACE) --all-containers

.PHONY: status
status: ## Mostra status dos pods
	@echo "=== App ==="
	kubectl get pods,svc,hpa -n $(NAMESPACE)
	@echo ""
	@echo "=== Monitoring ==="
	kubectl get pods -n $(MONITORING_NS)

.PHONY: grafana-port
grafana-port: ## Port-forward do Grafana -> localhost:3000
	kubectl port-forward svc/grafana 3000:80 -n $(MONITORING_NS)

.PHONY: grafana-password
grafana-password: ## Recupera a senha do Grafana do secret do cluster
	kubectl get secret grafana-admin-secret -n $(MONITORING_NS) \
		-o jsonpath="{.data.admin-password}" | base64 --decode
	@echo ""

.PHONY: app-port
app-port: ## Port-forward da app -> localhost:8000
	kubectl port-forward svc/$(APP_NAME) 8000:8000 -n $(NAMESPACE)

.PHONY: budget-check
budget-check: ## Verifica consumo de CPU/RAM vs budget (1.5 CPU / 350MB)
	@echo "=== Resource usage (namespace: $(NAMESPACE)) ==="
	kubectl top pods -n $(NAMESPACE)
	@echo ""
	@echo "=== Resource usage (namespace: $(MONITORING_NS)) ==="
	kubectl top pods -n $(MONITORING_NS)

.PHONY: all
all: cluster-create obs-install_part1 deploy obs-install_part2 ## Setup completo: cluster + obs + app
	@echo ""
	@echo "Stack completa rodando!"
	@echo "   App:     make app-port     -> http://localhost:8000"
	@echo "   Grafana: make grafana-port -> http://localhost:3000"
	@echo "   Senha:   make grafana-password"
