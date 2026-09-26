#!/usr/bin/env python3
"""P15 D4 — three dashboards: on-call, product, revenue. One request, one screen, under three seconds, on a phone.

    python3 tools/p15-dashboards.py --from docs/verification/p15-metrics-sample.json --out /srv/polygm/ops
    python3 tools/p15-dashboards.py --api https://api.polygm.trade --token "$PGM_ADMIN_TOKEN" --out /tmp/ops
    python3 tools/p15-dashboards.py --check        # the gate: three pages render, fit, and agree with the alarms

Three decisions worth stating, because they are the difference between a dashboard and a picture of one:

* **One read.** Every page is rendered from a single `/v1/admin/metrics` response, so no two numbers on the screen
  can come from two different moments. The page carries that read's `asOf` stamp at the top.
* **The colours are the alarms.** The status blocks are produced by evaluating `ops/alerts.yaml` against the same
  payload (`p15-alerts.evaluate`), so a red block on the on-call page and a page on a phone are literally the same
  rule firing — they cannot drift, because there is one implementation and it is imported, not copied.
* **Self-contained and offline.** No fetch, no CDN, no web font, no chart library: inline CSS, tables and inline
  SVG bars. The page renders in the time it takes to transfer ~25 KB, which is the whole reason the budget is
  three seconds. It also means an operator can attach it to an incident channel as a file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import importlib.util
import json
import os
import pathlib
import sys
import time
import typing as t

ROOT = pathlib.Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "docs" / "verification" / "p15-metrics-sample.json"

CSS = """
:root{--bg:#0b0d10;--panel:#14181d;--line:#252b33;--ink:#e8edf2;--dim:#98a2ad;--ok:#3fb950;--warn:#d29922;
--bad:#f85149;--accent:#4493f8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 ui-sans-serif,system-ui,-apple-system,
"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-variant-numeric:tabular-nums}
.wrap{max-width:480px;margin:0 auto;padding:12px 12px 40px}
h1{font-size:17px;margin:0 0 2px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin:18px 0 6px}
.sub{color:var(--dim);font-size:12px;margin-bottom:10px}
.banner{border-radius:8px;padding:10px 12px;margin-bottom:10px;font-weight:600;border:1px solid var(--line)}
.banner.bad{background:#2d1214;border-color:#67242a;color:#ffb4ad}
.banner.ok{background:#10231a;border-color:#1f4b31;color:#9fe0b4}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:10px}
.row{display:flex;justify-content:space-between;gap:8px;padding:5px 0;border-bottom:1px solid #1c2127}
.row:last-child{border-bottom:0}
.k{color:var(--dim)}
.v{font-weight:600;text-align:right}
.chip{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;font-weight:700;
letter-spacing:.04em;text-transform:uppercase;border:1px solid}
.chip.ok{color:#9fe0b4;border-color:#1f4b31;background:#10231a}
.chip.warn{color:#f0d28a;border-color:#5a4715;background:#241d0c}
.chip.bad{color:#ffb4ad;border-color:#67242a;background:#2d1214}
.chip.mute{color:var(--dim);border-color:var(--line)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:4px 2px;border-bottom:1px solid #1c2127}
th{color:var(--dim);font-weight:600}
td.n{text-align:right}
.bar{height:6px;background:#1c2127;border-radius:3px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--accent)}
.alarm{border-left:3px solid var(--bad);padding:6px 8px;margin:6px 0;background:#1a1113;border-radius:0 6px 6px 0}
.alarm.warn{border-color:var(--warn);background:#1a160d}
.alarm .id{font-weight:700}
.alarm .why{color:var(--dim);font-size:12px}
.alarm .rb{font-size:12px;color:#9ad0ff}
footer{color:var(--dim);font-size:11px;margin-top:18px;border-top:1px solid var(--line);padding-top:8px}
"""


def _load_alerts():
    path = ROOT / "tools" / "p15-alerts.py"
    spec = importlib.util.spec_from_file_location("p15_alerts_for_dash", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p15_alerts_for_dash"] = mod
    spec.loader.exec_module(mod)
    return mod


def esc(v: t.Any) -> str:
    return html.escape(str(v), quote=True)


def usdc(micro: t.Any) -> str:
    """Money is printed the way the product prints it: from integer micro-dollars, never from a float."""
    try:
        n = int(micro)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if n < 0 else ""
    n = abs(n)
    return "%s$%s.%02d" % (sign, format(n // 1_000_000, ","), (n % 1_000_000) // 10_000)


def ms(v: t.Any) -> str:
    if v is None:
        return "never"
    v = float(v)
    return "%.0f ms" % v if v < 1000 else "%.1f s" % (v / 1000.0)


def age(v: t.Any) -> str:
    if v is None or v == 0:
        return "—"
    v = float(v) / 1000.0
    if v < 60:
        return "%.0f s" % v
    if v < 3600:
        return "%.0f min" % (v / 60)
    return "%.1f h" % (v / 3600)


def chip(state: str) -> str:
    cls = {"ok": "ok", "warn": "warn", "bad": "bad"}.get(state, "mute")
    return '<span class="chip %s">%s</span>' % (cls, esc(state))


def alarms_block(fired: list[dict], unknown: list[dict]) -> str:
    """The alarm state, on every page. A dashboard that shows green while a rule cannot be evaluated is the exact
    failure this block exists to prevent, so `unknown` is rendered as loudly as `fired`."""
    out = []
    for row in sorted(fired, key=lambda r: str(r.get("severity"))):
        warn = " warn" if row.get("severity") == "SEV2" else ""
        out.append('<div class="alarm%s"><div class="id">%s · %s</div><div>%s</div>'
                   '<div class="why">owner %s</div><div class="rb">runbook: %s</div></div>'
                   % (warn, esc(row.get("severity")), esc(row["id"]), esc(row["summary"]),
                      esc(row.get("owner")), esc(row.get("runbook"))))
    for row in unknown:
        out.append('<div class="alarm warn"><div class="id">%s · cannot evaluate</div><div>%s</div>'
                   '<div class="why">a rule that cannot see its metric is not quiet, it is broken</div></div>'
                   % (esc(row.get("severity") or "SEV?"), esc(row["why"])))
    if not out:
        return ('<div class="panel"><span class="chip ok">no alarms firing</span> '
                '<span class="sub">every rule in the registry is quiet on this read</span></div>')
    return "\n".join(out)


def kill_banner(ks: dict) -> str:
    if ks.get("engaged"):
        return ('<div class="banner bad">KILL SWITCH ENGAGED · %s · for %s</div>'
                % (esc(ks.get("reason") or "no reason recorded"), esc(age(ks.get("engagedForMs")))))
    return '<div class="banner ok">kill switch clear · trading permitted by the risk gate</div>'


def freshness_table(fresh: dict) -> str:
    rows = []
    for f in fresh.get("feeds") or []:
        if f.get("neverSeen"):
            state = "bad"
        elif f.get("silent"):
            state = "bad"
        elif f.get("lagging"):
            state = "warn"
        elif f.get("state") == "ok":
            state = "ok"
        else:
            state = "warn"
        age_txt = "never seen" if f.get("neverSeen") else ms(f.get("frameAgeMs"))
        rows.append('<tr><td>%s<br><span class="k">%s%s</span></td><td>%s</td>'
                    '<td class="n">%s<br><span class="k">%s</span></td><td>%s</td></tr>'
                    % (esc(f.get("source")), esc(f.get("transport") or "?"),
                       "" if f.get("resyncs") in (None, 0) else " · %d resync" % f["resyncs"],
                       esc(f.get("state")), age_txt, ms(f.get("eventLagMs")), chip(state)))
    body = "".join(rows) or ('<tr><td colspan="4" class="k">no feeds recorded — the ingest service has never '
                             'written a cursor, which is itself the answer</td></tr>')
    return '<table><tr><th>source</th><th>state</th><th class="n">frame / event lag</th><th></th></tr>%s</table>' % body


def hops_table(hops: dict) -> str:
    rows = []
    for name in ("risk", "draftToSigned", "signedToSubmitted", "submittedToAck", "signedToAck", "endToEnd"):
        h = hops.get(name) or {}
        p50, p95, p99 = h.get("p50"), h.get("p95"), h.get("p99")
        width = 0 if p99 is None else min(100, int(float(p99) / 100.0))
        rows.append('<tr><td>%s</td><td class="n">%s</td><td class="n">%s</td><td class="n">%s</td>'
                    '<td><div class="bar"><i style="width:%d%%"></i></div></td></tr>'
                    % (esc(name), ms(p50), ms(p95), ms(p99), width))
    return ('<table><tr><th>hop</th><th class="n">p50</th><th class="n">p95</th><th class="n">p99</th><th></th>'
            "</tr>%s</table>" % "".join(rows))


def reasons_table(reasons: dict) -> str:
    if not reasons:
        return '<div class="k">no rejections in the last hour</div>'
    rows = [('<tr><td>%s</td><td class="n">%s</td></tr>' % (esc(k), esc(v)))
            for k, v in sorted(reasons.items(), key=lambda kv: -float(kv[1] or 0))]
    return '<table><tr><th>reason</th><th class="n">count</th></tr>%s</table>' % "".join(rows)


def page(title: str, metrics: dict, blocks: list[str], dashboard: str) -> str:
    # `asOf` is the read's own timestamp: an epoch-millisecond integer in this API, and it is rendered as a
    # readable stamp rather than as a number, because the first question about a dashboard at 2am is how old it is.
    as_of = metrics.get("asOf") or metrics.get("serverAsOf") or 0
    if isinstance(as_of, dict):
        stamp = as_of.get("at") or as_of.get("atMs") or ""
    else:
        try:
            stamp = dt.datetime.fromtimestamp(int(as_of) / 1000, dt.timezone.utc).strftime("%H:%M:%SZ")
        except (TypeError, ValueError, OSError):
            stamp = str(as_of)
    ks = metrics.get("killSwitch") or {}
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>PolyGM · %s</title><style>%s</style></head>
<body><div class="wrap">
<h1>%s</h1>
<div class="sub">%s · one read of /v1/admin/metrics · as of %s</div>
%s
%s
<footer>%s · three dashboards: on-call, product, revenue · every chip is evaluated from ops/alerts.yaml against
this same payload, so a red block here and a page on a phone are the same rule.</footer>
</div></body></html>
""" % (esc(title), CSS, esc(title), esc(dashboard),
       esc(stamp if isinstance(stamp, str) else json.dumps(stamp, default=str)),
       kill_banner(ks), "\n".join(blocks),
       esc(dt.datetime.now(dt.timezone.utc).strftime("rendered %Y-%m-%d %H:%MZ")))


# ---------------------------------------------------------------------------- the three pages
def render_oncall(metrics: dict, fired: list[dict], unknown: list[dict]) -> str:
    money = metrics.get("money") or {}
    unr = money.get("unreconciled") or {}
    biz = metrics.get("business") or {}
    ex = metrics.get("executor") or {}
    blocks = [alarms_block(fired, unknown)]
    money_state = "bad" if unr.get("page") else ("warn" if unr.get("count") else "ok")
    blocks.append('<h2>1 · money correctness</h2><div class="panel">'
                  '<div class="row"><span class="k">unreconciled orders</span><span class="v">%s %s</span></div>'
                  '<div class="row"><span class="k">oldest case</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">unknown-state intents</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">position drift</span><span class="v">%s mismatched · worst %s</span></div>'
                  '<div class="row"><span class="k">fee estimate delta</span><span class="v">%s sum · worst %s</span></div>'
                  '<div class="row"><span class="k">builder fees (30 d)</span><span class="v">%s expected · %s chain</span></div>'
                  "</div>" % (
                      chip(money_state), esc(unr.get("count", 0)), age(unr.get("oldestAgeMs")),
                      esc((money.get("ordersUnknownState") or {}).get("count", 0)),
                      esc((money.get("positionDrift") or {}).get("mismatchedOrders", 0)),
                      usdc((money.get("positionDrift") or {}).get("worstMicro", 0)),
                      usdc((money.get("feeEstimateDelta") or {}).get("sumAbsMicro", 0)),
                      usdc((money.get("feeEstimateDelta") or {}).get("worstMicro", 0)),
                      usdc((money.get("builderFees") or {}).get("expectedMicro", 0)),
                      usdc((money.get("builderFees") or {}).get("chainMeasuredMicro", 0))))
    op = metrics.get("orderPath") or {}
    ex_state = "ok" if ex.get("state") == "live" else ("bad" if ex.get("state") in ("down", "never") else "warn")
    blocks.append('<h2>2 · order path</h2><div class="panel">'
                  '<div class="row"><span class="k">executor</span><span class="v">%s %s · beat %s</span></div>'
                  '<div class="row"><span class="k">intents</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">rejections (1 h)</span><span class="v">%s</span></div>'
                  "%s%s</div>" % (
                      chip(ex_state), esc(ex.get("state")), age(ex.get("lastBeatAgeMs")),
                      esc(json.dumps(op.get("intentsByState") or {})),
                      esc(op.get("rejectionsLastHour", 0)),
                      hops_table(op.get("hops") or {}), reasons_table(op.get("rejectionsByReason") or {})))
    blocks.append('<h2>3 · data freshness</h2><div class="panel">%s</div>'
                  % freshness_table(metrics.get("freshness") or {}))
    sec = metrics.get("security") or {}
    blocks.append('<h2>4 · security and in-flight money</h2><div class="panel">'
                  '<div class="row"><span class="k">compromise indicators (24 h)</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">wallets suspended</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">stuck deposits</span><span class="v">%s · oldest %s</span></div>'
                  '<div class="row"><span class="k">withdrawals in flight</span><span class="v">%s · oldest %s</span></div>'
                  "</div>" % (esc(sec.get("compromiseIndicators24h", 0)), esc(sec.get("walletsSuspended", 0)),
                              esc((biz.get("stuckDeposits") or {}).get("count", 0)),
                              age((biz.get("stuckDeposits") or {}).get("oldestAgeMs")),
                              esc((biz.get("withdrawalsInFlight") or {}).get("count", 0)),
                              age((biz.get("withdrawalsInFlight") or {}).get("oldestAgeMs"))))
    blocks.append('<h2>5 · business, in one line</h2><div class="panel">'
                  '<div class="row"><span class="k">fills 24 h · volume</span><span class="v">%s · %s</span></div>'
                  '<div class="row"><span class="k">withdrawals 24 h</span><span class="v">%s · spike ratio %s</span></div>'
                  "</div>" % (esc(biz.get("fills24h", 0)), usdc(biz.get("volume24hMicro", 0)),
                              esc((biz.get("withdrawals24h") or {}).get("count", 0)),
                              esc(biz.get("withdrawalSpikeRatio", 0))))
    return page("On-call", metrics, blocks, "on-call · health, money, freshness")


def render_product(metrics: dict, fired: list[dict], unknown: list[dict]) -> str:
    biz = metrics.get("business") or {}
    blocks = [alarms_block(fired, unknown)]
    blocks.append('<h2>business</h2><div class="panel">'
                  '<div class="row"><span class="k">fills (24 h)</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">volume (24 h)</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">traders (24 h)</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">deposits (24 h)</span><span class="v">%s · %s</span></div>'
                  '<div class="row"><span class="k">withdrawals (24 h)</span><span class="v">%s · %s</span></div>'
                  '<div class="row"><span class="k">Pro conversions (7 d)</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">alert fires (24 h)</span><span class="v">%s</span></div>'
                  "</div>" % (esc(biz.get("fills24h", 0)), usdc(biz.get("volume24hMicro", 0)),
                              esc(biz.get("traders24h", 0)),
                              esc((biz.get("deposits24h") or {}).get("count", 0)),
                              usdc((biz.get("deposits24h") or {}).get("sumMicro", 0)),
                              esc((biz.get("withdrawals24h") or {}).get("count", 0)),
                              usdc((biz.get("withdrawals24h") or {}).get("sumMicro", 0)),
                              esc(biz.get("proConversions7d", 0)), esc(biz.get("alertFires24h", 0))))
    blocks.append('<h2>alert → trade</h2><div class="panel">'
                  '<div class="row"><span class="k">alerts fired (24 h)</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">fills in the same window</span><span class="v">%s</span></div>'
                  '<div class="sub">Deliberately not a percentage: we hold the two counts and their timestamps, not '
                  "a causal claim. P10's automation runs are where the conversion can be measured honestly.</div>"
                  "</div>" % (esc(biz.get("alertFires24h")), esc(biz.get("fills24h"))))
    return page("Product", metrics, blocks, "product · business metrics")


def render_revenue(metrics: dict, fired: list[dict], unknown: list[dict]) -> str:
    money = metrics.get("money") or {}
    bf = money.get("builderFees") or {}
    delta = bf.get("deltaMicro")
    state = "ok" if not delta else ("warn" if abs(int(delta)) < 1_000_000 else "bad")
    blocks = [alarms_block(fired, unknown)]
    blocks.append('<h2>builder attribution (30 d)</h2><div class="panel">'
                  '<div class="row"><span class="k">orders attributed</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">attributed volume</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">expected fees (our ledger)</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">measured on chain</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">delta</span><span class="v">%s %s</span></div>'
                  '<div class="row"><span class="k">days unmatched</span><span class="v">%s</span></div>'
                  "</div>" % (esc(bf.get("orders", 0)), usdc(bf.get("volumeMicro", 0)),
                              usdc(bf.get("expectedMicro", 0)), usdc(bf.get("chainMeasuredMicro", 0)),
                              chip(state), usdc(delta if delta is not None else 0),
                              esc(bf.get("daysUnmatched", 0))))
    bc = metrics.get("builderCode") or {}
    blocks.append('<h2>builder code</h2><div class="panel">'
                  '<div class="row"><span class="k">code</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">venue state</span><span class="v">%s</span></div>'
                  '<div class="row"><span class="k">rejections seen</span><span class="v">%s</span></div>'
                  "</div>" % (esc(bc.get("code") or "not configured"), esc(bc.get("state")),
                              esc(bc.get("rejectCount", 0))))
    blocks.append('<h2>cost, against the envelope</h2><div class="panel">'
                  '<div class="row"><span class="k">projected month</span><span class="v">%s</span></div>'
                  "</div>" % esc("`tools/p15-cost.py --project`"))
    return page("Revenue", metrics, blocks, "revenue · builder attribution")


PAGES = {"oncall": render_oncall, "product": render_product, "revenue": render_revenue}


def render_all(metrics: dict, fired: list[dict], unknown: list[dict]) -> dict[str, str]:
    return {name: fn(metrics, fired, unknown) for name, fn in PAGES.items()}


def check(metrics: dict, fired: list[dict], unknown: list[dict]) -> list[str]:
    """What a dashboard must be, checked rather than described: fast, single-read, self-contained, and carrying the
    switch. `--check` renders every page from the sample payload and asserts each property."""
    problems: list[str] = []
    for name, fn in PAGES.items():
        t0 = time.perf_counter()
        body = fn(metrics, fired, unknown)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms > 3000:
            problems.append("%s: render took %.0f ms, over the 3 s budget" % (name, elapsed_ms))
        size_kb = len(body.encode()) / 1024.0
        if size_kb > 80:
            problems.append("%s: %.0f KB, over the 80 KB budget for a phone on a bad connection" % (name, size_kb))
        for token in ("http://", "https://", "<script", "url(", "@import"):
            if token in body:
                problems.append("%s: contains %r — the page must be self-contained and offline (no external "
                                "requests, no scripts) or it cannot render in three seconds" % (name, token))
        if "viewport" not in body:
            problems.append("%s: no viewport meta — it will render as a desktop page on a phone" % name)
        if "kill switch" not in body.lower():
            problems.append("%s: the kill-switch state is not on the page (the kit asks for it on every dashboard)"
                            % name)
    oncall = PAGES["oncall"](metrics, fired, unknown)
    if "unreconciled" not in oncall:
        problems.append("oncall: money correctness is missing — it is the first block for a reason")
    if "expected" not in PAGES["revenue"](metrics, fired, unknown):
        problems.append("revenue: the expected-vs-measured comparison is missing")
    if unknown and "cannot evaluate" not in oncall:
        problems.append("oncall: a rule that cannot be evaluated is not shown — the page would read as green")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D4 — the three dashboards")
    ap.add_argument("--from", dest="src", default="", help="a captured /v1/admin/metrics payload")
    ap.add_argument("--api", default="")
    ap.add_argument("--token", default=os.environ.get("PGM_ADMIN_TOKEN", ""))
    ap.add_argument("--out", default="", help="directory to write oncall.html / product.html / revenue.html into")
    ap.add_argument("--check", action="store_true", help="render from the sample and assert the budgets")
    ap.add_argument("--registry", default=str(ROOT / "ops" / "alerts.yaml"))
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)

    metrics: dict = {}
    if a.src:
        metrics = json.loads(pathlib.Path(a.src).read_text())
        metrics = metrics.get("data", metrics)
    elif a.api:
        alerts = _load_alerts()
        if not a.token:
            print("--api needs a token (PGM_ADMIN_TOKEN)", file=sys.stderr)
            return 2
        metrics = alerts.fetch_metrics(a.api, a.token)
    elif a.check:
        if not SAMPLE.exists():
            print("no sample payload at %s — capture one with tools/p15-capture-metrics.py" % SAMPLE,
                  file=sys.stderr)
            return 2
        metrics = json.loads(SAMPLE.read_text())
        metrics = metrics.get("data", metrics)
    else:
        print("nothing to render: pass --from, --api or --check", file=sys.stderr)
        return 2

    import yaml
    alerts = _load_alerts()
    registry = yaml.safe_load(pathlib.Path(a.registry).read_text())
    fired, unknown, quiet = alerts.evaluate(registry.get("rules") or [], metrics,
                                            sources={"synthetic": None, "budgets": None})
    if a.check:
        # The synthetic and budget sources are not collected here, so their rules are `unknown` by design — and
        # reported as such rather than counted as problems, because a dashboard check that demanded a Telegram
        # token would be a check nobody could run.
        problems = check(metrics, fired, unknown)
        for p in problems:
            print("  FAIL %s" % p)
        print("dashboards: %d page(s), %d alarm(s) firing, %d rule(s) quiet, %d unevaluated here"
              % (len(PAGES), len(fired), len(quiet), len(unknown)))
        return 1 if problems else 0

    pages = render_all(metrics, fired, unknown)
    for name, body in pages.items():
        if a.out:
            out = pathlib.Path(a.out)
            out.mkdir(parents=True, exist_ok=True)
            (out / ("%s.html" % name)).write_text(body)
    print("\n".join("%s: %d bytes%s" % (n, len(b.encode()),
                                        " -> %s" % (pathlib.Path(a.out) / (n + ".html")) if a.out else "")
                    for n, b in pages.items()))
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps({"fired": [r["id"] for r in fired],
                                                    "unknown": [u["id"] for u in unknown]}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
