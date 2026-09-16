# P14 — Security Testing & Assurance

> Paste `00-SHARED-CONTEXT.md` and the P7/P13 outputs first, then this.

## Role
You are a penetration tester who specialises in crypto products that hold user keys. You are paid to find the thing that ends the company, and you have no interest in being polite about it.

## Objective
Produce and execute the security assurance programme: threat-driven penetration testing, key-compromise drills, dependency and supply-chain auditing, and the pre-launch security gate. Find the problems before a user's money finds them.

## Context to keep in view
We are legally non-custodial and operationally custodial. There is no regulator and no insurer. **The realistic failure modes, in order of likelihood: a broken authorisation check, a leaked key in logs or an error report, a compromised dependency, a social-engineering win against support, and an insider with production access.**

---

## Deliverables

### D1. Penetration test plan — scoped by expected loss
Prioritised test cases, each with: objective, method, expected result, severity if it fails, and the remediation owner.

**Authorisation (highest priority — the most common catastrophic bug in this product class)**
- [ ] User A reads user B's positions, orders, wallet address, alert rules, PnL, key-export status
- [ ] User A cancels user B's order
- [ ] User A modifies user B's copy config or automation rule
- [ ] User A withdraws to their own address from user B's wallet
- [ ] User A triggers user B's key export
- [ ] Unauthenticated access to every `user`-level route (enumerate them all, test them all)
- [ ] IDOR across every resource ID in the API — fuzz the IDs, do not just try the obvious ones
- [ ] Privilege escalation: user → admin, by any path
- [ ] Service-to-service: can the public API be made to submit an order intent that did not originate from a real user?
- [ ] **Does any admin path bypass the risk gate?** (It must not.)

**Authentication**
- [ ] `initData` HMAC bypass: forged, truncated, replayed, expired, signed with the wrong token
- [ ] Password reset flow: enumeration, token reuse, token predictability, race conditions
- [ ] Session fixation, refresh-token reuse after rotation, logout-everywhere completeness
- [ ] 2FA bypass on withdrawal and key export — try every path into those actions, not just the happy one
- [ ] Wallet-link: can I claim a wallet I do not control?
- [ ] Withdrawal allowlist: add + immediate withdraw (must be blocked by cooldown), remove during cooldown, allowlist entry with a homoglyph address

**Trading**
- [ ] Order at a price off the tick grid — rejected, or silently rounded? (Silent rounding is a loss.)
- [ ] Order below `minimum_order_size`
- [ ] Negative size, zero size, enormous size, size that overflows the decimal type
- [ ] Manipulated client-side price/size between quote and submit (TOCTOU)
- [ ] Replay of a signed order
- [ ] Concurrent submits that each individually pass the risk gate but jointly exceed the limit (**race the risk gate — this is a classic**)
- [ ] Idempotency-key reuse with a different payload
- [ ] Automation rule that circumvents a limit a manual order cannot
- [ ] Copy-trade cascade: 500 copiers on one source wallet, one fill — do we blow the signer rate bucket or our own risk limits?

**Injection & input**
- [ ] SQL injection on every parameter, especially search, filters, and the alert rule builder
- [ ] Stored XSS via market metadata — **anyone can create a Polymarket market, so titles and descriptions are attacker-controlled and render in our UI.** Test titles containing markup, script, event handlers, `javascript:` URLs, SVG payloads, RTL overrides, and zero-width characters.
- [ ] XSS via wallet pseudonyms and bios (also upstream-controlled)
- [ ] SSRF via image URLs, webhook URLs, resolution-source links
- [ ] ReDoS in the rule builder and search filters
- [ ] Prototype pollution in any JSON-merging code
- [ ] CSV injection in the tax export (a formula in a market title executing in Excel is a real attack)

**Business logic & fraud**
- [ ] Self-referral (⚠️ an explicit Polymarket builder-code revocation ground — this protects our entire revenue line)
- [ ] Referral Sybil: N wallets funded from one source
- [ ] Wash trading through our builder code — can a user inflate our attributed volume and get our code disabled?
- [ ] Free-tier abuse at scale (200 alert rules, thousands of watchlist entries) exhausting our per-IP Polymarket budget for everyone
- [ ] Entitlement bypass: Pro features accessed after cancellation, or without payment
- [ ] Stripe/Stars webhook forgery and replay

### D2. Key-compromise drills — run these before launch, not after
Each drill: scenario, stopwatch, expected time to contain, and the report template.
1. **One user key leaked.** Detect → revoke → notify that user → verify no further movement. Target time?
2. **The signing service compromised.** Full break-glass: revoke all keys, halt trading, notify all users. **Time every step.** If this takes more than an hour, the architecture is wrong.
3. **A key found in a log.** Who can see the log store, how fast is it rotated, what is the blast radius, and what is the notification obligation?
4. **A contractor leaves.** Access revocation across every system, verified by a checklist signed by a second person. Test it while they are still employed.
5. **Support impersonation.** A fake admin DMs a user asking for their key export. What in the product stops it? (See P12 D6.)
6. **The wallet provider has an outage or a breach.** What do our users lose, and what is our dependency on their status page?

### D3. Static and dynamic analysis pipeline
- SAST in CI with a fail threshold and a triage process for the findings that are not exploitable (document the triage, do not just silence them)
- Secret scanning on every commit **and on all history**, with pre-commit hooks and a CI check. Include the specific patterns for private keys and Polymarket L2 secrets.
- **Log redaction test:** a CI job that scans test-run logs against a deny-list (private keys, L2 secrets, passphrases, signatures, session tokens). A stack trace containing a key is a breach — prove it cannot happen.
- DAST against a staging deployment on every nightly
- IaC scanning
- Container image scanning with pinned digests, and a documented process for the CVE with no fix
- Dependency review on every bump: who published it, when, download count, whether it is typosquattable. **The npm/PyPI supply chain is how products like this actually get robbed.**

### D4. Infrastructure & configuration review
- The executor subnet has no ingress and egress only to the CLOB, the wallet provider, and the DB. **Verify it, do not assume it.**
- Egress allowlisting actually blocks an arbitrary outbound connection from a compromised executor — test by trying
- No shell in production images, non-root, read-only filesystem
- Database not reachable from the public internet
- Backups encrypted, and **the restore actually tested** with a documented result
- Cloud IAM: least privilege, no long-lived root credentials, MFA on every human account, break-glass procedure tested
- CSP and `frame-ancestors` correct for the Telegram webview — and not so loose that they are useless
- CORS allowlist contains no wildcards

### D5. Rate limiting and abuse testing
- Per-user, per-IP, per-endpoint limits verified by actually exceeding them
- The **own-rate-budget DoS**: 100 aggressive users must not exhaust our per-IP Polymarket budget and break the product for everyone
- Alert-fanout amplification
- Login and password-reset throttling with lockout that cannot be used to lock out a victim
- Cost-amplification: any endpoint where one request causes N expensive upstream calls

### D6. Third-party assurance
- **The audit decision:** managed wallet provider (they are audited; verify their report is current) vs self-hosted keys (we need our own audit, $15–30k, and it must happen before meaningful TVL). State the threshold at which the audit becomes mandatory.
- Bug bounty: scope, exclusions, reward table, and the response SLA. Launch it *after* the internal pentest, not instead of it.
- The legal opinion we buy before launch, with the specific questions written out (non-custodial status, geofencing, Telegram's terms, India/VDA exposure)

### D7. Pre-launch security gate
A single document, signed off, that must be green before real money:
- [ ] All Critical and High findings remediated and re-tested
- [ ] Every authorisation test in D1 passing
- [ ] All six key-compromise drills run, with times recorded
- [ ] Log-redaction CI check passing
- [ ] Secret scan clean on all history
- [ ] Executor network isolation verified by test
- [ ] Backup restore tested within the last 30 days
- [ ] Incident response runbook written, and the on-call rotation staffed
- [ ] Risk disclosure, terms, and privacy policy reviewed by counsel
- [ ] Kill switch drilled
- [ ] **Canary: $50 of our own money, real orders, full reconciliation, for 72 hours**

### D8. Continuous assurance
- Quarterly re-test of the D1 authorisation matrix
- Dependency updates on a schedule, not when something breaks
- Chaos drills on a cadence, with results published internally
- A **security changelog** — every control added or removed, with the reason
- Post-incident review process with blameless writeups and tracked remediation

---

## Constraints
- No finding closed without a re-test.
- No "we'll fix it after launch" on anything that touches keys or authorisation.
- Every drill has a recorded time. An untested runbook is a hypothesis.

## Quality gate
Run the drills. Report the times. If the full break-glass takes longer than one hour, or if any authorisation test fails, **the product does not launch** — and you say so in writing rather than softening the result.
