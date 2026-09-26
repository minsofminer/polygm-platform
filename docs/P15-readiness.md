# P15 D9 — operational readiness review

A signed checklist, not a status report. Each line names **what proves it**, because the kit's intent is that
launch readiness is a set of artefacts rather than a consensus. `tools/p15-readiness.py --check` reads the same
artefacts and refuses to render a green checklist when one of them is missing or empty.

| # | item | proof | state |
| --- | --- | --- | --- |
| 1 | Every dashboard reviewed by the person who will be on call | a row in `docs/verification/p15-dashboard-review.jsonl`: dashboard, reviewer, date, and the one thing they changed | done |
| 2 | Every alarm fired deliberately in staging, notification verified | `docs/verification/p15-alerts-fired.jsonl`, one row per rule id | partial — the text is verified; delivery needs the Telegram token |
| 3 | Every runbook executed once in staging by someone other than its author | `tools/p15-runbooks-check.py` reports each page's `drilled_by` and date | owner step — 18 pages, one operator |
| 4 | Rollback tested, timed | `docs/verification/P15-rollback-drill.txt` with the measured seconds against the 60 s budget | drill recorded |
| 5 | Synthetic order check running and alarming correctly | the probe's state file plus a recorded firing of `synthetic-order-failed` | text verified; out-of-fleet host is an owner step |
| 6 | On-call rotation staffed for 30 days | `docs/P15-oncall.md`: names, dates, escalation | owner step (staffing is a decision, not an artifact) |
| 7 | Kill switch drilled from a phone | a `kill_switch_drills` row with the propagation measurement and the phone in the notes | owner step — the drill tool is ready, the thumb is not |
| 8 | Cost dashboard showing actual spend | `var/cost-projection.json` with an `actual` recorded via `tools/p15-cost.py --actual` | pending — needs one real billing cycle |

Three of these are ours and five are owner-gated; the split is stated in the table rather than in a sentence, and
`tools/p15-readiness.py` prints exactly which artefact is missing for each red line.

## The 2am demonstration

The kit's quality gate for this phase is a scenario, so the review ends by rehearsing it:

> It is 2am. You get one page. From a phone, in under five minutes, can you say: **is the system healthy, is any
> user's money in an inconsistent state, and should I stop trading?**

The rehearsal, as a script rather than an intention:

1. **Open the on-call dashboard.** One request, one screen, top block first. It answers in the order the questions
   are asked: money correctness, order path, freshness, business — with the kill-switch banner above all of it, so
   "should I stop trading" is answered by whether the switch is already engaged and why.
2. **Read the alarm.** The page carries the metric, the threshold's reason, the owner and the runbook link. The
   notification repeats the kill-switch state, because the first follow-up question is always "is it already
   stopped?".
3. **Follow the runbook's first command.** Every runbook's diagnosis section starts with a copy-pasteable command
   whose output is the next decision (`tools/p15-runbooks-check.py` proves each command's paths and flags exist,
   against the tools' own `--help`).
4. **Decide.** If the money metrics are red, the answer is stop trading: `kill-switch.md`, one POST, reason
   required. If they are green and a feed is silent, the answer is usually don't — the gate already refuses stale
   quotes with `STALE_QUOTE`, and that refusal is visible in the rejection counts rather than hidden.
5. **Record it.** Whatever was touched, with a timestamp, in the incident channel — the P14 rule that outlives its
   phase: no finding closed without a re-test, no remediation without a recorded time.

## What would stop launch

* Any money-correctness alarm that has never been fired deliberately.
* A runbook that references a command this repository does not have (the checker catches it before a human has to).
* A rollback over 60 seconds, or a rollback whose in-flight-order behaviour is unspecified.
* A dashboard that needs two requests, a login, or a laptop.
* The P14 gate reading anything other than GO — which is the current state, and it is why this checklist is signed
  *ready* rather than *launched*.

## Signatures

The phase ends with four names against the eight rows above: the on-call, the money-path owner, the security lead,
and the founder. `tools/p15-readiness.py --sign <role> --evidence "…"` appends to
`docs/verification/p15-readiness-signatures.jsonl`; the tool refuses to record a signature while any row it can
check is red, which is the point of having a tool rather than a form.
