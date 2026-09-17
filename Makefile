# P04 · one entry point for everything the phase promises runs.
# `make test` and `make dev` are the two lines in the prompt's acceptance criteria; both are implemented,
# and `make dev` degrades LOUDLY (not silently) when a service's code has not landed yet — see core-only.

SHELL := /bin/bash
PY    ?= python3
ROOT  := $(shell pwd)
export PYTHONPATH := $(ROOT)/packages:$(ROOT)/services/api:$(ROOT)/services/executor-mock:$(ROOT)/tools
ENVFILE := $(ROOT)/.env

.DEFAULT_GOAL := help
.PHONY: help dev dev-core down logs test test-verbose lint typecheck migrate seed seed-sql gate \
        gate-mutate check sql-sqlite sql-sqlite-check openapi openapi-selftest clean doctor probe \
        p01 p02 p03 envelope

help:
	@printf '%s\n' \
	 'make dev          docker compose up (needs Docker; see doctor)' \
	 'make dev-core     postgres + redis + api + executor-mock only — the services that exist in P04' \
	 'make test         the whole suite on the portable SQLite engine (no Docker, no network)' \
	 'make lint         dependency/rule checks that do not need a compiler: migrations, secrets, money rules' \
	 'make gate         P04 quality gate (tools/p04-gate-check.py)' \
	 'make gate-mutate  proves the gate can fail, by breaking each rule on a copy' \
	 'make check        test + lint + gate + gate-mutate: what CI runs, and the definition of done' \
	 'make doctor       report which tools this machine actually has, and what that means for the above'

# ------------------------------------------------------------------ running
# `.env` is a real prerequisite, not a nicety: compose reads it, and a fresh clone without one used to die
# with "env file .env not found" pointing at nothing helpful.
dev: doctor .env
	@command -v docker >/dev/null 2>&1 || { echo "docker missing — use 'make test' (same code, SQLite engine)"; exit 2; }
	docker compose up --build

dev-core: doctor .env
	@command -v docker >/dev/null 2>&1 || { echo "docker missing — use 'make test'"; exit 2; }
	docker compose up --build postgres redis pgbouncer executor-mock api

down:
	docker compose down -v

logs:
	docker compose logs -f --tail=100 api executor-mock migrate

.env:
	@test -f $(ENVFILE) || { cp .env.example $(ENVFILE) && echo "created .env from .env.example — EDIT IT"; }

# ------------------------------------------------------------------ data
sql-sqlite:            ## regenerate the portable migration subset from the Postgres sources
	$(PY) tools/build-sqlite-migrations.py --write

sql-sqlite-check:      ## CI: the subset must be current, or it has drifted from the product schema
	$(PY) tools/build-sqlite-migrations.py --check

migrate:
	@if [ -n "$$PGM_DB_URL" ]; then \
	   $(PY) tools/run-sql.py --dir db/migrations --url "$$PGM_DB_URL"; \
	 else \
	   PGM_DB_PATH=$${PGM_DB_PATH:-var/polygm.db} $(PY) tools/run-sql.py --sqlite --dir db/migrations-sqlite; \
	 fi

# migrate THEN seed: seed.py inserts into tables it does not create, and the API applies the portable subset
# at import only as a dev convenience. Order here is the order a developer must use; `tools/envelope-demo.py`
# runs the same two steps for the same reason.
seed: migrate
	@PGM_DB_PATH=$${PGM_DB_PATH:-var/polygm.db} $(PY) tools/run-sql.py --file db/seed.sql --sqlite

seed-sql-apply:      ## apply the generated Postgres seed through $PGM_DB_URL
	$(PY) tools/run-sql.py --file db/seed.sql --url "$$PGM_DB_URL"

dev-data:
	docker compose --profile devdata up seed

seed-sql:      ## regenerate db/seed.sql from services/api/seed.py
	$(PY) services/api/seed.py --emit-sql

# ------------------------------------------------------------------ checks
# No `| tail` in any check below: a pipe reports the LAST command's status, which would make `make test`
# exit 0 on a red suite. This repo has been bitten by that four times, so the recipes stay one command each.
test:
	@$(PY) -m unittest discover -s tests -p "test_*.py" -q

test-verbose:
	$(PY) -m unittest discover -s tests -p "test_*.py" -v

lint: sql-sqlite-check openapi
	$(PY) tools/lint-rules.py                      # money/secret/float rules over the repo
	$(PY) tools/lint-rules.py --canary                 # and each rule must actually fire

typecheck:
	@command -v mypy >/dev/null 2>&1 && mypy packages services --ignore-missing-imports || \
	  echo "[skip] mypy not installed; CI runs it (requirements-dev.txt)"

openapi:
	$(PY) tools/check-openapi.py

openapi-selftest:      ## prove the contract checker can fail at all
	$(PY) tools/check-openapi.py --self-test

lint-canary:           ## prove every lint rule fires on a planted violation
	$(PY) tools/lint-rules.py --canary

envelope:              ## the error envelope, live: one good order, one refused, one replayed
	$(PY) tools/envelope-demo.py

gate:
	$(PY) tools/p04-gate-check.py

gate-mutate:
	$(PY) tools/p04-mutation-test.py

probe:
	$(PY) tools/datasource-probe.py

# CI runs this one; a human does not, because it takes ~40s and hits the venue. It exists so "the venue still
# behaves the way P01 measured" is a check and not a memory. If the network is blocked, it says SO.
probe-fresh:
	@$(PY) tools/datasource-probe.py --check-cache || echo "[warn] probe could not reach the venue; using the cached P01 result"

p01:
	$(PY) tools/p01-gate-check.py
p02:
	$(PY) tools/p02-gate-check.py
p03:
	$(PY) tools/p03-gate-check.py
	$(PY) tools/p03-mutation-test.py

check: test lint lint-canary openapi-selftest sql-sqlite-check gate gate-mutate p01 p02 p03 probe-fresh
	@echo "ALL GREEN"

# ------------------------------------------------------------------ diagnostics
doctor:
	@$(PY) tools/doctor.py

clean:
	rm -rf var/ **/__pycache__ .pytest_cache
	find . -name "*.pyc" -delete
