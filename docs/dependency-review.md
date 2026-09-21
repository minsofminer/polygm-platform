# Dependency review

P14 D3. Every pinned dependency gets a row: what it is, who owns the decision to move it, the licence we rely on,
and the date it was last looked at. The point is not the table — it is that a dependency nobody has read for a year
is a dependency nobody can defend in an incident review, and an advisory that lands on an unowned package is the
one that ships.

Reviewed **2026-09-21**, at the pins in `requirements.txt` / `requirements-dev.txt`. `python3
tools/dependency-scan.py` enforces the mechanical half (every line pinned as `name==version`, the advisory feed
consulted when `pip-audit` is importable, base images pinned by digest in `deploy/image-digests.txt`); this file is
the other half, which is judgement.

| Package | Version | Type | Licence | Reviewed | Owner | Notes |
|---|---|---|---|---|---|---|
| fastapi | 0.141.1 | runtime | MIT | 2026-09-21 | platform | the HTTP surface. Moves only with the contract check (`tools/check-openapi.py`) re-run and green |
| starlette | 1.6.0 | runtime | BSD-3-Clause | 2026-09-21 | platform | pinned to the version fastapi 0.141.1 resolves against, not floated |
| pydantic | 2.13.4 | runtime | MIT | 2026-09-21 | platform | request/response models. A major bump is a contract change, treated as one |
| uvicorn[standard] | 0.53.0 | runtime | BSD-3-Clause | 2026-09-21 | platform | ASGI server. `[standard]` pulls uvloop/httptools — both reviewed here as part of the extra |
| PyYAML | 6.0.3 | runtime | MIT | 2026-09-21 | platform | used only for reading our own fixture/token files; every load is `safe_load` |
| httpx | 0.28.1 | runtime | BSD-3-Clause | 2026-09-21 | platform | outbound HTTP. Note: the API's *inbound* test client is starlette's, which is why the deprecation warning in CI is noise, not a pin problem |
| asyncpg | 0.30.0 | runtime | Apache-2.0 | 2026-09-21 | platform | Postgres driver, production engine only; the test suite runs the stdlib sqlite3 driver |
| redis | 5.2.1 | runtime | MIT | 2026-09-21 | platform | cache + rate-limit counters. A Redis outage must degrade to "no cache", never to "no limits" |
| py-clob-client-v2 | 1.1.0 | runtime | MIT | 2026-09-21 | trading | the venue client, **V2 only**: V1 is deliberately absent so an accidental import fails at install time. This is the package that signs orders; a version bump here is a P13-level re-test, not a routine update |
| argon2-cffi | 25.1.0 | runtime | MIT | 2026-09-21 | security | password hashing. Runtime, not a dev extra: the hash runs in the API process |
| cryptography | 50.0.1 | runtime | Apache-2.0 / BSD-3-Clause | 2026-09-21 | security | AES-GCM envelope for the DEK/KEK plane. The single most important dependency in the tree to keep current: a CVE here is a key-compromise path |
| mypy | 1.13.0 | dev | MIT | 2026-09-21 | platform | type checking in CI |
| pytest | 8.3.4 | dev | MIT | 2026-09-21 | platform | the suite |
| ruff | 0.8.4 | dev | MIT | 2026-09-21 | platform | lint **and** P14 D3's SAST engine (the `S` rule set) — so this pin is also the version the security scan is judged against |

## What is deliberately not here

* **`schemathesis`** — removed at P06 with the reason recorded in `requirements-dev.txt`: nothing imported it and
  its dependency set conflicted with the pinned FastAPI/Starlette pair, which made a fresh clone unresolvable. The
  contract check that replaced it is `tools/check-openapi.py`, which validates the served document against the
  generated schema.
* **`bandit` / `pip-audit`** — not installed here, and the scan says so rather than printing a pass: SAST runs on
  `ruff`'s `S` set (with a canary proving the rules fire), and the advisory feed is consulted *when importable*.
  In CI — where the network exists — `pip-audit` runs and its absence from this table is the reason to check the
  job, not the reason to trust the gap.
* **No transitive pins in this file.** They are resolved by pip from the direct pins; the honest statement is that
  a transitive dependency with an advisory is caught by the advisory feed, not by a row here.

## Cadence

Rows are reviewed **quarterly** and on any advisory that touches `cryptography`, `argon2-cffi`, `asyncpg` or
`py-clob-client-v2` — the four whose failure modes are money, keys or the database. Each review updates the date
above; the scan fails if a pinned requirement has no row, and an owner is named in every row, because the review
that never happens is the one with no name against it.
