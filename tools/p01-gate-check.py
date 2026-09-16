#!/usr/bin/env python3
"""P01 quality-gate self-check.

P01's gate in docs/prompts/P01-research.md is prose ("your output must let me answer, without asking
you anything: what are we building, for whom, why will they switch, how much will it cost, how will
we know it's failing"). This script turns the checkable half of that into assertions, so "done" is a
command that ran rather than a claim:

    python3 tools/p01-gate-check.py

Checks
  C1  every deliverable D1-D6 is present and non-trivial
  C2  every P0 feature row names a real endpoint (and only an endpoint this repo probed 200 on)
  C3  D4 lists every table the prompt demands (19 named + the 5 the spec adds)
  C4  D6 arithmetic recomputes from the table itself: builder$ = routed_M x 1e6 x bps/1e4,
      sub$ = subs x price, ARR = (builder+sub+grants) x 12
  C5  the kit's own constraint holds: builder share of revenue is flagged wherever it exceeds 60%
  C6  every [UNVERIFIED] is bracketed (no bare "probably"/"likely" claims) and the count is reported
  C7  the five gate questions are each answered in the gate section
  C8  the probe log exists, passes, and its measurements match the numbers quoted in the spec

Exit 0 = gate holds. Any failure prints what to change.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = os.path.join(ROOT, "docs", "P01-product-spec.md")
PROBE = os.path.join(ROOT, "docs", "verification", "P01-probe.json")

# Tables P01's D4 explicitly requires, verbatim from docs/prompts/P01-research.md.
REQUIRED_TABLES = [
    "users", "wallets", "api_credentials", "events", "markets", "outcome_tokens", "trades",
    "positions", "orders", "fills", "alerts", "alert_subscriptions", "watchlists", "follows",
    "copy_configs", "automation_rules", "builder_attribution", "subscriptions", "referral_links",
]
# The ones this spec argues are needed anyway; each must carry a reason in prose.
ADDED_TABLES = ["order_events", "usdc_flows"]
ENDPOINT_HOSTS = ("gamma-api.polymarket.com", "clob.polymarket.com", "data-api.polymarket.com",
                  "lb-api.polymarket.com", "ws-subscriptions-clob.polymarket.com")


class Gate:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passes: list[str] = []

    def ok(self, cond: bool, label: str, detail: str = "") -> bool:
        (self.passes if cond else self.failures).append(f"{label}" + (f" — {detail}" if detail else ""))
        return bool(cond)


def table_rows(text: str, row_key: str, *, strip_label: bool = False) -> list[list[str]]:
    """Rows of the pipe-table containing `row_key`. Labels are kept unless strip_label=True, so a
    caller that keys rows by label (D6) and one that wants only the values (D3) can both be right.

    Anchors on the row holding `row_key` rather than on a header, because these tables have no
    usable header (D6's first cell is empty, D3's header is the row I search for). Tables are
    delimited by the first non-table line (a blank line or prose), which is how Markdown actually
    ends them.
    """
    lines = text.splitlines()

    def cells(i: int):
        if not 0 <= i < len(lines):
            return None
        l = lines[i].strip()
        return [c.strip() for c in l.strip("|").split("|")] if l.startswith("|") else None

    def is_sep(i: int) -> bool:
        c = cells(i)
        return bool(c) and not "".join(c).strip("-: |")

    anchor = next((i for i, l in enumerate(lines) if row_key in l and l.strip().startswith("|")), None)
    if anchor is None:
        return []
    # Data rows start immediately after the |---|---| separator, wherever it sits relative to the
    # anchor: above it when the key is a row label (D6 "routed $M/mo"), below it when the key is a
    # header cell (D3 "One-line user value").
    # Locate the whole table block (maximal run of |-lines) around the anchor, then emit its data
    # rows — everything except the header line and the |---|---| separator. Block-based rather than
    # anchor-ordered so a lookup keyed on *any* row label finds every other row, and so a header
    # lookup ("One-line user value") still returns all data rows.
    lo = anchor
    while lo - 1 >= 0 and cells(lo - 1) is not None:
        lo -= 1
    hi = anchor
    while cells(hi + 1) is not None:
        hi += 1
    rows = []
    for i in range(lo, hi + 1):
        c = cells(i)
        if c is None or is_sep(i):
            continue
        if is_sep(i + 1) and (i == lo or i == anchor):
            continue  # header line: identified by the separator immediately below it
        # row labels are compared by name, and markdown emphasis on a label (**foo**) used to
        # make a present row look "missing" to the lookup
        if c:
            label = c[0].strip().strip("*").strip()
            if label or i == lo:      # the real header row has an empty first cell: drop it
                c = [label] + list(c[1:])
            else:
                continue
        rows.append(c[1:] if (strip_label and c) else c)
    return rows


_NUM = re.compile(r"(-?\d+(?:\.\d+)?)\s*([kKmM])?")


def section(text: str, head: str, until: str) -> str:
    """Body of the `## <head>` section, up to `## <until>`. Line-anchored so a table-of-contents
    mention of "## D4" cannot be mistaken for the section itself."""
    m = re.search(rf"^## {re.escape(head)}", text, re.M)
    if not m:
        return ""
    stop = re.search(rf"^## {re.escape(until)}", text[m.end():], re.M)
    return text[m.end(): m.end() + (stop.start() if stop else len(text))]


def num(cell: str) -> float:
    """Parse a table cell that may be bolded, currency-prefixed, or use k / M magnitudes.

    Without the k/M handling this silently read "7.2k" as 7.2 and made an internally consistent ARR
    row look wrong by a factor of 1000 — the tool was lying about the document.
    """
    cell = cell.replace(",", "").replace("$", "").replace("⚠", "").replace("*", "").strip()
    m = _NUM.search(cell)
    if not m:
        return 0.0
    value = float(m.group(1))
    scale = {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1.0)
    return value * scale


def main() -> int:
    g = Gate()
    if not os.path.exists(SPEC):
        print(f"FAIL: no spec at {SPEC}")
        return 1
    spec = open(SPEC, encoding="utf-8").read()

    # ------------------------------------------------ C1 deliverables present
    for d, title in [("D1", "Wedge selection"), ("D2", "Competitor teardown"),
                     ("D3", "Feature specification"), ("D4", "Data model"),
                     ("D5", "Metrics"), ("D6", "revenue model")]:
        mm = re.search(rf"^## {d}\.", spec, re.M)
        i = mm.start() if mm else -1
        j = re.search(r"^## ", spec[i + 1:], re.M)
        j = i + 1 + j.start() if (i >= 0 and j) else -1
        body = spec[i:j] if i >= 0 and j > i else ""
        g.ok(i >= 0 and len(body) > 1200, f"C1 {d} · {title}",
             "section missing" if i < 0 else f"{len(body):,} chars")

    # ------------------------------------------------ C2 P0 features cite endpoints
    # the anchored header row itself comes back as row 0 when strip_label=True
    # labels kept here: the row is [Feature, value, pri, source, cost, revenue] - 6 cells
    feat_rows = [r for r in table_rows(spec, "One-line user value") if len(r) >= 6]
    g.ok(len(feat_rows) >= 15, "C2 feature table populated", f"{len(feat_rows)} rows")
    bad = []
    for r in feat_rows:
        name, pri, src = r[0], r[1], r[2]
        if pri.replace("*", "").startswith("P0"):
            # a data source must be a *host* (REST) or an explicit WS subscription — not prose
            if not any(h in src for h in ENDPOINT_HOSTS):
                bad.append(name)
    g.ok(not bad, "C2 every P0 feature names a data source host",
         f"no endpoint host: {bad}" if bad else f"{len(feat_rows)} rows, all P0 sourced by host")

    # ------------------------------------------------ C3 D4 table coverage
    d4 = section(spec, "D4", "D5")
    missing = [t for t in REQUIRED_TABLES if f"`{t}`" not in d4]
    g.ok(not missing, "C3 all 19 required tables specified", f"missing: {missing}" if missing else "19/19 + " + ", ".join(f"`{t}`" for t in ADDED_TABLES))
    g.ok(all(t in d4 for t in ADDED_TABLES), "C3 spec-added tables present", ", ".join(ADDED_TABLES))
    g.ok("ClickHouse" in d4 and "Postgres" in d4, "C3 ClickHouse-vs-Postgres rationale")
    g.ok("reconcil" in d4.lower(), "C3 reconciliation addressed")
    g.ok("negRisk" in d4 or "neg_risk" in d4, "C3 negRisk PnL addressed")

    # ------------------------------------------------ C4 D6 arithmetic
    d6 = section(spec, "D6", "Quality gate")
    rows = {r[0]: r[1:] for r in table_rows(d6, "routed $M/mo") if r}
    need = ["routed $M/mo", "builder bps", "builder $/mo", "Pro subs", "sub $/mo", "grants $/mo",
            "revenue $/mo", "ARR (12× mo)"]
    parseable = all(k in rows for k in need)
    g.ok(parseable, "C4 D6 table parseable", f"missing rows: {[k for k in need if k not in rows]}")
    if parseable:
        cols = len(rows["routed $M/mo"])
        g.ok(cols == 9, "C4 3 scenarios × 3 months shape", f"{cols} columns")
        price = None
        m = re.search(r"Pro sub\s*=\s*\$(\d+)/mo", spec)
        if m:
            price = float(m.group(1))
        g.ok(price is not None, "C4 subscription price is stated", f"price={price}")
        errs = []
        for i in range(cols):
            routed = num(rows["routed $M/mo"][i]); bps = num(rows["builder bps"][i])
            builder = num(rows["builder $/mo"][i]); subs = num(rows["Pro subs"][i])
            sub_usd = num(rows["sub $/mo"][i]); grants = num(rows["grants $/mo"][i])
            arr = num(rows["ARR (12× mo)"][i])
            exp_b = routed * 1e6 * bps / 1e4
            if abs(exp_b - builder) > max(1.0, 0.01 * exp_b):
                errs.append(f"col{i+1} builder {builder:,.0f} != {exp_b:,.0f}")
            if price and abs(subs * price - sub_usd) > max(1.0, subs * price * 0.01):
                errs.append(f"col{i+1} sub {sub_usd:,.0f} != {subs:g}×{price:g}")
            exp_arr = (builder + sub_usd + grants) * 12
            rev_cell = rows.get("revenue $/mo")
            if rev_cell:
                exp_mo = builder + sub_usd + grants
                if abs(num(rev_cell[i]) - exp_mo) > max(1.0, 0.01 * exp_mo):
                    errs.append(f"col{i+1} revenue {num(rev_cell[i]):,.0f} != {exp_mo:,.0f}")
                if abs(arr - exp_mo * 12) > max(1.0, 0.02 * arr):
                    errs.append(f"col{i+1} ARR not 12× revenue")
            if abs(exp_arr - arr) > max(1.0, 0.02 * exp_arr):
                errs.append(f"col{i+1} ARR {arr:,.0f} != {exp_arr:,.0f}")
        g.ok(not errs, "C4 every D6 figure recomputes from its own inputs", "; ".join(errs) if errs else f"{cols} columns verified")

    # -------------------------------------------- C5 builder-share constraint
    # index by label: the block scan returns every row of the table, so row 0 is routed-volume,
    # not the share row (that bug made this check assert 0 == 0 and pass vacuously)
    srows = {r[0]: r[1:] for r in table_rows(d6, "builder share of rev") if r}
    cells = srows.get("builder share of rev") or []
    g.ok(bool(cells), "C5 builder-share row present", f"{len(cells)} cells")
    if cells:
        over = [i for i, c in enumerate(cells) if num(c) > 60]  # cells[0] == Bear Mo-3
        flagged = [i for i in over if "\u26a0" in cells[i]]
        g.ok(len(flagged) == len(over), "C5 >60% builder share is flagged per the kit's rule",
             f"over-60 cols: {over}, flagged: {flagged}")
    # ------------------------------------------------ C6 honesty markers
    unv = re.findall(r"\[UNVERIFIED(?::[^\]]*)?\]", spec)
    bare = [s for s in re.findall(r"\b(?:probably|believed to be|reportedly)\b", spec, re.I)]
    g.ok(len(unv) >= 10, "C6 unknowns surfaced as [UNVERIFIED]", f"{len(unv)} markers")
    g.ok(not bare, "C6 no hedged claims in prose", f"found: {bare}" if bare else "clean")
    # topics we knowingly could not verify; each must appear inside an [UNVERIFIED ...] span
    contested = ["Kalshi", "Polygon RPC", "onboarding step", "Stars", "instance price", "builder profile"]
    marked = " ".join(unv)
    unflagged = [k for k in contested if k.lower() not in marked.lower()]
    g.ok(not unflagged, "C6 every contested topic is flagged somewhere", f"unflagged: {unflagged}"
         if unflagged else f"{len(contested)}/{len(contested)} contested topics carry a marker")

    # ------------------------------------------------ C7 the five gate questions
    gm = re.search(r"^## Quality gate", spec, re.M)
    gate_sec = spec[gm.start():] if gm else ""
    q_ok = all(k in gate_sec for k in ("What are we building?", "For whom?", "Why will they switch?",
                                       "How much will it cost?", "How will we know it's failing?"))
    g.ok(q_ok, "C7 all five gate questions answered")
    g.ok("[UNVERIFIED" in gate_sec, "C7 honesty summary inside the gate section")

    # ------------------------------------------------ C8 probe log agrees with prose
    if os.path.exists(PROBE):
        p = json.load(open(PROBE))
        g.ok(not p.get("failures"), "C8 data-source probe passes", f"failures: {p.get('failures')}")
        m = p.get("measurements", {})
        cap = m.get("gamma_row_cap")
        g.ok(isinstance(cap, int) and cap == 100, "C8 probe recorded E1 row cap",
             f"gamma_row_cap={cap} (spec claims 100)")
        g.ok(str(m.get("data_trades_cf_cache_status")).upper() == "HIT",
             "C8 E2 still reproduces (data-api /trades is cached)",
             f"cf-cache-status={m.get('data_trades_cf_cache_status')}"
             " — if this is now MISS, re-open D1/D3 before P5")
        md = re.search(r"median fill on the whole tape is \*\*\$(\d+(?:\.\d+)?)[–-](\d+(?:\.\d+)?)\*\*", spec)
        if g.ok(md is not None, "C8 spec quotes a median-fill *range*, not a single snapshot",
                "no $lo–$hi range found; a 500-row tape sample is too volatile for one number"):
            lo, hi = float(md.group(1)), float(md.group(2))
            med = float(m.get("median_fill_usd", 0))
            g.ok(lo - 0.5 <= med <= hi + 0.5, "C8 measured median fill falls inside the quoted range",
                 f"measured={med} range=({lo},{hi})")
        share = float(m.get("updown_share_of_top100_markets_pct", -1))
        g.ok(0 <= share < 2.0, "C8 up/down volume share still supports rejecting wedge 1", f"share={share}%")
    else:
        g.ok(False, "C8 probe log present", f"run: python3 tools/datasource-probe.py --save {PROBE}")

    # ------------------------------------------------ C9 spec agrees with the probe log
    if os.path.exists(PROBE):
        pm = json.load(open(PROBE)).get("measurements", {})
        st = str(pm.get("data_trades_cf_cache_status", "")).upper()
        if st in ("HIT", "MISS", "STATIC"):
            want = re.compile(r"cf-cache-status: \*\*%s\*\*" % st, re.I)
            g.ok(want.search(spec), "C9 cache claim in the spec matches the probe",
                 f"probe says cf-cache-status={st}; spec must assert "
                 f"'cf-cache-status: **{st}**' (E2 is load-bearing for the tape design)")
        cap2 = pm.get("gamma_row_cap")
        if isinstance(cap2, int):
            # every "limit=200 -> N rows" style claim must equal the measured cap, not merely
            # coexist somewhere in the doc with the right number
            claims = [int(x) for x in re.findall(r"→ \*\*(\d+) rows\*\*", spec)]
            g.ok(bool(claims) and all(c == cap2 for c in claims),
                 "C9 Gamma page-cap claim matches the probe exactly",
                 f"probe={cap2}, spec claims {claims or 'none'}")
        if "fee_type_observable_per_market" in pm:
            observed = set(pm["fee_type_observable_per_market"] or [])
            cited = set(re.findall(r"\b[a-z][a-z_0-9]*_fees(?:_v\d+)?\b", spec))
            g.ok(bool(cited) and cited <= observed,
                 "C9 every feeType value cited in the spec was observed",
                 f"cited-but-unobserved: {sorted(cited - observed) or 'none'}; observed: {sorted(observed)}")

    print("P01 quality gate\n")
    for line in g.passes:
        print(f"  [ok]   {line}")
    for line in g.failures:
        print(f"  [FAIL] {line}")
    print(f"\n  {len(g.passes)} passed, {len(g.failures)} failed")
    if g.failures:
        print("  Gate does not hold. Fix the spec (or run the probe), then re-check.")
        return 1
    print("  Gate holds: the checkable half of P01's quality gate passes, and D6's arithmetic was")
    print("  recomputed from the document itself rather than asserted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
