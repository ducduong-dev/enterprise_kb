.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose
UV := uv

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------------------ environment

.PHONY: install
install: ## Sync the uv workspace (all libs + services)
	$(UV) sync --all-packages

.env:
	cp .env.example .env

.PHONY: dev
dev: .env ## Bring up infrastructure (postgres, minio, temporal, keycloak)
	$(COMPOSE) up -d postgres minio minio-init temporal temporal-ui keycloak
	$(MAKE) wait-healthy
	$(MAKE) migrate
	$(MAKE) keyword-install
	@echo "Keycloak  http://localhost:8080 (admin/admin)"
	@echo "MinIO     http://localhost:9001"
	@echo "Temporal  http://localhost:8088"

.PHONY: dev-all
dev-all: dev ## Infrastructure + platform services + portal
	$(COMPOSE) --profile services up -d --build

.PHONY: observability
observability: ## Add Prometheus, Grafana and Loki
	$(COMPOSE) --profile observability up -d

.PHONY: models
models: ## Start the LiteLLM proxy — every model call goes through it (ADR-0024)
	$(COMPOSE) --profile models up -d litellm
	@echo "LiteLLM   http://localhost:4000  (config: ops/litellm/config.yaml)"

.PHONY: gpu
gpu: models ## Start the model servers behind the proxy (requires an NVIDIA runtime)
	$(COMPOSE) --profile gpu up -d

.PHONY: wait-healthy
wait-healthy: ## Block until infrastructure health checks pass
	@for i in $$(seq 1 60); do \
		if $(COMPOSE) ps --format json | grep -q '"Health":"starting"'; then sleep 2; else break; fi; \
	done
	@$(COMPOSE) ps

.PHONY: down
down: ## Stop everything (volumes preserved)
	$(COMPOSE) --profile services --profile observability --profile gpu down

.PHONY: clean
clean: ## Stop everything and delete volumes — destroys local data
	$(COMPOSE) --profile services --profile observability --profile gpu down -v

.PHONY: logs
logs: ## Tail logs (make logs SVC=retrieval-api)
	$(COMPOSE) logs -f $(SVC)

.PHONY: psql
psql: ## Open a psql shell on the platform database
	$(COMPOSE) exec postgres psql -U $${KB_DB_USER:-kb} -d $${KB_DB_NAME:-kb}

# -------------------------------------------------------------------------------- database

.PHONY: migrate
migrate: ## Apply migrations to head
	$(UV) run alembic upgrade head

.PHONY: keyword-install
keyword-install: ## Install the pg_search extension, folded columns and BM25 index (ADR-0021)
	@if command -v psql >/dev/null 2>&1; then \
		PGPASSWORD=$${KB_DB_PASSWORD:-kb} psql -h $${KB_DB_HOST:-localhost} \
			-p $${KB_DB_PORT:-5432} -U $${KB_DB_USER:-kb} -d $${KB_DB_NAME:-kb} \
			-f ops/pg_search/install.sql; \
	else \
		docker compose exec -T postgres psql -U $${KB_DB_USER:-kb} -d $${KB_DB_NAME:-kb} \
			< ops/pg_search/install.sql; \
	fi

.PHONY: keyword-maintenance
keyword-maintenance: ## Rebuild the BM25 index (run after a bulk purge — see ADR-0021)
	@if command -v psql >/dev/null 2>&1; then \
		PGPASSWORD=$${KB_DB_PASSWORD:-kb} psql -h $${KB_DB_HOST:-localhost} \
			-p $${KB_DB_PORT:-5432} -U $${KB_DB_USER:-kb} -d $${KB_DB_NAME:-kb} \
			-c "REINDEX INDEX CONCURRENTLY chunks_bm25"; \
	else \
		docker compose exec -T postgres psql -U $${KB_DB_USER:-kb} -d $${KB_DB_NAME:-kb} \
			-c "REINDEX INDEX CONCURRENTLY chunks_bm25"; \
	fi

.PHONY: downgrade
downgrade: ## Roll back one migration
	$(UV) run alembic downgrade -1

.PHONY: revision
revision: ## Autogenerate a migration (make revision M="add x")
	$(UV) run alembic revision --autogenerate -m "$(M)"

.PHONY: seed
seed: ## Load categories, fixture documents and the ACL canaries
	$(UV) run python scripts/seed.py

.PHONY: relink
relink: ## Re-derive reference edges from detected references (ADR-0028); safe to re-run
	$(UV) run python scripts/relink_references.py

.PHONY: check-drift
check-drift: ## Fail if the ORM and the migrations disagree
	$(UV) run alembic check

# ----------------------------------------------------------------------------------- checks

.PHONY: test
test: ## Unit tests (no infrastructure required)
	$(UV) run pytest -m "not integration"

.PHONY: test-all
test-all: ## Every test, including those needing live infrastructure
	$(UV) run pytest

.PHONY: acl-sweep
acl-sweep: ## The blocking ACL invariant sweep (INV-2/3/4/10)
	$(UV) run pytest -m acl_sweep -v

.PHONY: lint
lint: ## ruff check + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

.PHONY: format
format: ## Apply formatting and import sorting
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

.PHONY: typecheck
typecheck: ## mypy over libs and services
	$(UV) run mypy libs services scripts

.PHONY: invariants
invariants: ## Static enforcement of INV-1 and INV-12
	$(UV) run python scripts/check_invariants.py

.PHONY: check
check: lint typecheck invariants test ## Everything CI runs on a pull request

# ------------------------------------------------------------------------------ evaluation

.PHONY: eval
eval: ## Retrieval quality + ACL sweep against the golden set (needs `make seed`)
	$(UV) run python eval/harness/run.py --golden eval/golden_set

.PHONY: index
index: ## Drain the outbox into the keyword index once
	$(UV) run python -c "from kb_indexer.main import run_consumer; run_consumer()"

.PHONY: chat-eval
chat-eval: ## Answer faithfulness + chat red team (needs `make seed`)
	$(UV) run python eval/harness/answers.py

.PHONY: external-redteam
external-redteam: ## The public bot under pressure: disclosure and conduct (M8)
	$(UV) run python eval/harness/external.py

.PHONY: redteam
redteam: ## Run the PII red-team corpus (40 blocked + 40 clean)
	$(UV) run pytest services/pii-gate/tests/test_detector.py -q

.PHONY: scans
scans: ## Regenerate the twenty scanned fixtures and their OCR recordings (M3)
	$(UV) run python services/idp/src/kb_idp/testing/make_scans.py

.PHONY: eval-validate
eval-validate: ## Check the golden set is well-formed (runs in CI)
	$(UV) run python eval/harness/run.py --golden eval/golden_set --dry-run

.PHONY: backup
backup: ## Take a backup into ./backups (postgres + object storage + manifest)
	bash ops/backup/backup.sh ./backups

.PHONY: backup-drill
backup-drill: ## Prove the backup restores: counts, invariants, retrieval, ACL
	$(UV) run python scripts/backup_drill.py

.PHONY: dmz
dmz: ## Bring up the public surface in its own network segment (M8)
	$(MAKE) dmz-role
	$(COMPOSE) --profile dmz up -d --build
	@echo "DMZ gateway  http://localhost:8443/v1/chat/external"

.PHONY: dmz-role
dmz-role: ## Create the external-only database role and its row-level security (INV-4)
	@if command -v psql >/dev/null 2>&1; then \
		PGPASSWORD=$${KB_DB_PASSWORD:-kb} psql -h $${KB_DB_HOST:-localhost} \
			-p $${KB_DB_PORT:-5432} -U $${KB_DB_USER:-kb} -d $${KB_DB_NAME:-kb} \
			-v ON_ERROR_STOP=1 -f ops/dmz/external-role.sql; \
	else \
		$(COMPOSE) exec -T -e KB_DMZ_DB_PASSWORD postgres \
			psql -U $${KB_DB_USER:-kb} -d $${KB_DB_NAME:-kb} -v ON_ERROR_STOP=1 \
			< ops/dmz/external-role.sql; \
	fi

.PHONY: signoff
signoff: ## Export the Compliance/Legal pack for the public surface (M8)
	$(UV) run python eval/harness/external.py --json /tmp/kb-external-redteam.json || true
	$(UV) run python scripts/dmz_check.py --json /tmp/kb-dmz-check.json --skip surface,gateway,network || true
	$(UV) run python scripts/export_signoff.py --output docs/signoff \
		--redteam-json /tmp/kb-external-redteam.json --dmz-json /tmp/kb-dmz-check.json

.PHONY: dmz-check
dmz-check: ## Prove the DMZ's isolation: role, process, gateway, network
	$(UV) run python scripts/dmz_check.py

.PHONY: loadtest
loadtest: ## 50 concurrent users against the running stack (needs `make dev-all`)
	$(UV) run python ops/loadtest/run.py --concurrency 50 --duration 900

.PHONY: bakeoff
bakeoff: ## Keyword-engine bake-off (ADR-0021 decided it; re-run on the real corpus)
	$(UV) run python eval/bakeoff/run.py

.PHONY: e2e
e2e: ## Playwright end-to-end flows (upload → review → publish → search → chat)
	cd frontend/portal && npm run test:e2e
