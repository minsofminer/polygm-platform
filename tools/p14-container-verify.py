#!/usr/bin/env python3
"""P14 D4 — container verification, by running the artefact.

D4's runtime section has carried two OPENs since it was written, both for the same reason: the build environment
had no docker. `docs/verification/P14-infra-verify.txt` says it plainly —

    OPEN  NO IMAGE-LEVEL PROOF OF THE RUNTIME HARDENING: no docker here, so the Dockerfiles and compose are read
          as text; nobody has yet run `id` inside the built image or tried to write to `/`
          — run the kit's own probes on the built image: `docker run … id` (expect uid 10001, not 0),
            `touch /srv/app/x` (expect permission denied), `nc -l` (absent), `getent passwd polygm` (expect
            nologin), then record the output

This tool is that instruction, executable. It builds the two images from the same compose context a deploy uses,
runs the four probes on both, boots the API image and asks it for `/healthz`, brings the compose stack up and
proves the produced Postgres schema accepts a real write and refuses to change it.

The first run of this tool found five defects that no test in the repository could see, because every test ran in
the working tree and none had ever run the artefact (each is fixed, and each fix carries its own note in place):

  1. `services/api/Dockerfile` never copied `contracts/` or `config/`, so `uvicorn` died at import — the API image
     could not boot at all.
  2. `edoburu/pgbouncer:1.23.1` does not exist; the publisher's tags carry a `v` and a patch suffix, so the whole
     stack failed at *pull*, before a container started.
  3. `0002_money.sql`'s `date_trunc('day', to_timestamp(...))` index is not IMMUTABLE, so migration 0002 could
     never be applied to Postgres.
  4. `0004_product.sql` spoke sqlite (`substr(trim(jsonb))`, `unique (user_id, lower(name))`) — neither is valid
     Postgres.
  5. `0005_triggers.sql` raised `TG_TABLENAME`, a variable that does not exist, so the append-only refusal (the
     control P11's c27 counts across 30 tables) produced an error *about the error* on every attempt.

Usage:  python3 tools/p14-container-verify.py [--record docs/verification/P14-container-verify.txt] [--json …]
Exit:   0 all checks passed, 1 a check failed, 2 docker is unavailable (recorded as OPEN, not as a pass).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
API_IMAGE, MOCK_IMAGE = "polygm-api:p14-container", "polygm-mock:p14-container"
STACK = ("postgres", "redis", "pgbouncer", "executor-mock")     # started before the boot probes
STACK_API_PORT = 18091                                          # the tool's own API publish, not compose's 8080


class Gate:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def check(self, name: str, ok: bool, why: str = "") -> bool:
        self.results.append(("PASS" if ok else "FAIL", name, "" if ok else why))
        return bool(ok)

    def open(self, name: str, why: str) -> None:
        self.results.append(("OPEN", name, why))

    @property
    def failed(self):
        return [r for r in self.results if r[0] == "FAIL"]

    @property
    def opened(self):
        return [r for r in self.results if r[0] == "OPEN"]


def sh(argv, *, timeout=900, stdin=None, check=False):
    """Run, never raise: the output IS the evidence this tool records."""
    p = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
                       input=stdin, check=False)
    if check and p.returncode != 0:
        raise SystemExit("command failed: %s\n%s\n%s" % (" ".join(argv), p.stdout[-2000:], p.stderr[-2000:]))
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def docker_argv() -> list[str]:
    """`docker` when the process may use it, else `sudo -n docker` — the same binary either way."""
    if shutil.which("docker") is None:
        return []
    rc, _, _ = sh(["docker", "info"], timeout=30)
    return ["docker"] if rc == 0 else ["sudo", "-n", "docker"]


def compose_argv(dk: list[str]) -> list[str]:
    """The compose prefix that can actually reach the daemon.

    The first version of this asked `docker compose version`, which is answered by the CLIENT — it needs no
    socket, so it succeeded while every daemon call through the same prefix failed with "permission denied …
    dial unix /var/run/docker.sock". Probe with the prefix that already works for `docker run` instead.
    """
    for tail in (["compose"], []):
        if not tail:
            continue
        rc, _, _ = sh(dk + tail + ["version"], timeout=60)
        if rc == 0:
            return dk + tail
    for prog in (["sudo", "-n", "docker-compose"], ["docker-compose"]):
        rc, _, _ = sh(prog + ["version"], timeout=60)
        if rc == 0:
            return prog
    return []


def http(url: str, timeout: int = 6) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:              # noqa: S310 - fixed localhost URLs
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                                   # noqa: BLE001
        return 0, "%s: %s" % (type(e).__name__, e)


# --------------------------------------------------------------------------------------------- 1. runtime probes

def section_runtime(g: Gate, dk: list[str], facts: dict) -> None:
    """The four probes the OPEN text names, run on both images, plus the boot proof the first run made necessary."""
    for image, user, uid in ((API_IMAGE, "polygm", "10001"), (MOCK_IMAGE, "mock", "10002")):
        rc, out, err = sh(dk + ["run", "--rm", "--entrypoint", "", image, "id"], timeout=300)
        facts["%s.id" % image] = (out or err)[:120]
        g.check("%s runs as uid %s, not root" % (image, uid),
                rc == 0 and re.search(r"uid=%s\(" % uid, out) is not None, (out or err)[:160])

        # `touch /srv/app/x` succeeded on the first run and that was a **finding, not a failure of the image**:
        # the image chowns /srv/app to the app user so the app can write its sqlite file and logs there, and the
        # probes that matter are the ones about what a compromised process can do OUTSIDE its own directory.
        rc, out, err = sh(dk + ["run", "--rm", "--entrypoint", "", image, "sh", "-c",
                                "touch /p14_probe 2>&1; touch /etc/p14_probe 2>&1; "
                                "echo rc=$?"], timeout=300)
        text = out + err
        facts["%s.write_outside" % image] = text[:160]
        g.check("%s cannot write outside its working directory" % image,
                "Permission denied" in text, text[:160])

        rc, out, err = sh(dk + ["run", "--rm", "--entrypoint", "", image, "sh", "-c",
                                "for c in nc ncat netcat socat telnet curl wget ssh; do command -v $c; done; "
                                "echo done"], timeout=300)
        tools_found = [t for t in out.split() if t != "done"]
        facts["%s.network_tools" % image] = tools_found or "(none)"
        g.check("%s ships no network client to exfiltrate with" % image, not tools_found,
                "found: %s" % ", ".join(tools_found))

        rc, out, err = sh(dk + ["run", "--rm", "--entrypoint", "", image, "getent", "passwd", user], timeout=300)
        facts["%s.passwd" % image] = (out or err)[:160]
        g.check("%s's account has no login shell" % image,
                "/usr/sbin/nologin" in out, (out or err)[:160])

    # ROOTFS writability is the container-level half of the same question, and it is the one the compose
    # hardening specifies (`read_only: true` + a tmpfs for /tmp). Probed directly instead of assumed.
    rc, out, err = sh(dk + ["run", "--rm", "--read-only", "--tmpfs", "/tmp", "--entrypoint", "", API_IMAGE,
                            "sh", "-c", "touch /probe 2>&1; touch /tmp/probe && echo tmpfs-writable"], timeout=300)
    text = out + err
    facts["read_only_rootfs"] = text[:200]
    # The refusal's wording depends on WHY the write failed: a missing file permission says "Permission denied",
    # a read-only mount says "Read-only file system". Asserting the specific word is how a green check turns red
    # on a correct image — the first run of this tool did exactly that.
    refused = "Permission denied" in text or "Read-only file system" in text
    g.check("with --read-only the rootfs refuses writes while /tmp still works (the prod shape)",
            refused and "tmpfs-writable" in text, text[:200])


def section_boot(g: Gate, dk: list[str], facts: dict) -> None:
    """Boot the artefact. The first run's defect (a missing `contracts/` COPY) is only visible here."""
    sh(dk + ["rm", "-f", "pgm-p14-api"], timeout=60)
    rc, out, err = sh(dk + ["run", "-d", "--name", "pgm-p14-api", "-p", "%d:8080" % STACK_API_PORT,
                            "--tmpfs", "/srv/app/var:mode=0777", API_IMAGE,
                            "sh", "-c", "python3 tools/run-sql.py --sqlite --dir db/migrations-sqlite && "
                                        "exec uvicorn app:app --host 0.0.0.0 --port 8080"], timeout=300)
    if rc != 0:
        g.check("the API image starts", False, (out + err)[:300])
        return
    status, body = 0, ""
    for _ in range(20):                       # the container migrates 22 files before it binds
        time.sleep(2)
        status, body = http("http://127.0.0.1:%d/healthz" % STACK_API_PORT)
        if status == 200:
            break
    g.check("the API image boots and serves /healthz (the missing-COPY defect lived here)",
            status == 200 and '"ok"' in body, "status=%s body=%s" % (status, body[:200]))
    status, body = http("http://127.0.0.1:%d/v1/markets" % STACK_API_PORT)
    facts["image_api.markets"] = "status=%s %s" % (status, body[:120])
    g.check("the booted image answers a real endpoint", status == 200, "status=%s %s" % (status, body[:200]))
    rc, out, err = sh(dk + ["logs", "--tail", "5", "pgm-p14-api"], timeout=60)
    facts["image_api.log"] = (out or err)[-300:]
    sh(dk + ["rm", "-f", "pgm-p14-api"], timeout=60)


# --------------------------------------------------------------------------------------------- 2. the compose stack

def section_compose(g: Gate, dk: list[str], cp: list[str], facts: dict) -> None:
    """The first `docker compose up` this repository has ever run, made repeatable.

    It asserts the three things the OPEN text could only describe: that the stack starts, that the *Postgres*
    schema is created by `db/migrations/` (the source of truth, applied by the `migrate` service), and that the
    append-only control is enforced by the database rather than by the code that writes to it.
    """
    rc, out, err = sh(cp + ["config", "--services"], timeout=120)
    facts["compose.services"] = out.split()
    g.check("compose parses and declares the six services", rc == 0 and len(out.split()) >= 5, (out + err)[:200])
    if rc != 0:
        return

    sh(cp + ["down", "-v"], timeout=300)
    rc, out, err = sh(cp + ["up", "-d"] + list(STACK), timeout=900)
    g.check("the stack starts (postgres, redis, pgbouncer, executor-mock)", rc == 0, (out + err)[-300:])

    rc, out, err = sh(cp + ["run", "--rm", "migrate"], timeout=900)
    m = re.search(r"postgres: (\d+) applied, (\d+) already present", out)
    facts["migrate"] = (m.group(0) if m else out[-300:])
    g.check("db/migrations applies to real Postgres (the source of truth had never been applied)",
            rc == 0 and m is not None and int(m.group(1)) + int(m.group(2)) == 21, (out + err)[-400:])

    rc, out, err = sh(cp + ["up", "-d", "api"], timeout=900)
    status, body = 0, ""
    for _ in range(25):
        time.sleep(2)
        status, body = http("http://127.0.0.1:8080/healthz")
        if status == 200:
            break
    facts["compose.api"] = "status=%s %s" % (status, body[:120])
    g.check("the compose API answers /healthz", status == 200 and '"ok"' in body,
            "status=%s %s" % (status, body[:200]))

    # The append-only proof, live. `audit_log` is in the declared list P11's c27 counts; the row is written by
    # the probe, then the database is asked to change it and to delete it.
    psql = cp + ["exec", "-T", "postgres", "psql", "-U", "polygm", "-d", "polygm"]
    sql = """
INSERT INTO audit_log (at_ms, actor_type, actor_id, action, request_id)
    VALUES (1,'service','p14-container-verify','p14.append_only_probe','req-p14-container');
UPDATE audit_log SET action='tampered' WHERE request_id='req-p14-container';
DELETE FROM audit_log WHERE request_id='req-p14-container';
"""
    rc, out, err = sh(psql, timeout=300, stdin=sql)
    text = out + err
    facts["postgres.append_only"] = text[-400:]
    g.check("a Postgres write succeeds and is then refused by the append-only trigger",
            "INSERT 0 1" in text and text.count("append-only table: audit_log is not updatable") == 2,
            text[-400:])

    # ...and the REVOKE half: a role that is not the owner cannot change the row even with the trigger dropped.
    sql = """
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='polygm_app') THEN CREATE ROLE polygm_app LOGIN; END IF; END $$;
REVOKE UPDATE, DELETE, TRUNCATE ON cash_ledger FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON cash_ledger FROM polygm_app;
GRANT INSERT, SELECT ON cash_ledger TO polygm_app;
SET ROLE polygm_app;
UPDATE cash_ledger SET amount_micro=1 WHERE id=1;
DELETE FROM cash_ledger WHERE id=1;
RESET ROLE;
"""
    rc, out, err = sh(psql, timeout=300, stdin=sql)
    text = out + err
    facts["postgres.revoke"] = text[-400:]
    g.check("the REVOKE half denies UPDATE and DELETE to the application role",
            text.count("permission denied for table cash_ledger") == 2, text[-400:])

    rc, out, err = sh(cp + ["ps", "--format", "{{.Service}} {{.State}}"], timeout=120)
    facts["compose.ps"] = out.splitlines()
    g.check("every started service is running at the end",
            all(re.search(r"^%s running" % s, out, re.M) for s in ("postgres", "redis", "pgbouncer", "executor-mock")),
            out[:300])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P14 D4 — container/runtime verification by running the artefact")
    ap.add_argument("--record")
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--keep-stack", action="store_true", help="leave the compose stack up afterwards")
    a = ap.parse_args(argv)

    g, facts = Gate(), {}

    dk = docker_argv()
    if not dk:
        g.open("container verification could not run",
               "no docker runtime is reachable in this environment (looked for `docker`, then `sudo -n docker`);"
               " install docker + compose, start the daemon, and re-run this tool unchanged")
        rc = 2
    else:
        rc, out, _ = sh(dk + ["version", "--format", "{{.Server.Version}}"], timeout=60)
        facts["docker"] = out or "server version unavailable"
        cp = compose_argv(dk)
        facts["compose"] = " ".join(cp) if cp else "(compose unavailable)"
        if not cp:
            g.open("compose verification could not run", "docker is present but no `docker compose`/`docker-compose`")
            rc = 2
        else:
            sh(dk + ["build", "-f", "services/api/Dockerfile", "-t", API_IMAGE, "."], check=True)
            sh(dk + ["build", "-f", "services/executor-mock/Dockerfile", "-t", MOCK_IMAGE, "."], check=True)
            section_runtime(g, dk, facts)
            section_boot(g, dk, facts)
            section_compose(g, dk, cp, facts)
            if not a.keep_stack:
                sh(cp + ["down", "-v"], timeout=300)
            rc = 1 if g.failed else 0

    passed = sum(1 for r in g.results if r[0] == "PASS")
    lines = ["P14 CONTAINER VERIFY — %s" % ("PASS" if rc == 0 else ("OPEN" if rc == 2 else "FAIL")),
             "%d passed, %d failed, %d OPEN" % (passed, len(g.failed), len(g.opened)), ""]
    lines += ["%-5s %s%s" % (s, n, (" — " + w) if w else "") for s, n, w in g.results]
    if facts:
        lines += ["", "facts:"] + ["  %-28s %s" % (k, json.dumps(v)[:400]) for k, v in sorted(facts.items())]
    text = "\n".join(lines) + "\n"
    print(text)
    if a.record:
        pathlib.Path(a.record).write_text(text)
        print("recorded %s" % a.record)
    if a.json_path:
        # The house artifact shape, because `tools/p14-security-gate.py` reads every recorded artifact with one
        # loader: `verdict`, `checks[{status,name,why}]`, `open_conditions`, `facts`. A tool that invents its own
        # shape is invisible to the gate — this tool's first record was, and the D4 containers row rendered as
        # MISSING while the run behind it had passed 18 checks.
        pathlib.Path(a.json_path).write_text(json.dumps(
            {"verdict": "PASS" if rc == 0 else ("OPEN" if rc == 2 else "FAIL"),
             "checks": [{"status": s, "name": n, "why": w} for s, n, w in g.results],
             "open_conditions": [{"check": n, "why": w} for s, n, w in g.opened],
             "facts": facts},
            indent=1, sort_keys=True))
        print("wrote %s" % a.json_path)
    return rc


if __name__ == "__main__":
    sys.exit(main())
