SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

COMPOSE := docker compose
s ?=

.PHONY: help init check models models-extra up down restart status logs bootstrap \
        ingest library library-remove tune test-ui test backup restore update \
        reset-config trust lint test-memory test-normalizer

help: ## List targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

init: ## Create .env and generate secrets (ADMIN_EMAIL=… optional)
	@ADMIN_EMAIL="$(ADMIN_EMAIL)" ./scripts/init-env.sh

check: ## Host checks (Docker, GPU, RAM, memory modules, disk, ports)
	@./scripts/check-host.sh

models: ## Download/verify the required GGUF files (asks first)
	@./scripts/fetch-models.sh required

models-extra: ## Download optional A/B candidates for `make tune` (asks first)
	@./scripts/fetch-models.sh extra

up: .env ## Start the stack
	$(COMPOSE) up -d --wait

down: ## Stop the stack
	$(COMPOSE) down

restart: ## Restart the stack
	$(COMPOSE) restart

status: ## Health, VRAM/RAM usage, last generation speed
	@./scripts/status.sh

logs: ## Follow logs (s=<service>)
	$(COMPOSE) logs -f --tail=200 $(s)

bootstrap: ## Provision Open WebUI (§9)
	$(COMPOSE) --profile tools run --rm --build bootstrap

ingest: ## Convert and upload books from library/inbox (§8)
	@./scripts/ingest.sh

library: ## List ingested books
	$(COMPOSE) --profile ingest run --rm --build ingest list

library-remove: ## Remove a book (BOOK=<slug>)
	@test -n "$(BOOK)" || { echo "usage: make library-remove BOOK=<slug>"; exit 1; }
	$(COMPOSE) --profile ingest run --rm --build ingest remove "$(BOOK)"

tune: ## Measure and choose the fastest llama.cpp settings (§13)
	@./scripts/tune.sh $(ARGS)

test-memory: ## leo-memory unit + DB tests (throwaway database)
	$(COMPOSE) --profile test run --rm --build memory-test; rc=$$?; \
	$(COMPOSE) --profile test rm -sf memory-test-db >/dev/null 2>&1; exit $$rc

test-normalizer: ## LaTeX normalizer unit + property tests
	$(COMPOSE) --profile tools run --rm --build -v "$(CURDIR):/repo:ro" -w /repo ui-test \
	  pytest -q -p no:cacheprovider services/openwebui/tests

test-ui: ## Playwright tests: LaTeX render matrix, live math, basic UI
	$(COMPOSE) --profile tools run --rm --build ui-test

test: ## Unit tests + test-ui + end-to-end smoke tests
	@./scripts/smoke-test.sh

backup: ## Back up databases, Open WebUI data, manifest and .env (keeps 14)
	@./scripts/backup.sh

restore: ## Restore a backup (TS=<timestamp>, asks first)
	@test -n "$(TS)" || { echo "usage: make restore TS=<timestamp>"; ls backups 2>/dev/null; exit 1; }
	@./scripts/restore.sh "$(TS)"

update: ## Back up, pull pinned images, recreate, re-provision, run tests
	@./scripts/backup.sh
	$(COMPOSE) --profile tools --profile ingest pull --ignore-buildable
	$(COMPOSE) up -d --wait --build
	@$(MAKE) bootstrap
	@$(MAKE) test

reset-config: ## Restart Open WebUI once with RESET_CONFIG_ON_START=true
	RESET_CONFIG_ON_START=true $(COMPOSE) up -d --wait --force-recreate open-webui
	$(COMPOSE) up -d --wait --force-recreate open-webui

trust: ## Export the Caddy local root CA to ./leo-root-ca.crt
	@./scripts/trust.sh

lint: ## shellcheck + ruff (run in containers)
	docker run --rm -v "$(CURDIR):/mnt" -w /mnt koalaman/shellcheck:v0.11.0 -x scripts/*.sh services/llm/*.sh services/postgres/init/*.sh
	docker run --rm -u $$(id -u):$$(id -g) -e RUFF_NO_CACHE=true -v "$(CURDIR):/mnt" -w /mnt ghcr.io/astral-sh/ruff:0.16.9 check .

.env:
	@echo ".env missing — run 'make init' first" >&2; exit 1
