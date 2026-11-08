SERVICES    := converter-api web
VENV        := .venv
BIN         := $(VENV)/bin
CLUSTER     := devtoolkit
KIND_VERSION := $(shell awk '$$1 == "kind" {print $$2}' .tool-versions)
TAILWIND_VERSION := $(shell sed -n -E 's/^ARG TAILWIND_VERSION=(.*)/\1/p' services/web/Dockerfile)

.DEFAULT_GOAL := help
.PHONY: help install lint format test run run-web css build validate-k8s cluster-up cluster-down deploy-local port-forward clean

help: ## List available commands
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

$(VENV):
	python3 -m venv $(VENV)

install: $(VENV) ## Install dev dependencies for every service into .venv
	$(BIN)/pip install $(foreach s,$(SERVICES),-r services/$(s)/requirements-dev.txt)

lint: ## Run ruff lint and format checks on every service
	@for s in $(SERVICES); do echo "== $$s"; (cd services/$$s && ../../$(BIN)/ruff check . && ../../$(BIN)/ruff format --check .) || exit 1; done

format: ## Auto-format every service with ruff
	@for s in $(SERVICES); do (cd services/$$s && ../../$(BIN)/ruff check --fix . && ../../$(BIN)/ruff format .) || exit 1; done

test: ## Run every service's tests with coverage
	@for s in $(SERVICES); do echo "== $$s"; (cd services/$$s && ../../$(BIN)/pytest --cov --cov-report=term-missing --cov-fail-under=95) || exit 1; done

run: ## Run the API locally with auto-reload (http://localhost:8000/docs)
	cd services/converter-api && ../../$(BIN)/uvicorn main:app --reload --port 8000

run-web: css ## Run the web UI locally with auto-reload (http://localhost:8080), expects `make run` in another terminal
	cd services/web && ../../$(BIN)/uvicorn main:app --reload --port 8080

css: ## Build services/web/static/app.css with the Tailwind CLI (downloaded to .venv/bin on first use)
	@test -x $(BIN)/tailwindcss || { \
		echo "Downloading tailwindcss $(TAILWIND_VERSION)"; \
		case "$$(uname -s)-$$(uname -m)" in \
			Linux-x86_64) asset=tailwindcss-linux-x64 ;; \
			Linux-aarch64) asset=tailwindcss-linux-arm64 ;; \
			Darwin-arm64) asset=tailwindcss-macos-arm64 ;; \
			Darwin-x86_64) asset=tailwindcss-macos-x64 ;; \
			*) echo "unsupported platform; see https://tailwindcss.com/blog/standalone-cli" >&2; exit 1 ;; \
		esac; \
		curl -sSfL -o $(BIN)/tailwindcss "https://github.com/tailwindlabs/tailwindcss/releases/download/$(TAILWIND_VERSION)/$$asset" \
			&& chmod +x $(BIN)/tailwindcss; }
	cd services/web && ../../$(BIN)/tailwindcss -i styles/input.css -o static/app.css --minify

build: ## Build every service's Docker image
	@for s in $(SERVICES); do scripts/build.sh $$s || exit 1; done

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

deploy-local: cluster-up ## Build the images and deploy everything to the local kind cluster
	scripts/deploy-local.sh

port-forward: ## Forward localhost:8080 to the in-cluster web UI
	kubectl -n devtoolkit port-forward svc/web 8080:8080

clean: ## Remove caches and the virtualenv
	rm -rf $(VENV) .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -name .coverage -delete
