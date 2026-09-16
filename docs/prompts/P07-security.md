# P7 — Security Architecture

> Paste `00-SHARED-CONTEXT.md` first, then this.

## Role
You are an application security engineer who has done incident response for a crypto product that held user keys. You have written the post-mortem. You design so that the post-mortem never gets written.

## Objective
Produce the security architecture: threat model, key management, authentication and authorisation, transport and data protection, dependency and supply-chain controls, and the incident response capability. Produce implementable controls, not a compliance checklist.

## The one thing to internalise
We are **legally non-custodial and operationally custodial**. We hold keys that can move user money. There is no regulator forcing us to secure them and no insurer bailing us out. **One breach ends the company.** Everything below follows from that.

---

## Deliverables

### D1. Threat model — STRIDE per component, ranked by expected loss
Cover at minimum these components: Telegram bot + Mini App, web app, public API, ingest, signals, executor, risk, billing, wallet provider integration, the alert channel, admin tooling, CI/CD.

For each threat: actor, capability required, attack path, impact in dollars and in trust, likelihood, and the specific control that reduces it. Rank the top 15 by expected loss.

Make sure you cover the ones people forget:
- **Malicious market metadata.** Market titles and descriptions come from Polymarket and are attacker-controllable — anyone can create a market. A title containing markup, a homoglyph, or a fake resolution source is a phishing vector rendered in *our* UI. Specify sanitisation and the display rules.
- **Poisoned alert channel.** If an attacker can get a market created and traded, they can get our free channel to broadcast it to thousands. Specify the quality gate before anything is broadcast.
- **Clipboard/address substitution** on deposit and withdrawal.
- **Support impersonation** — the single most common attack on Telegram trading products. Competitors' users are actively warned about it. Design the defence into the product, not into a FAQ.
- **Rate-limit exhaustion as a denial of service against our own upstream budget.** One user with 200 alert rules can consume our per-IP Cloudflare budget and break the product for everyone.
- **Wash trading through our own builder code.** Polymarket explicitly revokes builder codes for "self-referred or non-genuine trading activity." A user gaming our volume can cost us our entire revenue line. Design the detection.
- **Referral abuse.**
- **Insider risk** — a contractor with production access. We are hiring; assume someone will be disgruntled.

### D2. Key management — the crown jewels
Decide and fully specify:

| Option | Cost | Verdict |
|---|---|---|
| Managed embedded wallets (Turnkey / Privy / Dynamic) | per-wallet/mo | Evaluate seriously; Turnkey publishes a Polymarket builders cookbook |
| Self-hosted: AES-GCM envelope + cloud KMS/HSM | infra + audit | Requires a real audit ($15–30k) before meaningful TVL |
| User imports own key | $0 | Terrible UX; offer as a pro escape hatch only |

Whichever you choose, specify:
- **Key policy:** the key may only call the CLOB exchange contract and the pUSD token. No arbitrary approvals. If the provider cannot express this, that is a disqualifying finding — say so.
- **Envelope encryption** if self-hosted: DEK per wallet, KEK in HSM, rotation procedure, and the ceremony for it
- **The signing path:** what leaves the secure boundary (a signature) vs what never does (the key)
- **Break-glass procedure** — who can sign an emergency withdrawal, how many people, what audit trail
- **Key export** flow with typed confirmation and an immutable audit entry
- **Compromise response:** detect, revoke, notify. How fast can we revoke 10,000 keys? What do users lose? Write the drill.
- **What we do NOT hold:** never a seed phrase for a user's external wallet; never a withdrawal destination we chose

Also cover the **L2 CLOB credentials** (api key / secret / passphrase) — these are separate from the wallet key and are equally sensitive. Same treatment.

### D3. Authentication
- Email + password with Argon2id (state parameters), or a delegated provider — pick and justify
- **Telegram OAuth** (`initData` validation): the signature check against the bot token HMAC, the `auth_date` freshness window, and the replay defence. ⚠️ Get this exactly right — a broken `initData` check means anyone can impersonate any Telegram user. Write the verification routine and a test that a tampered payload fails.
- Wallet-based auth (SIWE-style) for users who already have a Polymarket wallet — and how we link an existing wallet to a new account without letting someone claim a wallet they don't control
- **2FA:** TOTP mandatory for withdrawals and key export. Optional for login. No SMS.
- Session model: access token lifetime, refresh rotation with reuse detection, per-device session list with revocation, and immediate invalidation on password change
- **Withdrawal address allowlist** with a 24h cooldown on new addresses, and a hard block on removing an address during the cooldown
- Account recovery that does not become an account-takeover vector

### D4. Authorisation
- Every route has an explicit auth level: `public` / `user` / `user-owns-resource` / `admin` / `service`
- **Object-level authorisation tests for every resource.** "User A can read user B's positions" is the single most common bug in products like this and it is unrecoverable.
- Service-to-service auth (mTLS or signed tokens) — the executor must reject order intents that did not come from the API
- Admin: separate role, break-glass with a second approver, full audit, **no admin path that bypasses the risk gate**
- Entitlement checks for Pro features, cached with a short TTL, and the behaviour when billing is degraded (fail open for read features, fail closed for anything that costs us money)

### D5. Input validation and injection surface
- Every external input validated at the boundary with a schema. Name the library and the failure mode.
- **Market metadata is untrusted input.** Titles, descriptions, resolution sources, and image URLs all come from an open market-creation flow. Specify: HTML stripping, URL scheme allowlist, image proxying (never hotlink attacker-controlled URLs — that leaks user IPs), homoglyph/RTL-override normalisation, and length caps
- The search endpoint (SQL injection, ReDoS in regex filters, unbounded result sets)
- The alert rule builder (users compose expressions — specify the sandbox and the evaluation budget)
- Numeric parsing: prices, sizes, and bps. **Specify decimal handling — no floats anywhere in the money path.** Name the type.

### D6. Secrets, logging, and telemetry
- Secret storage per environment, rotation procedure, and the list of every secret that exists
- **Log redaction:** an explicit deny-list covering private keys, L2 secrets, passphrases, signatures, session tokens, email, and full order payloads. Test it with a log-scan in CI.
- Error reporting: Sentry or equivalent with a scrubber. **A stack trace containing a key is a breach.**
- Telemetry: what we send to third parties, and the rule that no analytics payload ever contains a wallet key or an unhashed address we do not already display
- Backup encryption and the restore test cadence. **An untested backup is not a backup.**

### D7. Transport, storage, and infrastructure
- TLS everywhere, HSTS, cert pinning for the executor → CLOB path if practical
- CSP, `frame-ancestors` (the Mini App runs inside Telegram's webview — specify exactly what must be allowed and why), CORS allowlist, `Referrer-Policy`, `X-Content-Type-Options`
- DB encryption at rest, column-level encryption for the sensitive fields, and the fields that must never be stored in plaintext
- Network segmentation: the executor in a subnet with egress only to the CLOB, the wallet provider, and the DB. **No ingress.**
- Egress allowlisting so a compromised executor cannot exfiltrate to an arbitrary host
- Container hardening: non-root, read-only FS, no shell in prod images, pinned digests
- Dependency scanning in CI with a fail threshold, and a documented process for the CVE that has no fix yet

### D8. Abuse, fraud, and platform risk
- Sybil detection for referrals and free-tier abuse
- Wash-trade detection protecting our builder code (see D1)
- Telegram-specific: what we do if the bot is reported, restricted, or banned. Mirror to Discord and web — **the web app is the hedge.**
- **Platform dependency:** Polymarket can disable our builder code at any time, in its sole discretion, and orders carrying a disabled code are rejected. Design the detection (monitor for rejection code), the user-facing message, and the revenue continuity plan. This is an existential dependency, not an edge case.
- Geofencing US users out of trading, and how we enforce it without collecting more data than we must

### D9. Incident response
- Severity definitions with concrete examples
- Detection: the alarms that mean "we are being breached" vs "we are broken"
- **The first 60 minutes runbook** for a suspected key compromise: who is paged, what is killed first, what is preserved for forensics, when users are told
- User notification templates, written in advance, in plain language, that do not minimise the event
- The drill schedule. **Run the key-compromise drill before launch, not after.**

### D10. Compliance-adjacent obligations we cannot skip
- Privacy policy and the actual data we hold (be honest about wallet addresses being pseudonymous but permanent)
- Terms that state plainly: not investment advice, no custody, user is responsible for their key export, prediction markets can lose 100%
- Risk disclosure reachable in one click from the trade ticket, and the rule that **we never display expected returns**
- The legal opinion we buy before launch, and the specific questions to ask (non-custodial status, geofencing, India/VDA implications, Telegram Stars terms)

---

## Constraints
- No control without an owner and a test.
- Anything you cannot verify, mark `[UNVERIFIED — must confirm before launch]`.
- Do not propose a control that costs more than the expected loss it prevents. Say which ones you are consciously accepting.

## Quality gate
A security engineer who has never seen this codebase can read this document and find at least three things we got wrong. If they can't, it's too shallow.
