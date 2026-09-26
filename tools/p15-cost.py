#!/usr/bin/env python3
"""P15 D8 — the cost envelope: attribution, the 50/80/100% gates, and the decision about what to cut first.

    python3 tools/p15-cost.py --project                 # the projection, by line, against the envelope
    python3 tools/p15-cost.py --project --gate          # the alarm shape: non-zero only when a NEW gate is crossed
    python3 tools/p15-cost.py --check                   # the drift gate: config, doc and Terraform must agree
    python3 tools/p15-cost.py --actual 118.40           # record what the providers actually billed this month

Why a file and a program rather than a number in a dashboard:

* **Three copies of a number is how a budget becomes a rumour.** The envelope lives in `config/costs.json`; the D2
  table in `docs/P15-deployment.md` and the `monthly_cost_estimate_usd` output in `infra/terraform/outputs.tf`
  both quote it. `--check` fails when the three disagree, so drift is caught by the gate instead of by a bill.
* **A gate that fires every month is a gate nobody reads.** `--gate` compares this projection with the last one
  recorded in `--state` and exits non-zero only when a gate is crossed for the *first* time. 50% is the "look at
  this now" moment; 100% is a decision about the product's runway, and it belongs to the owner, not to an
  on-call engineer at 3am.
* **Actuals are recorded, not inferred.** Nothing here can read a provider's invoice, so `--actual` takes the
  number an operator read off the console and stores it beside the projection. D9's checklist asks for a cost
  dashboard showing actual spend; this is where "actual" is allowed to come from.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "costs.json"
STATE = ROOT / "var" / "cost-projection.json"
DOC = ROOT / "docs" / "P15-deployment.md"
COST_DOC = ROOT / "docs" / "P15-cost.md"
OUTPUTS = ROOT / "infra" / "terraform" / "outputs.tf"


def load(path: pathlib.Path | None = None) -> dict:
    return json.loads(pathlib.Path(path or CONFIG).read_text())


def project(config: dict, scenario: str, usage: dict[str, float] | None = None) -> dict:
    """The arithmetic, and the only place it happens: a point estimate per line, plus the honest range where a
    line has one (the traffic-dependent cache, and the prices marked unverified)."""
    sc = (config.get("scenarios") or {}).get(scenario)
    if sc is None:
        raise SystemExit("no scenario %r in %s (have: %s)"
                         % (scenario, CONFIG, ", ".join(sorted(config.get("scenarios") or {}))))
    lines, point, low, high = [], 0.0, 0.0, 0.0
    for line in sc["lines"]:
        amount = float(line["amount"])
        rng = line.get("range") or [amount, amount]
        override = (usage or {}).get(line["id"])
        if override is not None:
            amount, rng = float(override), [float(override), float(override)]
        lines.append({**line, "amount": round(amount, 2), "range": [round(float(rng[0]), 2),
                                                                    round(float(rng[1]), 2)]})
        point += amount
        low += float(rng[0])
        high += float(rng[1])
    envelope = float(config["envelope_usd_per_month"])
    return {"scenario": scenario, "label": sc["label"], "lines": lines, "point": round(point, 2),
            "range": [round(low, 2), round(high, 2)], "envelope": envelope,
            "used_fraction": round(point / envelope, 4),
            "gates_crossed": [g for g in config["gates"] if point >= g * envelope],
            "headroom": round(envelope - point, 2)}


def print_projection(p: dict) -> None:
    print("%s — %s" % (p["scenario"], p["label"]))
    print("  %-20s %-44s %10s" % ("line", "what", "$/month"))
    for line in p["lines"]:
        note = " [UNVERIFIED]" if line.get("verified") is False else ""
        rng = "" if line["range"][0] == line["range"][1] else " (range %.0f–%.0f)" % tuple(line["range"])
        print("  %-20s %-44s %10.2f%s%s" % (line["id"], line["what"][:44], line["amount"], rng, note))
    print("  %-20s %-44s %10.2f" % ("TOTAL", "range %.0f–%.0f" % tuple(p["range"]), p["point"]))
    print("  envelope $%.0f/month · using %.1f%% · headroom $%.2f"
          % (p["envelope"], p["used_fraction"] * 100, p["headroom"]))
    for gate in p["gates_crossed"]:
        print("  GATE crossed: %.0f%% ($%.0f)" % (gate * 100, gate * p["envelope"]))


def record(p: dict, state_path: pathlib.Path, actual: float | None, scenario: str) -> dict:
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            state = {}
    entry = state.setdefault(scenario, {})
    previous = set(entry.get("gates_crossed") or [])
    now = set(p["gates_crossed"])
    entry.update({"point": p["point"], "range": p["range"], "gates_crossed": sorted(now),
                  "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
    if actual is not None:
        entry["actual"] = round(float(actual), 2)
        entry["actual_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return {"new_gates": sorted(now - previous), "previous_gates": sorted(previous), "state": str(state_path)}


def check(config: dict) -> list[str]:
    """The drift gate. The number in the doc and the number in the Terraform output are both claims about the
    arithmetic in this file, and a claim nobody checks is the first step to a surprise invoice."""
    problems: list[str] = []
    p = project(config, "launch")
    total = p["point"]
    terra = OUTPUTS.read_text() if OUTPUTS.exists() else ""
    m = re.search(r'monthly_cost_estimate_usd[\s\S]*?value\s*=\s*"([^"]+)"', terra)
    if not m:
        problems.append("infra/terraform/outputs.tf has no monthly_cost_estimate_usd value")
    else:
        nums = [float(x) for x in re.findall(r"\$(\d+(?:\.\d+)?)", m.group(1))]
        if not nums or abs(nums[0] - total) > 1.0:
            problems.append("outputs.tf says %r but the config projects $%.2f for launch" % (m.group(1), total))
    doc = DOC.read_text() if DOC.exists() else ""
    if ("%.0f" % total) not in doc and ("%.2f" % total) not in doc:
        problems.append("docs/P15-deployment.md never states the projected launch total ($%.2f) — the doc and the "
                        "number printed at apply time must be the same claim" % total)
    cost_doc = COST_DOC.read_text() if COST_DOC.exists() else ""
    if cost_doc and ("%.2f" % total) not in cost_doc:
        problems.append("docs/P15-cost.md does not state the projected total ($%.2f)" % total)
    if float(config["envelope_usd_per_month"]) > 300:
        problems.append("the envelope is $%.0f, over the kit's $300/month constraint"
                        % float(config["envelope_usd_per_month"]))
    for sc_name, sc in (config.get("scenarios") or {}).items():
        for line in sc["lines"]:
            if "amount" not in line:
                problems.append("%s/%s has no amount" % (sc_name, line.get("id")))
            if line.get("verified") is False and "UNVERIFIED" not in json.dumps(line):
                problems.append("%s/%s is unverified but does not say so" % (sc_name, line.get("id")))
    if not (config.get("cut_order") or []):
        problems.append("no cut order — the kit asks for the decision to be made before it is needed")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D8 — the cost envelope")
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--scenario", default="launch")
    ap.add_argument("--project", action="store_true")
    ap.add_argument("--gate", action="store_true", help="non-zero only when a gate is crossed for the first time")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--actual", type=float, default=None, help="what the providers actually billed this month")
    ap.add_argument("--state", default=str(STATE))
    ap.add_argument("--json", default="")
    ap.add_argument("--usage", action="append", default=[], help="line=amount, e.g. neon-postgres=110.50")
    a = ap.parse_args(argv)

    config = load(pathlib.Path(a.config))
    if a.check:
        problems = check(config)
        for p in problems:
            print("  FAIL %s" % p)
        print("cost config: %d line(s) across %d scenario(s), %d problem(s)"
              % (sum(len(s["lines"]) for s in config["scenarios"].values()), len(config["scenarios"]),
                 len(problems)))
        return 1 if problems else 0

    usage = {}
    for pair in a.usage:
        key, _, value = pair.partition("=")
        usage[key.strip()] = float(value)
    p = project(config, a.scenario, usage)
    print_projection(p)
    rec = record(p, pathlib.Path(a.state), a.actual, a.scenario)
    if a.actual is not None:
        print("recorded actual spend: $%.2f (projection was $%.2f)" % (a.actual, p["point"]))
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps({"projection": p, "record": rec}, indent=2) + "\n")
    if a.gate:
        if rec["new_gates"]:
            print("NEW GATE CROSSED: %s (previous: %s)"
                  % (", ".join("%.0f%%" % (g * 100) for g in rec["new_gates"]),
                     ", ".join("%.0f%%" % (g * 100) for g in rec["previous_gates"]) or "none"))
            return 1
        print("no new gate crossed (crossed: %s)"
              % (", ".join("%.0f%%" % (g * 100) for g in p["gates_crossed"]) or "none"))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
