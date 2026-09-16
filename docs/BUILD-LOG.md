# BUILD-LOG — one entry per phase

Format per phase: **built / verified / `[UNVERIFIED]`**. Newest first.

---

## P01 — Research & product specification · 2026-09-16

**Built.** `docs/P01-product-spec.md` (≈34 KB): wedge selection with a weighted matrix and a
recommended 5th wedge; 6-competitor teardown with provenance discipline; 20-row feature spec where
every P0 row names a live-verified data source; data model (19 required tables + `order_events`,
`usdc_flows`, ClickHouse/Postgres split, fail-closed reconciliation, negRisk PnL algorithm);
10 metrics with formulas and week-4/week-12 gates; risk-adjusted revenue model, 3 scenarios × 3
horizons, every figure recomputable. Supporting: `tools/datasource-probe.py` (26 live endpoint
assertions), `tools/p01-gate-check.py` (33 gate assertions incl. D6 arithmetic and spec↔probe
consistency), `docs/verification/P01-verification-log.md` + `P01-probe.json` / `-output.txt`.
Also corrected upstream facts into `docs/00-SHARED-CONTEXT.md` (appended, dated, nothing deleted)
and added `docs/AGENTS-BUILD.md` (protocol in force) + repo `README.md`.

**Verified.** `python3 tools/datasource-probe.py` → **26/26 exit 0**; `python3 tools/p01-gate-check.py`
→ **33/33 exit 0**. Gate mutation-tested with 9 deliberate spec corruptions (bad ARR, deleted ⚠
flags, removed table, contradicting the probe, unobserved fee type, stripped unknown marker): **9/9
caught, control passes**. Live API evidence backs every claim: Gamma caps at **100 rows** (so
`limit=` is not a page budget — pagination required; my 300-row sweep returned $59.19M, reproducing
the kit's $59.1M); `data-api /trades` is served from **Cloudflare cache** (`cf-cache-status: HIT`,
byte-identical 8 s apart) ⇒ the tape must come from the WebSocket; up/down 5-min markets are
**≤0.14% of top-100 market volume** and **0 of 300** volume-ordered events ⇒ wedge 1 rejected on
measurement, not taste; `lb-api /pnl` **404**, `/rank` **400** even with params supplied; trade rows
already carry `name/pseudonym/bio/profileImage` (no profile endpoint exists — `/profile` 404s);
`REDEEM` rows have `price==0` on **366/366** with payout in `usdcSize` on **280/366** at 1:1
$-per-share, which fixes the PnL algorithm; `feeType` is readable per market (6 values observed) so
fees need no category guesswork; CLOB unauthenticated L2 → **401**.

**`[UNVERIFIED]`.** 10 items, each with a closing step, in the log's last section. The load-bearing
three: (1) the **WebSocket message shapes were never observed** — host resolves but I did not
subscribe, and the whole wedge's latency claim rests on it; (2) **negRisk redemption exactness**
needs the adapter contract plus a test against one real resolved event; (3) **all six competitors'
onboarding step counts** — 4/6 sites return empty bodies to curl and one is behind a Vercel
checkpoint, so the D2 "steps to first trade" column is honestly empty and needs a human with a
wallet. Also open: Polygon RPC/pUSD-CTF addresses, deep `/activity` history, Kalshi API, builder
profile queries, Telegram Stars net take, infra prices, and the $2k-vs-$10k whale threshold
(0.2–1.2% of fills are ≥$1k on a 500-row sample, which is too small to set it).
