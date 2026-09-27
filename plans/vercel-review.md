# Vercel optimization audit — the run, and the blocker that stopped it

Method: `vercel-labs/agent-skills`' `vercel-optimize` v1.2.0 — its own pipeline: collect production signals,
scan the codebase, gate candidates, investigate only what the gate promotes, verify citations, render. The skill's
doctrine is explicit and is the reason this document exists in this shape:

> Metrics first. Recommendations start from Vercel production signals, not repo-wide grep.

This run **stopped at the first step**, and the honest record of a stopped audit is more useful than a page of
recommendations invented from a grep. What follows is the blocker, the evidence, the scan that did run, and what
the skill's own rules say about the findings that scan produced.

---

## 1. Blocker: the Vercel credential no longer reaches the project

| Step | Result |
|---|---|
| `vercel --version` | 60.1.3 — installed for this audit, meets the skill's v53+ prerequisite |
| `collect-signals.mjs` | `FAILED: NOT_AUTH: run vercel login` |
| `vercel whoami` | `Error: Not authorized: Trying to access resource under scope "miners4". You must re-authenticate to this scope or use a token with access to this scope.` |
| `GET /v2/teams` | `{"teams":[]}` — the token sees no teams |
| `GET /v9/projects/prj_Jp3ES1my6xPofpCWWXYs8D4Oh8ka?teamId=team_CXIJ9RpnYma4N3nDVMzE8DiV` | **403** |
| `GET /v2/user` | `minsofminer`, `defaultTeamId: team_CXIJ9RpnYma4N3nDVMzE8DiV` — and `web/.vercel/project.json` names that same team as `orgId` |

So the credential authenticates as a **user** and no longer has access to the **team** its own default points at.
That is `forbidden` in the skill's blocker taxonomy, whose required action is *fix auth/team scope; do not pitch
Observability Plus*. It is not a missing product feature and it is not a code problem: it is an owner action.

**Why this matters beyond the audit.** This is the same token the Mini App deploys with. `polygm-mini-app` is live
and serving, so nothing is broken right now — but **a redeploy from this workspace cannot authenticate**, which
makes the token a launch-readiness item in its own right, alongside the Supabase token that is returning 401. Two
credentials in this environment have now lost access without anyone rotating them; both are recorded as owner
actions rather than worked around.

**Owner action.** Re-issue or re-authorize a Vercel token for the `miners4` team scope
(`team_CXIJ9RpnYma4N3nDVMzE8DiV`), put it in `.secrets/tokens.env`, and this audit re-runs from step 1 with real
metrics. **Observability Plus is also required** for route-level metric-backed recommendations — worth checking
while re-authenticating, because without it the pipeline can only ever produce the platform-scope half.

## 2. What did run: the skill's own scanner

`scripts/scan-codebase.mjs web` needs no credentials. It reports **192 files, 36 routes, 15 scanners, 18 findings**,
preserved verbatim at `docs/verification/vercel-scan.json` so the next run can diff it instead of re-deriving it:

| Pattern | Count | Where |
|---|---|---|
| `force-dynamic` | 17 | every account route (`/alerts`, `/automation`, `/terminal`, `/portfolio`, `/radar`, …) **and** five public ones (`/market/[market]`, `/trader/[who]`, `/markets`, `/whales`, the leaderboard routes) |
| `headers-in-page` | 1 | `app/layout.tsx:39` — `cookies()`, which is what makes the whole tree dynamic |

## 3. Why none of those became recommendations — the skill's rule, applied to itself

Every one of the 18 findings carries `trafficIndependent: false`. The skill is unambiguous about that:

> Scanner findings are supplementary. Drop findings annotated `COLD-PATH` or `NO-ROUTE-MAPPING` unless the scanner
> declares `metadata.trafficIndependent === true`. … Route-local cache or data-fetch patterns need route-level
> traffic evidence.

A `force-dynamic` route is exactly a route-local rendering pattern, and whether it *costs* anything depends on
traffic that only the blocked metrics can supply. On this product the split is obvious to a human — five public
pages are crawler-and-stranger traffic that could be cacheable, and the account routes genuinely need cookies —
but "obvious to a human" is what the skill exists to avoid, and there is no route-level evidence to stand the
claim on. **So the scan's findings are recorded and none of them is promoted.** That is the audit behaving
correctly under a blocker, not the audit failing to find something.

## 4. The watch-list (explicitly *not* recommendations)

If a reader wants to know where to point the metrics once the token works, this is the list — pending evidence,
not advice:

* **The five public routes** — `/market/[market]`, `/trader/[who]`, `/markets`, `/whales`, `/leaderboard/[board]/{c,w}` — render dynamically and read through `publicRead`, which carries `cache: "no-store"` on purpose. They are the only routes whose audience is strangers and crawlers, so they are the only place where a cache policy could be named and justified. Whether it *should* be is a freshness-semantics question the product has already decided: a price must be able to say it is old, and a page cached at the edge cannot compute an age at render time. Any change here has to move the freshness computation and be argued on its own merits — it is not a free win and it is not this audit's to make from a scanner line.
* **`app/layout.tsx`'s `cookies()`** — the app reads the theme and density cookie in the root layout, which is what makes the entire tree dynamic. That is a deliberate P08 decision (dark-first with no first-paint flash, and the comment says so). It is also the single highest-leverage fact on this list, because it bounds what any route-level caching can ever do without moving theming to the client — which would reintroduce the flash the decision was made to prevent. Recorded as a *trade*, not a defect.

## 5. `[UNVERIFIED]`, and what would end it

* **Every cost and performance claim about the deployed project.** No usage, no contract, no route metrics, no Core Web Vitals: the collector never got past authentication. Nothing in this document estimates a saving, and the skill forbids customer-facing `$N` figures in any case.
* **Whether Observability Plus is enabled** for the project — the check requires the same access that is failing.

Both end the moment the token is re-authorized: `collect-signals.mjs` → `gate-investigations.mjs` →
`deep-dive.mjs` → briefs → verify → render, all of which are already vendored at
`skills/vercel-agent-skills/skills/vercel-optimize/`.

## What this pass did not touch

No product code changed. This is a **blocked** audit, and its deliverable is the blocker, the evidence, the
preserved scan, and the owner action — the same treatment the Supabase 401 has had since it appeared.
