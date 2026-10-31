SERVICE_DIR := services/converter-api
VENV        := .venv
BIN         := $(VENV)/bin
CLUSTER     := devtoolkit
KIND_VERSION := $(shell awk '$$1 == "kind" {print $$2}' .tool-versions)

.DEFAULT_GOAL := help
.PHONY: help install lint format test run build validate-k8s cluster-up cluster-down deploy-local port-forward clean

help: ## List available commands
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

$(VENV):
	python3 -m venv $(VENV)

install: $(VENV) ## Install dev dependencies into .venv
	$(BIN)/pip install -r $(SERVICE_DIR)/requirements-dev.txt

lint: ## Run ruff lint and format checks
	cd $(SERVICE_DIR) && ../../$(BIN)/ruff check . && ../../$(BIN)/ruff format --check .

format: ## Auto-format code with ruff
	cd $(SERVICE_DIR) && ../../$(BIN)/ruff check --fix . && ../../$(BIN)/ruff format .

test: ## Run tests with coverage
	cd $(SERVICE_DIR) && ../../$(BIN)/pytest --cov --cov-report=term-missing --cov-fail-under=95

run: ## Run the API locally with auto-reload (http://localhost:8000/docs)
	cd $(SERVICE_DIR) && ../../$(BIN)/uvicorn main:app --reload --port 8000

build: ## Build the Docker image
	scripts/build.sh

validate-k8s: ## Validate K8s manifests with kubeconform
	kubeconform -strict -summary k8s/

cluster-up: ## Create the local kind cluster (no-op if it exists)
	@kind version | grep -q '$(KIND_VERSION) ' \
		|| echo "warning: this project uses kind $(KIND_VERSION) (.tool-versions); you have: $$(kind version)"
	@kind get clusters 2>/dev/null | grep -qx $(CLUSTER) \
		|| kind create cluster --config scripts/kind-cluster.yaml
	@kubectl config use-context kind-$(CLUSTER)

cluster-down: ## Delete the local kind cluster
	kind delete cluster --name $(CLUSTER)

deploy-local: cluster-up ## Build the image and deploy it to the local kind cluster
	scripts/deploy-local.sh

port-forward: ## Forward localhost:8000 to the in-cluster service
	kubectl -n devtoolkit port-forward svc/converter-api 8000:8000

clean: ## Remove caches and the virtualenv
	rm -rf $(VENV) .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -name .coverage -delete
