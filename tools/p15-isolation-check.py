#!/usr/bin/env python3
"""P15 D2 — is the executor actually isolated, or does the diagram just say so?

    python3 tools/p15-isolation-check.py
    python3 tools/p15-isolation-check.py --record docs/verification/P15-isolation.txt

Three layers have to agree before the claim is true, and each layer fails in a different way:

1. **The platform firewall** (`infra/terraform/network.tf`) — if a later edit adds an `in` rule to the executor's
   firewall, nothing else on this list notices. Checked by parsing the resource block and refusing any inbound rule.
2. **The host rules** (`infra/terraform/cloud-init/executor.yaml`) — the egress allowlist is default-drop with a
   named allowlist. Checked by asserting the drop policy comes first, that the allowed hosts are exactly the venue,
   the wallet provider, the database and the API's wake-up port, and that no blanket accept exists.
3. **The container** (`docker-compose.prod.yml`) — a published port or the edge network would make the first two
   layers irrelevant. Checked structurally, not by grep: the compose file is parsed as YAML and the executor's
   `ports`, `expose` and `networks` are read from the service definition.

The runtime half — a probe from *inside* the subnet trying to leave — needs the real hosts and is therefore an
owner step; P14 D4 already specifies the probe and this harness prints the exact command to run it. The honest
status while that is unrun is CONDITIONAL, and the P15 gate says so rather than this tool guessing.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
NETWORK_TF = ROOT / "infra" / "terraform" / "network.tf"
EXECUTOR_YAML = ROOT / "infra" / "terraform" / "cloud-init" / "executor.yaml"
COMPOSE = ROOT / "docker-compose.prod.yml"

#: The only hosts the executor may reach. Adding one here is a deliberate act with a review attached; the check
#: fails if the terraform or the host script grows a destination that is not on this list.
ALLOWED_HOSTS = ("clob.polymarket.com", "gamma-api.polymarket.com", "data-api.polymarket.com", "api.turnkey.com")

RUNTIME_PROBE = (
    "# on polygm-executor-1, with the stack running:\n"
    "docker compose -f /srv/polygm/docker-compose.prod.yml exec executor sh -c \\\n"
    "  'for h in example.com github.com api.openai.com; do "
    "curl -s -m 3 -o /dev/null -w \"$h %{http_code}\\n\" https://$h || echo \"$h BLOCKED\"; done'\n"
    "# expected: every line BLOCKED, and `grep polygm-egress-drop /var/log/kern.log` shows the attempts.\n"
    "# then the inverse: `curl -sS -o /dev/null -w '%{http_code}\\n' https://clob.polymarket.com/` must answer 200."
)


class Report:
    def __init__(self) -> None:
        self.passes = 0
        self.failures: list[str] = []

    def check(self, ok: bool, what: str, why: str = "") -> bool:
        if ok:
            self.passes += 1
        else:
            self.failures.append("%s%s" % (what, (" — " + why) if why else ""))
        return bool(ok)


def _strip_comments(text: str) -> str:
    """HCL comments removed before any value is matched. A comment must never be able to fail — or pass — a check:
    this tool's own terraform says 'NOT 0.0.0.0/0' in prose, and matching raw text made a correct file red."""
    return "\n".join(re.sub(r"#.*$", "", line) for line in text.splitlines())


def _rule_blocks(text: str) -> list[str]:
    """Every `rule { ... }` body in a firewall resource, brace-matched."""
    blocks, i = [], 0
    while True:
        m = re.search(r"rule\s*\{", text[i:])
        if not m:
            return blocks
        start = i + m.end()
        depth, j = 1, start
        while j < len(text) and depth:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        blocks.append(text[start:j - 1])
        i = j


def hcl_block(text: str, resource: str) -> str:
    """The body of `resource "type" "name" { ... }`, brace-matched. Regex-only parsing is exactly the shortcut that
    lets a rule three lines down slip past a check, so this walks the braces."""
    m = re.search(r'resource\s+"%s"\s+"%s"\s*\{' % tuple(resource.split(".")), text)
    if not m:
        return ""
    i = m.end()
    depth = 1
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[m.end():i - 1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D2 — executor isolation")
    ap.add_argument("--record", help="write the transcript here")
    args = ap.parse_args(argv)
    lines: list[str] = []
    rep = Report()

    def say(msg: str = "") -> None:
        lines.append(msg)

    say("P15 D2 — executor isolation check")
    say("=" * 72)

    # ---------------------------------------------------------------- layer 1: the platform firewall
    say("\n1. platform firewall (infra/terraform/network.tf)")
    tf = _strip_comments(NETWORK_TF.read_text())
    body = hcl_block(tf, "hcloud_firewall.executor")
    rep.check(bool(body), "the executor firewall resource exists")
    if body:
        inbound = re.findall(r'direction\s*=\s*"in"', body)
        rep.check(not inbound, "the executor firewall declares no inbound rule",
                  "found %d: an ingress rule on the host that signs orders is the whole risk in one line" % len(inbound))
        out = re.findall(r'direction\s*=\s*"out"', body)
        rep.check(len(out) >= 4, "the executor firewall declares its egress allowlist (%d rules)" % len(out))
        ports = set(re.findall(r'port\s*=\s*"(\d+)"', body))
        rep.check(ports <= {"53", "443", "5432", "8443", "22"},
                  "egress ports are exactly the ones the job needs",
                  "unexpected: %s" % sorted(ports - {"53", "443", "5432", "8443", "22"}))
    api_body = hcl_block(tf, "hcloud_firewall.api")
    rep.check('source_ips = var.admin_cidrs' in api_body,
              "the API's SSH rule comes from var.admin_cidrs, not 0.0.0.0/0",
              "the same mistake P14 F14 holds Supabase to")
    # The SSH rule is read as a *block*, not a regex window: a window is exactly how the previous version of this
    # check failed on a correct file (the source is a variable reference, not a literal list).
    ssh_rules = [b for b in _rule_blocks(api_body) if re.search(r'port\s*=\s*"22"', b)]
    rep.check(len(ssh_rules) == 1, "the API firewall has exactly one SSH rule",
              "found %d — two rules on the operator port is how one of them quietly becomes 0.0.0.0/0" % len(ssh_rules))
    rep.check(all("0.0.0.0/0" not in b for b in ssh_rules),
              "no 0.0.0.0/0 on the API's SSH port", "the rule is %s" % (ssh_rules,))
    say("   terraform validates: see `make infra-validate` (recorded in the D2 section of docs/P15-deployment.md)")

    # ---------------------------------------------------------------- layer 2: the host rules
    say("\n2. host egress allowlist (infra/terraform/cloud-init/executor.yaml)")
    yml = EXECUTOR_YAML.read_text()
    rep.check("iptables -P OUTPUT DROP" in yml, "the host output policy is DROP")
    rep.check("iptables -P OUTPUT ACCEPT" not in yml, "nothing re-opens the output policy")
    m = re.search(r"ALLOW_HOSTS=\((.*?)\)", yml)
    hosts = tuple(m.group(1).split()) if m else ()
    rep.check(hosts == ALLOWED_HOSTS, "the allowlist is exactly the venue, the wallet provider and the data API",
              "it is %s" % (hosts,))
    rep.check("PGM_DB_HOST" in yml, "the database host is added from the environment rather than hard-coded")
    rep.check("udp --dport 53 -d 1.1.1.1" in yml, "DNS is pinned to named resolvers",
              "an open 53 rule is an exfiltration channel with a friendly name")
    rep.check("--log-prefix \"polygm-egress-drop" in yml, "dropped packets are logged, so the runtime probe has evidence")
    rep.check("ufw default deny incoming" in yml, "the second layer also denies inbound")
    rep.check("OnUnitActiveSec=60" in yml, "the allowlist is re-resolved on a timer",
              "a vendor that moves an edge must not look like silence from the executor")

    # ---------------------------------------------------------------- layer 3: the container
    say("\n3. container (docker-compose.prod.yml)")
    try:
        import yaml  # type: ignore
        compose = yaml.safe_load(COMPOSE.read_text())
    except ImportError:
        compose = None
        rep.check(False, "PyYAML is installed so the compose file can be parsed structurally")
    if compose:
        ex = (compose.get("services") or {}).get("executor") or {}
        rep.check(bool(ex), "the executor service exists")
        rep.check(not ex.get("ports"), "the executor publishes no ports", "published: %s" % (ex.get("ports"),))
        rep.check(not ex.get("expose"), "the executor exposes nothing to the edge network")
        nets = ex.get("networks") or []
        if isinstance(nets, dict):
            nets = list(nets)
        rep.check(nets == ["internal"], "the executor is attached to the internal network only",
                  "attached to %s — the edge network is where inbound traffic lives" % (nets,))
        api_nets = (compose["services"]["api"].get("networks") or [])
        if isinstance(api_nets, dict):
            api_nets = list(api_nets)
        rep.check(set(api_nets) == {"edge", "internal"}, "the API is the only service bridging the two networks")
        rep.check(compose["networks"]["internal"].get("internal") is True,
                  "the internal network has no route off the host")
        rep.check("POLYGM_API_IMAGE" in str(ex.get("image", "")) or "POLYGM_EXECUTOR_IMAGE" in str(ex.get("image", "")),
                  "the executor image comes from the deploy job's digest, never a floating tag")
        caddy = compose["services"].get("caddy") or {}
        rep.check(not (caddy.get("networks") and "internal" in (list(caddy["networks"]) if isinstance(caddy["networks"], dict) else caddy["networks"])),
                  "the public-facing proxy is not on the internal network")

    # ---------------------------------------------------------------- the honest part
    say("\n" + "=" * 72)
    say("Checks passed: %d, failed: %d" % (rep.passes, len(rep.failures)))
    for f in rep.failures:
        say("  FAIL %s" % f)
    say("")
    say("Still owner-run on real infrastructure (the halves a file cannot prove):")
    say(RUNTIME_PROBE)
    status = "PASS (static) — runtime probe PENDING" if not rep.failures else "FAIL"
    say("\nVerdict: %s" % status)

    text = "\n".join(lines) + "\n"
    if args.record:
        pathlib.Path(args.record).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.record).write_text(text)
        print("wrote %s" % args.record)
    print(text if not args.record else text.split("Verdict:")[-1].strip())
    return 1 if rep.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
