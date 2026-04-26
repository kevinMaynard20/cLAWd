# Dev commands. `make` with no args prints targets.

PY := .venv/bin/python
UV := uv

.PHONY: help
help:
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_-]+:.*?## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: install
install: ## Install Python and web deps
	$(UV) sync --extra dev
	pnpm install

.PHONY: install-optional
install-optional: ## Install optional heavy deps (marker, faster-whisper)
	$(UV) sync --extra dev --extra marker --extra audio

.PHONY: dev
dev: ## Run backend + frontend together (Ctrl-C stops both cleanly)
	@bash scripts/dev.sh

.PHONY: stop
stop: ## Hard-kill anything bound to the dev ports (8000, 3000)
	@bash scripts/stop.sh

.PHONY: restart
restart: stop dev ## stop then dev

.PHONY: api
api: ## Run only the FastAPI dev server on 127.0.0.1:8000
	PYTHONPATH=apps/api/src:apps/api .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --reload

.PHONY: web
web: ## Run only the Next.js dev server on localhost:3000
	cd apps/web && npm run dev

.PHONY: test
test: ## Run Python tests
	$(UV) run pytest

.PHONY: test-web
test-web: ## Run web tests
	pnpm --filter @lawschool/web test

.PHONY: lint
lint: ## Lint all code
	$(UV) run ruff check .
	$(UV) run black --check .
	pnpm --filter @lawschool/web lint

.PHONY: fmt
fmt: ## Format all code
	$(UV) run ruff check --fix .
	$(UV) run black .

.PHONY: typecheck
typecheck: ## Type check
	$(UV) run mypy apps/api/src
	pnpm --filter @lawschool/web typecheck

.PHONY: clean
clean: ## Remove caches and builds
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf apps/web/.next
