---
id: dependency-cve
order: 17
severity: SEV3
owner: security owner
alarms: [dependency-vulnerability]
last_drilled: 2026-09-23
---

# Dependency CVE (triage and patch)

**One line:** a vulnerability landed in something we depend on. The work is to answer three questions quickly —
are we exposed, is it exploited in the wild, and what is the smallest change that removes the exposure — and then
to write the answer down where the next audit can find it.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV3 `dependency-vulnerability`: the scheduled scan found a new advisory above the severity gate.
* A vendor notice, a CVE in the news, or a container base-image rebuild that fails a policy check.

```bash
python3 tools/dependency-scan.py --json /tmp/deps.json | head -30
python3 -c "import json; d=json.load(open('/tmp/deps.json')); print(json.dumps(d, indent=2)[:2000])"
```

## Diagnosis

1. **Exposure, not severity.** A `critical` in a library we import but never call on a network path is a P3; a
   `medium` in our parser of untrusted input is a P1. The scanner gives the CVSS number; the answer comes from
   reading our own call sites.

```bash
grep -rn "import " services/api/app.py services/ingest/normalise.py | head -20
python3 -c "import importlib.metadata as m; print(m.version('fastapi'))"
```

2. **Is it reachable from the internet?** Anything in the API's request path (`services/api/app.py`) and the
   ingest's parser path (`services/ingest/normalise.py`) is reachable; a build-time tool is not.
3. **Is there a fixed version, and does it exist for our runtime?** Check the advisory's fixed version against what
   we pin in `requirements.txt` — and remember the P05 rule from the tape work: a minor bump can change a float's
   shape, so a bump on the money path gets the same review as a code change.

## Remediation

* **Patch, with a test.** The change and the regression test go in one commit; the dependency scan runs in CI, so a
  silent re-introduction is already caught.
* **No fix available:** document the compensating control (an input validation, a feature off, a WAF rule) and give
  it an owner and an expiry date. "We are aware" is not a control.
* **Vendor/binary dependencies:** record the advisory id in `docs/dependency-review.md` beside the decision,
  including the ones we decided not to act on — that is the difference between a review and a habit.

```bash
python3 tools/dependency-scan.py --json /tmp/deps.json > /dev/null && echo "scan completed"
python3 tools/dependency-scan.py --json /tmp/deps.json; echo "exit=$?"
```

## Verification

* The scan is clean for that advisory, and CI's dependency job is green on the commit that fixes it.
* If a compensating control was used instead: a test that proves the control fires (a payload that would exploit it
  is refused), not a paragraph claiming it does.

## Escalation

* Security owner, next business day for SEV3.
* Immediately, as SEV1/SEV2, if the advisory is being exploited in the wild *and* the component is on a reachable
  path: then this is an incident, the kill switch is on the table, and `docs/runbooks/key-compromise.md` is the
  page if there is any sign of key material being affected.
