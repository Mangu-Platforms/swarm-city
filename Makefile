SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

.PHONY: help up down logs models health smoke dryrun licenses test lint format verify task context package

help: ## show available commands
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z_-]+:.*## / {printf "%-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

up: ## build and start the stack
	docker compose up -d --build

down: ## stop the stack
	docker compose down

logs: ## follow orchestrator logs
	docker compose logs -f orchestrator

models: ## pull models for AGENTS_PROFILE (default balanced)
	./provisioning/pull_models.sh

health: ## check API and exact-model readiness
	python3 tools/swarm.py health

smoke: ## run an end-to-end coding task and wait for the patch
	python3 tools/swarm.py task \
	  "Add an ISBN-13 validator with focused unit tests" \
	  --language python --mode build -C orchestrator/app

dryrun: ## run the smoke test without the remote finalizer
	SKIP_FINALIZE=true docker compose up -d --build orchestrator
	$(MAKE) smoke

licenses: ## verify every bundled roster against the license manifest
	python3 provisioning/check_licenses.py

test: ## run unit and integration tests
	PYTHONPATH=orchestrator python3 -m pytest -q orchestrator/tests

lint: ## run Ruff static checks
	ruff check .

format: ## format Python code with Ruff
	ruff format .

verify: ## run the complete local release gate
	./provisioning/verify_release.sh

task: ## submit TASK="..." with optional LANG=python MODE=fix
	@test -n "$(TASK)" || (echo 'usage: make task TASK="..." [LANG=python] [MODE=fix]' && exit 2)
	python3 tools/swarm.py task "$(TASK)" $(if $(LANG),--language "$(LANG)") $(if $(MODE),--mode "$(MODE)")

context: ## preview selected files for TASK="..."
	@test -n "$(TASK)" || (echo 'usage: make context TASK="..." [LANG=python]' && exit 2)
	python3 tools/swarm.py context "$(TASK)" $(if $(LANG),--language "$(LANG)")

package: ## create a verified release archive under dist/
	./provisioning/package_release.sh
