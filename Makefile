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
        p01 p02 p03 p04 p05 gate-p05 gate-p05-offline gate-p05-mutate chaos-p05 seed-rules envelope \
        p06 gate-p06 gate-p06-offline gate-p06-mutate chaos-p06 drill-p06 \
        p07 gate-p07 gate-p07-offline gate-p07-mutate drill-p07

help:
	@printf '%s\n' \
	 'make dev          docker compose up (needs Docker; see doctor)' \
	 'make dev-core     postgres + redis + api + executor-mock only — the services that exist in P04' \
	 'make test         the whole suite on the portable SQLite engine (no Docker, no network)' \
	 'make lint         dependency/rule checks that do not need a compiler: migrations, secrets, money rules' \
	 'make gate         P04 quality gate (tools/p04-gate-check.py)' \
	 'make gate-mutate  proves the gate can fail, by breaking each rule on a copy' \
	 'make check        test + lint + gate + gate-mutate: what CI runs, and the definition of done' \
	 'make doctor       report which tools this machine actually has, and what that means for the above' \
	 'make p05          P05 gate, offline half (suite + recorded live evidence + invariants)' \
	 'make gate-p05     the live 300s WebSocket outage, then the gate that reads its artifact' \
	 'make chaos-p05    the outage alone, streamed to your terminal' \
	 'make seed-rules   OWNER=demo python3 tools/p05-seed-rules.py — default alert rules' \
	 'make p06          P06 gate: 31 checks over the trading plane (wallet, venue, reconcile, limits, copy, automation, builder)' \
	 'make gate-p06     SIGKILL the executor mid-flight, record it, then run the gate on the recording' \
	 'make chaos-p06    the kills alone, streamed to your terminal' \
	 'make drill-p06    kill switch against running processes; measures the 1000 ms budget'

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
	@$(PY) tools/datasource-probe.py --check-cache; rc=$$?; \
	 if [ $$rc -eq 3 ]; then echo "[warn] venue unreachable — no claim made either way"; exit 0; fi; exit $$rc

p04:
	$(PY) tools/p04-gate-check.py

# P05 · the offline half (suite + the recorded live evidence + the schema/tooling invariants).
p05:
	$(PY) tools/p05-gate-check.py --fast

# The gate's headline claim is a live 5-minute WebSocket outage, so the target that satisfies the phase runs it
# and records it. `tee` writes the artifact the offline check reads afterwards; the pipeline's exit status is
# the harness's, not tee's, via PIPESTATUS — a `| tail` here would eat a failure, which is a lesson this repo
# has already paid for once.
gate-p05:
	@mkdir -p docs/verification
	@set -o pipefail; $(PY) tools/p05-chaos-test.py --subjects 8 --capacity 40000 2>&1 \
	   | tee docs/verification/P05-chaos-output.txt; rc=$${PIPESTATUS[0]}; \
	 if [ $$rc -ne 0 ]; then exit $$rc; fi; \
	 $(PY) tools/p05-gate-check.py --live

gate-p05-offline:   ## the gate without spending 6 minutes on the venue
	$(PY) tools/p05-gate-check.py --fast

gate-p05-mutate:    ## prove the P05 gate can fail
	$(PY) tools/p05-mutation-test.py

chaos-p05:          ## just the live outage, on a terminal where you can watch it
	$(PY) tools/p05-chaos-test.py --subjects 8

# P06 · the trading plane. `p06` is offline and fast (it reads the chaos artifact if one has been recorded,
# and says plainly that it has not if it has not); `gate-p06` is the target that satisfies the phase, because
# the phase's headline claim is a process that was SIGKILLed mid-flight.
p06:
	$(PY) tools/p06-gate-check.py

gate-p06:
	@mkdir -p docs/verification
	$(PY) tools/p06-chaos-test.py --record docs/verification/P06-chaos-output.txt
	$(PY) tools/p06-gate-check.py

gate-p06-offline:   ## the 31 checks with nothing to kill (reads the recorded artifact)
	$(PY) tools/p06-gate-check.py --fast

gate-p06-mutate:    ## prove the P06 gate can fail, one broken money rule at a time
	$(PY) tools/p06-mutation-test.py

chaos-p06:          ## just the SIGKILLs, on a terminal where you can watch the venue counters move
	$(PY) tools/p06-chaos-test.py

drill-p06:          ## engage the switch against running processes and measure who refuses when
	@mkdir -p docs/verification
	$(PY) tools/p06-drill.py --record docs/verification/P06-drill.txt

# P07 · the security plane. `p07` is the 32 checks against a booted plane, reading the drill and mutation
# artifacts that `make gate-p07` and `make gate-p07-mutate` write; `gate-p07` re-runs the 10,000-key drill first,
# because a phase whose headline is "the drill happened" should not be satisfied by a file from last month.
p07:
	$(PY) tools/p07-gate-check.py

gate-p07:
	@mkdir -p docs/verification
	$(PY) tools/p07-drill.py --record docs/verification/P07-key-drill.txt
	$(PY) tools/p07-gate-check.py

gate-p07-offline:   ## the 32 checks with the recorded drill and mutation runs
	$(PY) tools/p07-gate-check.py --fast

drill-p07:          ## revoke 10,000 keys, kill every session, throw the switch; records its own transcript
	@mkdir -p docs/verification
	$(PY) tools/p07-drill.py --record docs/verification/P07-key-drill.txt

gate-p07-mutate:    ## prove the P07 checks can fail, one inverted security rule at a time
	@mkdir -p docs/verification
	$(PY) tools/p07-mutation-test.py --record docs/verification/P07-mutation.txt

seed-rules:         ## write the default alert rules for one owner (P09 owns the UI for this table)
	$(PY) tools/p05-seed-rules.py --owner $${OWNER:-demo}

p01:
	$(PY) tools/p01-gate-check.py
p02:
	$(PY) tools/p02-gate-check.py
p03:
	$(PY) tools/p03-gate-check.py
	$(PY) tools/p03-mutation-test.py

check: test lint lint-canary openapi-selftest sql-sqlite-check gate gate-mutate p01 p02 p03 p04 p05 p06 p07 probe-fresh
	@echo "ALL GREEN"

# ------------------------------------------------------------------ diagnostics
doctor:
	@$(PY) tools/doctor.py

clean:
	rm -rf var/ **/__pycache__ .pytest_cache
	find . -name "*.pyc" -delete
