#!/usr/bin/env python3
"""P15 D5 — the alarm engine: evaluate the registry, render the page, refuse to let a rule rot.

    python3 tools/p15-alerts.py --check                       # the gate: registry vs runbooks vs the sample payload
    python3 tools/p15-alerts.py --from docs/verification/p15-metrics-sample.json --all --notify
    python3 tools/p15-alerts.py --api https://api.polygm.trade --token "$PGM_ADMIN_TOKEN"
    python3 tools/p15-alerts.py --record unreconciled-orders --evidence "staging, INC-1, notification verified"

Why this is a program and not a monitoring console:

* **An alarm without a runbook gets deleted** (the kit's rule). Enforcing that needs both halves in one place, so
  the registry names a runbook file and `--check` fails if the file is absent — and fails the other way too, if a
  runbook claims an alarm id the registry does not define.
* **A rule that cannot see its metric is not silent, it is broken.** Evaluation returns three states, never two:
  fired, quiet, and *cannot evaluate*. The third exits 2 and is printed loudly, because the classic monitoring
  failure is a renamed field that turns a page into a shrug.
* **The colour on the dashboard and the page on the phone are the same rule.** `tools/p15-dashboards.py` imports
  `evaluate()` from here, so a screen cannot disagree with an alarm about the same number.

Sources, because "the alarm exists" and "the alarm can be evaluated" are different claims:

    metrics     the `/v1/admin/metrics` payload (the same read the dashboards render)
    synthetic   the external probe's state file (`tools/p15-synthetic.py`)
    budgets     our declared venue buckets (`services/ingest/net.py`) vs the counters we have spent
    host        a fact about the box (disk); collected here, since there is no node-exporter in this stack
    tool        another tool's exit code (the cost projector, the dependency scan, the post-deploy watch)

A source that cannot produce its fact (no `rate_counters` table in a fresh checkout, no cache on this box) yields
**unknown**, which is printed and never rendered as ok: unknown is not green.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import typing as t

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "ops" / "alerts.yaml"
SAMPLE = ROOT / "docs" / "verification" / "p15-metrics-sample.json"
RUNBOOKS = ROOT / "docs" / "runbooks"
FIRED_LOG = ROOT / "docs" / "verification" / "p15-alerts-fired.jsonl"
SYNTHETIC_STATE = pathlib.Path(os.environ.get("PGM_SYNTHETIC_STATE") or "/var/lib/polygm/synth.json")

FIRED, QUIET, UNKNOWN = "fired", "quiet", "unknown"


class RuleError(Exception):
    """A rule that cannot be evaluated. Never a silence, never a page: an error the gate and the cron both see."""


# ---------------------------------------------------------------------------- the payload, by path
_MISSING = object()


def resolve(payload: t.Any, path: str) -> tuple[t.Any, bool]:
    """`a.b.c` and `a.b[*].c`, returning (value, is_list_selection).

    A path that walks into a list without `[*]` is an error rather than a guess: a rule that says
    `freshness.feeds.silent` when the data is a list of feeds is a rule that will fire on nothing forever.
    """
    if "[*]" in path:
        head, _, tail = path.partition("[*]")
        base_path = head.rstrip(".")
        base = _walk(payload, base_path) if base_path else payload
        if base is _MISSING:
            raise RuleError("no such path: %s" % base_path)
        if not isinstance(base, list):
            raise RuleError("%s is not a list, so %s[*] selects nothing" % (base_path, base_path))
        items = [(_walk(item, tail.lstrip(".")) if tail.strip(".") else item) for item in base]
        return items, True
    return _walk(payload, path), False


def _walk(obj: t.Any, path: str) -> t.Any:
    cur = obj
    for part in [p for p in path.split(".") if p]:
        if isinstance(cur, dict):
            if part not in cur:
                return _MISSING
            cur = cur[part]
        elif isinstance(cur, list):
            # A list indexed by name is a map; index numerically, but say so in the error, because "0" is the only
            # index a path can name and that is almost never what a rule means.
            if not part.isdigit():
                raise RuleError("cannot index a list with %r — use `[*]`" % part)
            idx = int(part)
            if idx >= len(cur):
                return _MISSING
            cur = cur[idx]
        else:
            return _MISSING
    return cur


OPS: dict[str, t.Callable[[t.Any, t.Any], bool]] = {
    "is_true": lambda v, _x: v is True,
    "is_false": lambda v, _x: v is False,
    "nonzero": lambda v, _x: _num(v) not in (0, None),
    "gt": lambda v, x: _num(v) is not None and _num(v) > _num(x),
    "gte": lambda v, x: _num(v) is not None and _num(v) >= _num(x),
    "lt": lambda v, x: _num(v) is not None and _num(v) < _num(x),
    "lte": lambda v, x: _num(v) is not None and _num(v) <= _num(x),
    "eq": lambda v, x: v == x,
    "neq": lambda v, x: v != x,
    "in": lambda v, x: v in (x or []),
}


def _num(v: t.Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


# ---------------------------------------------------------------------------- evaluation
def evaluate(rules: list[dict], payload: dict, *, sources: dict[str, t.Any] | None = None,
             now_ms: int | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """(fired, unknown, quiet) — three lists, because collapsing two of them is how a broken rule looks healthy."""
    sources = sources or {}
    fired: list[dict] = []
    unknown: list[dict] = []
    quiet: list[dict] = []
    for rule in rules:
        rid, source = rule["id"], rule.get("source", "metrics")
        try:
            detail, hits = _evaluate_one(rule, payload, sources)
        except RuleError as exc:
            unknown.append({"id": rid, "severity": rule.get("severity"), "why": str(exc),
                            "runbook": rule.get("runbook"), "owner": rule.get("owner")})
            continue
        context = dict(payload)
        context.update(detail)
        row = {"id": rid, "severity": rule.get("severity"), "summary": _render(rule, context),
               "details": detail, "hits": hits, "runbook": rule.get("runbook"), "owner": rule.get("owner"),
               "route": rule.get("route"), "source": source, "why": rule.get("why", "")}
        (fired if hits else quiet).append(row)
    return fired, unknown, quiet


def _evaluate_one(rule: dict, payload: dict, sources: dict[str, t.Any]) -> tuple[dict, bool]:
    source = rule.get("source", "metrics")
    if source == "metrics":
        return _evaluate_metrics(rule, payload)
    if source == "synthetic":
        return _evaluate_synthetic(rule, sources.get("synthetic"))
    if source == "budgets":
        return _evaluate_budgets(rule, sources.get("budgets"))
    if source == "host":
        return _evaluate_host(rule, sources.get("host"))
    if source == "tool":
        return _evaluate_tool(rule, sources.get("tool"))
    raise RuleError("unknown source %r" % source)


def _evaluate_metrics(rule: dict, payload: dict) -> tuple[dict, bool]:
    path = rule["metric"]
    value, is_list = resolve(payload, path)
    missing_policy = rule.get("missing", "error")

    def one(v: t.Any) -> bool:
        if v is _MISSING:
            if missing_policy == "zero":
                v = 0
            else:
                raise RuleError("no value at %s in this payload (add `missing: zero` if absent means zero)" % path)
        return OPS[rule["op"]](v, rule.get("value"))

    if not is_list:
        return {"value": None if value is _MISSING else value}, one(value)

    selected = value
    where = rule.get("where") or {}
    if where:
        # A rule that filters on a field of the item (`where: {transport: ws}`) and then tests another field
        # (`silent`) cannot filter a list of *values*: the filter needs the items themselves. So the item path is
        # resolved instead and the projected field is taken from each surviving item — otherwise the selection is
        # silently empty and the rule reports "unknown" forever, which is exactly the class of bug this tool exists
        # to make impossible.
        item_path, _, tail = path.partition("[*]")
        items, _ = resolve(payload, item_path + "[*]")
        kept = [item for item in items
                if isinstance(item, dict) and all(item.get(k) == v for k, v in where.items())]
        field = tail.lstrip(".")
        if not field:
            raise RuleError("%s uses `where` but selects whole items — name a field after [*]" % path)
        selected = [(_walk(item, field) if field else item) for item in kept]
        if not kept:
            raise RuleError("%s selects no item with where=%s — an empty selection is not a pass"
                            % (path, where))
    if rule.get("min_items") and len(selected) < int(rule["min_items"]):
        raise RuleError("%s selects %d item(s) but the rule needs at least %s — an empty selection is not a pass"
                        % (path, len(selected), rule["min_items"]))
    if not selected:
        raise RuleError("%s selects nothing with where=%s — an empty selection is not a pass" % (path, where))
    booleans = [one(v) for v in selected]
    mode = rule.get("mode", "any")
    hits = all(booleans) if mode == "all" else any(booleans)
    return {"value": selected, "mode": mode, "matched": sum(1 for b in booleans if b),
            "of": len(booleans)}, hits


def _evaluate_synthetic(rule: dict, state: t.Any) -> tuple[dict, bool]:
    if state is None:
        raise RuleError("no synthetic probe state (set PGM_SYNTHETIC_STATE, or run tools/p15-synthetic.py "
                        "--loop once); the probe's own file is the only evidence that the product works "
                        "end to end")
    misses = int(state.get("consecutive_misses") or 0)
    need = int(rule.get("misses", 3))
    return {"misses": misses, "need": need, "detail": str(state.get("last_detail") or "")[:200],
            "last_ok_ms": state.get("last_ok_ms")}, misses >= need


def _evaluate_budgets(rule: dict, budgets: t.Any) -> tuple[dict, bool]:
    if budgets is None:
        raise RuleError("no bucket budget facts collected (run with --budgets; see services/ingest/net.py)")
    if not isinstance(budgets, list) or not budgets:
        raise RuleError("the budget collector returned no declared buckets — an empty list is not a pass")
    threshold = float(rule.get("used_fraction_gte", 0.8))
    rows = [b for b in budgets if float(b.get("limit") or 0) > 0]
    if not rows:
        raise RuleError("every declared bucket has a zero limit — a configuration bug, not a quiet alarm")
    worst = max(rows, key=lambda b: (b["used"] / b["limit"]))
    frac = worst["used"] / worst["limit"]
    return {"key": worst["key"], "used": worst["used"], "limit": worst["limit"], "bucketMs": worst["bucketMs"],
            "usedFraction": round(frac, 3), "buckets": rows}, frac >= threshold


def _evaluate_host(rule: dict, host: t.Any) -> tuple[dict, bool]:
    probe = rule.get("probe")
    if host is None:
        raise RuleError("host facts were not collected (run with --host)")
    if host.get(probe) is None:
        raise RuleError("the host collector could not read %s on this box — unknown, which is not green" % probe)
    return dict(host), OPS[rule["op"]](host[probe], rule.get("value"))


def _evaluate_tool(rule: dict, tools: t.Any) -> tuple[dict, bool]:
    rel = rule["tool"]
    if tools is None:
        raise RuleError("tool results were not collected (run with --tools)")
    res = tools.get(rel)
    if res is None:
        raise RuleError("the collector did not run %s" % rel)
    if res.get("exit") is None:
        raise RuleError("%s could not be run here: %s" % (rel, res.get("why") or "unknown"))
    # Exit 2 is the convention these tools use for "cannot tell" (no verdict recorded, no data source): it is
    # reported as unknown rather than as a fire, because an alarm that pages on its own missing input is an alarm
    # that teaches people to ignore it.
    if int(res["exit"]) == 2:
        raise RuleError("%s could not tell: %s" % (rel, (res.get("output") or "").strip()[:200]))
    return {"tool": rel, "output": res.get("output", "")[:400], "exit": res["exit"]}, int(res["exit"]) != 0


#: `{path}` or `{path|filter}` — the filter vocabulary is `FILTERS` below.
PLACEHOLDER = re.compile(r"\{([a-zA-Z0-9_.\[\]*]+(?:\|[a-z]+)?)\}")


def _human_ms(v: t.Any) -> str:
    """A duration, because a page that says `1800557 ms` makes the reader do arithmetic at 2am."""
    try:
        ms = float(v)
    except (TypeError, ValueError):
        return json.dumps(v, default=str)
    if ms < 1000:
        return "%.0f ms" % ms
    if ms < 60_000:
        return "%.1f s" % (ms / 1000.0)
    if ms < 3_600_000:
        return "%.0f min" % (ms / 60_000.0)
    return "%.1f h" % (ms / 3_600_000.0)


def _usd(micro: t.Any) -> str:
    """Money from integer micro-dollars, never from a float — the same rule the product follows."""
    try:
        n = int(micro)
    except (TypeError, ValueError):
        return json.dumps(micro, default=str)
    sign = "-" if n < 0 else ""
    n = abs(n)
    return "%s$%s.%02d" % (sign, format(n // 1_000_000, ","), (n % 1_000_000) // 10_000)


FILTERS: dict[str, t.Callable[[t.Any], str]] = {"age": _human_ms, "usd": _usd, "raw": json.dumps}


def _render(rule: dict, detail: dict) -> str:
    """One sentence an operator can act on, with the numbers already substituted.

    A placeholder may name a filter: `{money.unreconciled.oldestAgeMs|age}` renders as "30 min" and
    `{...sumAbsMicro|usd}` as "$1.23". The default renders the raw value, because guessing a unit is how a
    dashboard ends up displaying microseconds as dollars.
    """
    template = rule.get("summary") or rule["id"]

    def sub(m: re.Match) -> str:
        spec = m.group(1)
        path, _, filt = spec.partition("|")
        try:
            value, _ = resolve(detail, path)
        except RuleError:
            return "?"
        if value is _MISSING:
            return "?"
        if filt:
            fn = FILTERS.get(filt.strip())
            if fn is None:
                return "?"
            return fn(value)
        if isinstance(value, str):
            return value
        return json.dumps(value, default=str)

    return PLACEHOLDER.sub(sub, template)


def render_notification(row: dict, kill_switch: dict | None) -> str:
    """The text the on-call reads. The kill-switch state goes in every one of them: the first follow-up question
    at 2am is always "is it already stopped?", and a page that does not answer it costs a round trip."""
    ks = kill_switch or {}
    state = ("ENGAGED (%s)" % (ks.get("reason") or "no reason recorded")) if ks.get("engaged") else "clear"
    lines = ["[%s] %s" % (row.get("severity", "SEV?"), row["id"]),
             row["summary"],
             "kill switch: %s" % state,
             "owner: %s" % (row.get("owner") or "UNOWNED — this alarm must be deleted or given an owner")]
    if row.get("why"):
        lines.append("why it matters: %s" % row["why"])
    lines.append("runbook: %s" % (row.get("runbook") or "NONE — an alarm without a runbook gets deleted"))
    detail = row.get("details") or {}
    if "matched" in detail:
        lines.append("matched %s of %s selected item(s)" % (detail["matched"], detail["of"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------- sources
def collect_host() -> dict:
    """The facts about a box this stack has no exporter for. Anything that cannot be read stays absent, and
    `_evaluate_host` turns an absent fact into unknown rather than into a quiet rule."""
    out: dict = {}
    try:
        usage = shutil.disk_usage("/")
        out["disk_used_fraction"] = round(usage.used / usage.total, 4)
        out["disk_free_gb"] = round(usage.free / 1e9, 2)
        out["disk_mount"] = "/"
    except OSError as exc:               # pragma: no cover - depends on the box
        out["why"] = str(exc)
    return out


def collect_budgets() -> list[dict] | None:
    """Our declared venue buckets against what we have spent. `net.BUCKETS` is the declaration; `rate_counters`
    is what happened; reading them together is the whole point, because a limiter invented at page time is a
    limiter nobody has been respecting."""
    net_path = ROOT / "services" / "ingest" / "net.py"
    if not net_path.exists():
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("p15_net", net_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:                    # noqa: BLE001 - a module that cannot load is "cannot tell", not zero
        return None
    buckets = getattr(mod, "BUCKETS", None)
    if not isinstance(buckets, dict):
        return None
    db = pathlib.Path(os.environ.get("PGM_DB_PATH") or ROOT / "var" / "polygm.db")
    spent: dict[str, int] = {}
    if db.exists():
        import sqlite3
        import time
        try:
            con = sqlite3.connect(str(db))
            rows = con.execute("SELECT key, bucket_ms, count FROM rate_counters WHERE updated_ms >= ?",
                               (int(time.time()) * 1000 - 300_000,)).fetchall()
            for key, _bucket_ms, count in rows:
                spent[str(key)] = max(spent.get(str(key), 0), int(count))
            con.close()
        except Exception:                # noqa: BLE001 - a read that fails is unknown, and unknown is printed
            spent = {}
    out = []
    for key, b in buckets.items():
        capacity = int(getattr(b, "capacity", 0) if not isinstance(b, dict) else b.get("capacity", 0))
        bucket_ms = int(getattr(b, "bucket_ms", 60_000) if not isinstance(b, dict) else b.get("bucket_ms",
                                                                                              60_000))
        per_sec = float(getattr(b, "per_sec", 0) if not isinstance(b, dict) else b.get("per_sec", 0))
        out.append({"key": str(key), "bucketMs": bucket_ms, "limit": capacity, "perSec": per_sec,
                    "used": int(spent.get(str(key), 0))})
    return out


def collect_synthetic(path: pathlib.Path | None = None) -> dict | None:
    p = pathlib.Path(path or SYNTHETIC_STATE)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def collect_tools(rules: list[dict]) -> dict:
    """Run the exit-code alarms. Each one is a tool that answers "is this fine?" with its exit status, so the
    registry cannot invent a fact that no program produces."""
    out: dict = {}
    for rule in rules:
        if rule.get("source") != "tool":
            continue
        rel = rule["tool"]
        if not (ROOT / rel).exists():
            out[rel] = {"exit": None, "why": "%s does not exist" % rel}
            continue
        argv = [sys.executable, str(ROOT / rel), *[str(a) for a in (rule.get("args") or [])]]
        try:
            p = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            out[rel] = {"exit": None, "why": str(exc)}
            continue
        stdout = p.stdout.strip()
        out[rel] = {"exit": p.returncode,
                    "output": (stdout.splitlines() or [""])[-1] if stdout else p.stderr.strip()[-200:]}
    return out


def fetch_metrics(api: str, token: str, timeout: float = 10.0) -> dict:
    import urllib.request
    req = urllib.request.Request(api.rstrip("/") + "/v1/admin/metrics",
                                 headers={"x-admin-token": token, "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 - the operator's own URL
        body = json.loads(resp.read().decode())
    return body.get("data", body)


# ---------------------------------------------------------------------------- the gate
def paths_of(obj: t.Any, prefix: str = "") -> set[str]:
    """Every leaf path in a payload, with list items collapsed to `[*]` so the check means "this shape exists"."""
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out |= paths_of(v, "%s.%s" % (prefix, k) if prefix else str(k))
    elif isinstance(obj, list):
        for item in obj:
            out |= paths_of(item, prefix + "[*]")
    else:
        out.add(prefix)
    return out


def _runbook_alarm_ids() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    if not RUNBOOKS.exists():
        return found
    for path in sorted(RUNBOOKS.glob("*.md")):
        if path.name == "README.md":
            continue
        text = path.read_text()
        head = text.split("---")[1] if text.startswith("---") else ""
        m = re.search(r"\balarms:\s*\[([^\]]*)\]", head)
        found[path.name] = {a.strip() for a in (m.group(1).split(",") if m else []) if a.strip()}
    return found


def check(registry: dict) -> list[str]:
    """Everything that can rot: the runbook links, the owner, the route, the budget sum, and the metric paths."""
    problems: list[str] = []
    rules = registry.get("rules") or []
    owners = registry.get("owners") or {}
    routes = registry.get("routes") or {}
    ids = [r.get("id") for r in rules]
    if len(set(ids)) != len(ids):
        problems.append("duplicate alarm ids: %s" % sorted({i for i in ids if ids.count(i) > 1}))
    for required in ("unreconciled-orders", "orders-unknown-state", "kill-switch-engaged", "executor-down",
                     "key-compromise-indicator", "withdrawal-spike", "all-websocket-sources-dead",
                     "builder-code-disabled", "synthetic-order-failed"):
        if required not in ids:
            problems.append("the kit's SEV1 set is missing %s" % required)

    runbook_ids = _runbook_alarm_ids()
    sample = json.loads(SAMPLE.read_text()) if SAMPLE.exists() else None
    sample_paths = paths_of(sample if sample is not None else {}) if sample else set()
    budget = 0.0
    for rule in rules:
        rid = rule.get("id")
        sev, source = rule.get("severity"), rule.get("source", "metrics")
        if sev not in ("SEV1", "SEV2", "SEV3"):
            problems.append("%s: severity must be SEV1/SEV2/SEV3, not %r" % (rid, sev))
        if rule.get("owner") not in owners:
            problems.append("%s: owner %r is not in the owners map" % (rid, rule.get("owner")))
        route = rule.get("route")
        if route not in routes:
            problems.append("%s: route %r is not a declared route" % (rid, route))
        elif sev in ("SEV1", "SEV2") and routes.get(route, {}).get("channel") != "telegram-oncall":
            problems.append("%s: a %s alarm must route to the on-call channel, not %r"
                            % (rid, sev, routes.get(route, {}).get("channel")))
        rb = rule.get("runbook", "")
        if not rb:
            problems.append("%s: no runbook — an alarm without one gets deleted" % rid)
        elif not (ROOT / rb).exists():
            problems.append("%s: runbook %s does not exist" % (rid, rb))
        elif rid not in runbook_ids.get(pathlib.Path(rb).name, set()):
            problems.append("%s: %s does not claim this alarm in its front matter (the link must work both ways)"
                            % (rid, pathlib.Path(rb).name))
        if not str(rule.get("why", "")).strip():
            problems.append("%s: no `why` — a threshold nobody can justify is a threshold that will be tuned "
                            "away" % rid)
        budget += float(rule.get("expected_pages_per_week") or 0)
        if source == "metrics":
            metric = str(rule.get("metric", ""))
            if "*" in metric:
                if sample_paths and metric not in sample_paths:
                    problems.append("%s: %s is not in the sample payload (a renamed field must break this check)"
                                    % (rid, metric))
            elif sample_paths and metric not in sample_paths and rule.get("missing") != "zero":
                problems.append("%s: %s is not in the sample payload and the rule does not declare "
                                "`missing: zero`" % (rid, metric))
            if rule.get("op") not in OPS:
                problems.append("%s: unknown op %r" % (rid, rule.get("op")))
        if source == "tool":
            tool = ROOT / str(rule.get("tool", ""))
            if not tool.exists():
                problems.append("%s: tool %s does not exist" % (rid, rule.get("tool")))
            else:
                helps = subprocess.run([sys.executable, str(tool), "--help"], capture_output=True, text=True,
                                       cwd=str(ROOT), timeout=60).stdout
                for flag in (rule.get("args") or []):
                    if str(flag).startswith("--") and str(flag) not in helps:
                        problems.append("%s: %s does not accept %s" % (rid, rule.get("tool"), flag))
    limit = float((registry.get("budget") or {}).get("pages_per_week_max") or 0)
    if budget >= limit:
        problems.append("the rules' own page estimates sum to %.2f/week, at or above the %.2f budget — the kit's "
                        "rule is fewer than three pages a week or the thresholds are wrong" % (budget, limit))
    synth = [r for r in rules if r.get("source") == "synthetic"]
    if len(synth) != 1:
        problems.append("exactly one synthetic rule is expected, found %d" % len(synth))
    elif int(synth[0].get("misses", 0)) != 3:
        problems.append("the synthetic rule must need three consecutive misses (a single blip must not page)")
    orphan = {a for a in ids} - {a for claimed in runbook_ids.values() for a in claimed}
    if orphan:
        problems.append("alarms no runbook claims: %s" % sorted(orphan))
    dangling = {a for claimed in runbook_ids.values() for a in claimed} - set(ids)
    if dangling:
        problems.append("runbooks claim alarm ids the registry does not define: %s" % sorted(dangling))
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D5 — the alarm registry, evaluated")
    ap.add_argument("--registry", default=str(REGISTRY))
    ap.add_argument("--check", action="store_true", help="validate the registry against the runbooks and the sample")
    ap.add_argument("--from", dest="src", default="", help="evaluate a captured metrics payload")
    ap.add_argument("--api", default="", help="read /v1/admin/metrics from a live API")
    ap.add_argument("--token", default=os.environ.get("PGM_ADMIN_TOKEN", ""))
    ap.add_argument("--all", action="store_true", help="collect every source (host, budgets, synthetic, tools)")
    ap.add_argument("--host", action="store_true")
    ap.add_argument("--budgets", action="store_true")
    ap.add_argument("--tools", action="store_true")
    ap.add_argument("--synthetic-state", default="")
    ap.add_argument("--json", default="", help="write the evaluation to a file")
    ap.add_argument("--record", default="", help="append a deliberate firing to the drill record (D9 evidence)")
    ap.add_argument("--evidence", default="")
    ap.add_argument("--notify", action="store_true", help="print the notification text for every fired rule")
    a = ap.parse_args(argv)

    registry = yaml.safe_load(pathlib.Path(a.registry).read_text())
    rules = registry.get("rules") or []

    if a.record:
        return record_firing(a.record, a.evidence, registry)

    if a.check:
        problems = check(registry)
        for p in problems:
            print("  FAIL %s" % p)
        total = sum(float(r.get("expected_pages_per_week") or 0) for r in rules)
        print("alarm registry: %d rules, %d problem(s), %.2f estimated pages/week of a %.2f budget"
              % (len(rules), len(problems), total,
                 float((registry.get("budget") or {}).get("pages_per_week_max", 0))))
        return 1 if problems else 0

    payload: dict = {}
    if a.src:
        payload = json.loads(pathlib.Path(a.src).read_text())
        payload = payload.get("data", payload)
    elif a.api:
        if not a.token:
            print("--api needs a token (PGM_ADMIN_TOKEN)", file=sys.stderr)
            return 2
        payload = fetch_metrics(a.api, a.token)

    sources: dict[str, t.Any] = {}
    if a.all or a.host:
        sources["host"] = collect_host()
    if a.all or a.budgets:
        sources["budgets"] = collect_budgets()
    if a.all or a.synthetic_state or not payload:
        sources["synthetic"] = collect_synthetic(pathlib.Path(a.synthetic_state) if a.synthetic_state else None)
    if a.all or a.tools:
        sources["tool"] = collect_tools(rules)

    fired, unknown, quiet = evaluate(rules, payload, sources=sources)
    kills = (payload or {}).get("killSwitch") or {}
    out = {"fired": fired, "unknown": unknown, "quiet": [q["id"] for q in quiet],
           "pages_per_week_estimate": sum(float(r.get("expected_pages_per_week") or 0) for r in rules)}
    for row in fired:
        print("FIRED %s %s" % (row["severity"], row["id"]))
        print("      %s" % row["summary"])
        if a.notify:
            print("\n".join("      " + line for line in render_notification(row, kills).splitlines()))
    for row in unknown:
        print("UNKNOWN %s: %s" % (row["id"], row["why"]))
    if not fired and not unknown:
        print("nothing fired (%d rule(s) quiet)" % len(quiet))
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps(out, indent=2, default=str) + "\n")
    if unknown:
        return 2                     # cannot-evaluate is an error, not a quiet system
    return 1 if fired else 0


def record_firing(rid: str, evidence: str, registry: dict) -> int:
    """D9 asks for every alarm fired deliberately in staging, with the notification verified. This writes the row;
    the readiness checklist reads it, so "we tested the alerts" becomes a list of ids and times."""
    rules = {r["id"]: r for r in registry.get("rules") or []}
    if rid not in rules:
        print("no such alarm: %s" % rid, file=sys.stderr)
        print("known: %s" % ", ".join(sorted(rules)), file=sys.stderr)
        return 2
    if len(evidence.strip()) < 12:
        print("a firing needs evidence a reviewer can read (where it fired, what the notification said)",
              file=sys.stderr)
        return 2
    FIRED_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {"id": rid, "severity": rules[rid].get("severity"), "runbook": rules[rid].get("runbook"),
           "owner": rules[rid].get("owner"),
           "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "evidence": evidence.strip()[:400]}
    with FIRED_LOG.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    print("recorded %s at %s%s" % (rid, row["at"], "" if row["runbook"] else " (no runbook!)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
