# BUILD-LOG — one entry per phase

Format per phase: **built / verified / `[UNVERIFIED]`**. Newest first.

---

## P02 — Brand & identity · 2026-09-16

**Built.** `docs/P02-brand.md` (D1–D8): 12 name candidates each carrying a *measured* collision result,
with **Openout** chosen and `oddspace`/`openbet` as fallbacks; positioning sentence + three landing
messages; the mark's construction documented and its size survival measured; a full semantic colour
system in `brand/tokens.json` (+ generated `brand/tokens.css`); a 10-step type scale with numeric
display rules and a fetched webfont budget; an 18-icon set spec with the metaphor named per icon;
13 states of verbatim microcopy; the asset checklist. New tooling: `tools/colour_audit_lib.py` (one
WCAG/ΔE implementation, now shared), `tools/build-contrast-table.py`, `tools/build-tokens.mjs`,
`tools/palette-search.py`, `tools/mark-legibility.py`, `tools/name-collision-probe.py`,
`tools/p02-gate-check.py`; `brand/SIZE-RULES.md`, `brand/palette-search.json`. Vendored `brandkit.py`
state ledger used for real: `approve_palette`, `approve_typography`, `set_visual_axes` → rev 3, with
the state file kept out of the repo per `SKILLS.md`.

**Verified.** `tools/p02-gate-check.py` **69/69 exit 0**; `tools/colour-audit.py` **0 contrast
failures** across 24 dark + 24 light roles; `tools/build-contrast-table.py --check` (doc table ≡
generated); `node tools/build-tokens.mjs --check` (CSS ≡ tokens). Colour claims are computed, not
asserted: best 8-hue dark chart palette reaches worst-case ΔE*ab **11.3**; light tops out at **6**
(13.4) and fails at 7 (9.1), so the themes ship *different-length* arrays. `outcome.no` vs
`alert.critical` measures **ΔE 4.1 (dark) / 3.2 (light)** — hence the hard layout rule, recorded in
`tokens.json.rules`, that an outcome chip and a severity badge never share a row. The mark was
measured analytically (not screenshotted) at 512/128/64/40/24/**16**px: it survives to 24px and the
**gold dot is sub-pixel (Ø 1.44px) at 16px**, which is exactly why the kit's `favicon.svg` exists
(stroke 6u vs 3.5u, dot r 6u vs 4.5u, 19.4px² per chevron at 16px) — so `--pgm-mark-min-size-px: 24`
is now a rule, not folklore. `mark.svg` is byte-identical to the kit's approved asset and no logo
candidates were generated (Brand Lock `fixed`; `logo.md` forbids redesign-by-adjacent-request).
The gate was mutation-tested: 4 of 5 corruptions caught (non-hex token, sub-floor contrast, near-
duplicate chart hue, gutted bullet copy, deleted bullet) — and **one is admitted undetectable**: a
mid-sentence shortening that leaves a valid 120-char quote passes any length floor. `tools/
name-collision-probe.py` re-fetches page titles for every candidate, and the gate re-probes the
font claims live so they cannot rot.

**`[UNVERIFIED]`.** (1) **Trademark clearance for all 12 names** — no USPTO/EUIPO access from this
sandbox; `openout` DNS/CT/GitHub are clean and `openout.app` is an unrelated link-in-bio product, but
registration status is a paid search. (2) **WHOIS registrant** for `openout.*` — "no A record" is not
"unregistered"; the 195-byte `oddspace.com` response shows how thin that signal is. (3)
**`rsvg-convert` is absent**, so the skill's `logo-export` / `logo-inspect` / brandbook-PDF stages
cannot run and the logo slot was deliberately **not** approved rather than faked; real raster previews
of 16/40/512px are unverified (the numbers in this phase are computed geometry, which is stronger for
the question asked and weaker for "what it looks like"). (4) **Telegram Mini App dark-mode chrome
behaviour** at 40px in an actual chat list — needs a device. (5) `Instrument Sans Condensed` may exist
as an unhosted release somewhere; I proved it is *not on Google Fonts* and has no obvious repo, not
that it never existed.

**Corrections made to my own draft during this phase** (recorded, not hidden): `no: #D55E00` was
adopted before I checked it against `alert.critical` (ΔE 4.1 — it collides); the "8 chart colours
per theme, any canvas" premise is false and became 8/6 with per-theme arrays; the light
`brand.primary` `#2e5cff` failed AA for body text (4.05:1) and became `#1c3fe2` with
`brand.primary-text` added; `Instrument Sans Condensed` is not a real Google Fonts release, so
`Archivo Narrow` (11,776 B, fetched) replaced it and the webfont total is now measured (88.4 KB) not
estimated (118.4 KB); "tapemark" was dropped as a name because its racing etymology is false;
and `tools/colour-audit.py` had been auditing its own stale copy of the palette — it now loads
`brand/tokens.json`, which is why it could report "0 failures" while the gate disagreed.


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
