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
        p13 p13-read p13-matrix p13-chaos p13-chaos-live p13-recovery p13-load-quick p13-load p13-soak \
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

# P08 · the frontend shell. `p08` is the 15 checks, and unlike every earlier phase it cannot be satisfied by
# a recorded artefact: c11 boots the real pair (uvicorn under `next start`, one shared SQLite file) because the
# cookie flags, the CSRF hop, the refresh race and the logged-out flash are properties of a *served* response.
# So the target builds first — and `npm run measure` re-writes docs/verification/P08-bundle.txt in the same
# breath, because a budget number older than the tree it describes is a number nobody should read.
# P09 · the markets surfaces. `p09` is the 7 checks. Two of them (c3, c4) call the P08 gate's own scanners over
# this phase's files rather than reimplementing them; c7 is the phase's acceptance line, run whole. The build is
# not run here for the same reason as P08 — `make p08` builds and records the payload measurement, and c7 refuses
# a measurement older than the newest source file, so `p08` before `p09` is the order that keeps both honest.
p09: web-deps
	$(PY) tools/p09-gate-check.py --record docs/verification/P09-gate.txt

p09-offline: web-deps    ## the 6 checks that need no build artefact
	$(PY) tools/p09-gate-check.py --fast

p09-selftest: web-deps   ## prove the 7 checks can fail, one planted violation at a time
	$(PY) tools/p09-gate-check.py --self-test

p08: web-build
	$(PY) tools/p08-gate-check.py --record docs/verification/P08-gate.txt

p08-offline: web-deps        ## the 14 checks that need neither a build nor a booted server
	$(PY) tools/p08-gate-check.py --fast

p08-selftest: web-deps        ## prove the 15 checks can fail, one planted violation at a time
	$(PY) tools/p08-gate-check.py --self-test

# P10 · the terminal. `p10` is the eleven checks, and it is the first gate in this repo whose subject is a
# *journey*: c9 walks the phase's own acceptance sentence (find a whale -> open the trader -> the win rate is
# real -> the drawdown is there -> a copy config in dry-run -> the slippage risk) over the real API, in one
# script, because a chain verified one endpoint at a time is a chain nobody has ever walked. c1 runs
# check-openapi over the whole contract, so this target is also where the 37-path document is enforced, and
# c10 walks the Wallet Radar's cost control (cache, budget, the limit in the refusal, the async job, the key).
# The WEB half of D1-D9 is checked by `npm run test` and `npm run build` under `p08`/`p09`; a web-side gate
# check lands with the screens, and this comment used to promise one that did not exist - which is the same
# drift the docs phase exists to catch.
p10: web-deps
	$(PY) tools/p10-gate-check.py --record docs/verification/P10-gate.txt

p10-fast: web-deps        ## the 7 checks that need neither check-openapi nor the P08 money scanner
	$(PY) tools/p10-gate-check.py --fast

p10-selftest:             ## prove the scanners can fail, one planted violation at a time
	$(PY) tools/p10-gate-check.py --self-test

# `web-deps` is its own target because four of the 15 checks shell out to node (openapi-typescript, vitest,
# the i18n and env guards): "run the offline subset" must not quietly mean "install 400 MB first" either, so
# each target states which half it needs and the install happens once.
# P12 · the Telegram surface and the Mini App. The check that matters most here is the first one, because it is the
# one no single-side test can make: the bot mints a deep link in Python and the app parses it in TypeScript, and from
# P08 to P12 those two implementations used different grammars while both suites stayed green. This target runs the
# real pair against each other — python mints, node parses — and `p12-selftest` proves the check can fail by handing
# it the exact payload shapes that were wrong before.
p12:
	$(PY) tools/p12-gate-check.py --record docs/verification/P12-gate.txt

p12-live:                 ## also probe the two deployments over the network
	$(PY) tools/p12-gate-check.py --live --record docs/verification/P12-gate-live.txt

p12-selftest:             ## prove the P12 checks can fail, one planted breakage at a time
	$(PY) tools/p12-gate-check.py --self-test

# ------------------------------------------------------------------ P13 · the test phase
# `make p13` is the phase gate. It reads the recorded evidence AND re-runs the heavy half (the drills and the
# load suite), because P13's subject is the other gates and a gate that only reads transcripts is a gate a
# transcript can fool. `p13-read` is the same gate with `--skip-heavy`, which is what the PR path runs: it
# re-checks every recorded artifact without spending half an hour.
p13: web-deps
	$(PY) tools/p13-gate-check.py --record docs/verification/P13-gate.txt --json docs/verification/P13-gate.json

p13-read:                 ## the phase gate reading the recorded evidence (no drills, no load re-run)
	$(PY) tools/p13-gate-check.py --skip-heavy --record docs/verification/P13-gate-ci.txt

p13-matrix:               ## D2: every money-path row must resolve to a test, and that test must pass
	$(PY) tools/p13-money-matrix.py --check
	$(PY) tools/p13-money-matrix.py --run --record docs/verification/P13-money-matrix.txt --json docs/verification/P13-money-matrix.json

p13-chaos:                ## D7: the ten drills, each with a written expected outcome and its own artifact
	$(PY) tools/p13-chaos-suite.py --record docs/verification/P13-chaos-suite.txt --json docs/verification/P13-chaos-suite.json

p13-chaos-live:           ## D7 drill 2 for real: a five-minute WebSocket outage (needs network, ~7 minutes)
	$(PY) tools/p13-chaos-suite.py --only 2 --execute-live --record docs/verification/P13-chaos-suite.txt --json docs/verification/P13-chaos-suite.json

p13-recovery:             ## D7's headline loop: 100 kills between signing and the response
	$(PY) tools/p13-recovery-loop.py --runs 100 --seed 13 --record docs/verification/P13-chaos-recovery.txt --json docs/verification/P13-chaos-recovery.json

p13-load-quick:           ## D5 at CI sizes (seconds, not half-hours)
	$(PY) tools/p13-load.py --test all --quick --record docs/verification/P13-load-quick.txt --json docs/verification/P13-load-quick.json

p13-load:                 ## D5 at the kit's sizes: 30-minute soak, 2,000 books, 500 users, 10k fanout, 1k storm
	$(PY) tools/p13-load.py --test all --pace --record docs/verification/P13-load.txt --json docs/verification/P13-load.json

p13-soak:                 ## D5's 30-minute clause on its own, with the artifact the gate reads
	$(PY) tools/p13-load.py --test soak --seconds 1800 --pace --record docs/verification/P13-soak-1800s.txt --json docs/verification/P13-soak-1800s.json

web-deps:
	@cd web && { [ -d node_modules ] || npm ci --no-audit --no-fund; }

web-build: web-deps
	@cd web && npm run build && npm run measure

p01:
	$(PY) tools/p01-gate-check.py
p02:
	$(PY) tools/p02-gate-check.py
p03:
	$(PY) tools/p03-gate-check.py
	$(PY) tools/p03-mutation-test.py

# P14's six harnesses. `security` is the whole phase in one command — it is what CI runs nightly and what a
# human runs before signing the gate document. `security-gate` regenerates the document and then *checks* it, so a
# verdict that no longer matches the artifacts is a failing target rather than a stale page.
security:
	$(PY) tools/p14-authz-matrix.py
	$(PY) tools/p14-attack-surface.py
	$(PY) tools/p14-key-drills.py --scale 500
	$(PY) tools/p14-appsec-scan.py
	$(PY) tools/p14-infra-verify.py
	$(PY) tools/p14-abuse-probe.py
security-record:
	$(PY) tools/p14-authz-matrix.py --record docs/verification/P14-authz-matrix.txt --json docs/verification/P14-authz-matrix.json
	$(PY) tools/p14-attack-surface.py --record docs/verification/P14-attack-surface.txt --json docs/verification/P14-attack-surface.json
	$(PY) tools/p14-key-drills.py --scale 500 --record docs/verification/P14-key-drills.txt --json docs/verification/P14-key-drills.json
	$(PY) tools/p14-appsec-scan.py --record docs/verification/P14-appsec-scan.txt --json docs/verification/P14-appsec-scan.json
	$(PY) tools/p14-infra-verify.py --record docs/verification/P14-infra-verify.txt --json docs/verification/P14-infra-verify.json
	$(PY) tools/p14-abuse-probe.py --record docs/verification/P14-abuse-probe.txt --json docs/verification/P14-abuse-probe.json
security-gate:
	$(PY) tools/p14-security-gate.py
	$(PY) tools/p14-security-gate.py --check

check: test lint lint-canary openapi-selftest sql-sqlite-check gate gate-mutate p01 p02 p03 p04 p05 p06 p07 p08 p09 p10 p12 p12-selftest p13-read probe-fresh
	@echo "ALL GREEN"

# ------------------------------------------------------------------ diagnostics
doctor:
	@$(PY) tools/doctor.py

clean:
	rm -rf var/ **/__pycache__ .pytest_cache
	find . -name "*.pyc" -delete
