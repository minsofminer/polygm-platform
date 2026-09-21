/**
 * The route ledger: every API path the shell is allowed to call, and whether the backend actually has it.
 *
 * Why a ledger instead of calling paths inline: P08 designs screens for capabilities the API has not shipped
 * yet (signup, key export, billing, notification settings). A UI that calls a missing route does not fail
 * loudly — it 404s, and someone wires "not found" to an empty state, and the product quietly grows screens
 * that do nothing. So the unbuilt half is declared here, the gate (`tools/p08-gate-check.py` c1) asserts a
 * `built: true` route exists in contracts/openapi.yaml and a `built: false` route does *not*, and every
 * `built: false` entry must be named in docs/P08-frontend-shell.md's launch list.
 *
 * The second half is what keeps this honest when the backend catches up: the moment `/v1/auth/signup` lands,
 * the `built: false` line becomes a lie the gate refuses to compile.
 */
export type RouteDecl = {
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  built: boolean;
  /** What the UI does while the route is missing. "refuses" is the only acceptable production value. */
  whileMissing: "refuses" | "hides";
  /** The launch-list item that owns closing this gap. */
  owner: string;
  note?: string;
  /**
   * `true` means the API's own contract serves this read to nobody in particular (`x-auth: public` or `none`), so
   * the proxy must not require a session for it.
   *
   * Why the flag exists: the proxy keeps the bearer token out of the browser, and it does that by acquiring a
   * session before every upstream call. For a genuinely public read that turns a working link into a 401 — a market
   * link opened in a plain browser has no `initData` and never will, so "sign in to see this" is a bug in the deep
   * link, not a security boundary. The two reads below are the ones a read-only card needs, and only those:
   * `/v1/public/blocks` sits under the same prefix and is `x-auth: admin`, so the obvious shortcut would have opened
   * the one route that mattered.
   */
  anonymous?: true;
};

export const ROUTES = {
  // ---- shipped (P06 trading plane, P07 security plane) -----------------------------------------------
  healthz: { method: "GET", path: "/healthz", built: true, whileMissing: "refuses", owner: "P07" },
  login: { method: "POST", path: "/v1/auth/login", built: true, whileMissing: "refuses", owner: "P07" },
  refresh: { method: "POST", path: "/v1/auth/refresh", built: true, whileMissing: "refuses", owner: "P07" },
  logout: { method: "POST", path: "/v1/auth/logout", built: true, whileMissing: "refuses", owner: "P07" },
  sessions: { method: "GET", path: "/v1/auth/sessions", built: true, whileMissing: "refuses", owner: "P07" },
  revokeSessions: {
    method: "POST",
    path: "/v1/auth/sessions/revoke",
    built: true,
    whileMissing: "refuses",
    owner: "P07",
  },
  telegramAuth: {
    method: "POST",
    path: "/v1/auth/telegram",
    built: true,
    whileMissing: "refuses",
    owner: "P07",
  },
  totpEnroll: { method: "POST", path: "/v1/auth/totp/enroll", built: true, whileMissing: "refuses", owner: "P07" },
  totpVerify: { method: "POST", path: "/v1/auth/totp/verify", built: true, whileMissing: "refuses", owner: "P07" },
  markets: { method: "GET", path: "/v1/markets", built: true, whileMissing: "refuses", owner: "P05" },
  market: { method: "GET", path: "/v1/markets/{market_id}", built: true, whileMissing: "refuses", owner: "P05" },
  // `anonymous` on the book: the contract says `x-auth: none` for it, and the Mini App's read-only card cannot
  // price a market without it. The terminal screens that also read it live under `app/(app)/`, whose layout
  // redirects a signed-out visitor to /sign-in — the page gate, not this hop, is what protects them.
  book: { method: "GET", path: "/v1/markets/{market_id}/book", built: true, whileMissing: "refuses", owner: "P05", anonymous: true },
  fills: { method: "GET", path: "/v1/markets/{market_id}/fills", built: true, whileMissing: "refuses", owner: "P05" },
  tape: { method: "GET", path: "/v1/tape", built: true, whileMissing: "refuses", owner: "P05" },
  // ---- P09's three read surfaces. Each is `built: true` because the gate's c1 asserts a built route is in
  // contracts/openapi.yaml, and each is declared HERE rather than called by path so a screen cannot reach a
  // route the ledger does not know about.
  history: {
    method: "GET",
    path: "/v1/markets/{market_id}/history",
    built: true,
    whileMissing: "refuses",
    owner: "P09",
  },
  holders: {
    method: "GET",
    path: "/v1/markets/{market_id}/holders",
    built: true,
    whileMissing: "refuses",
    owner: "P09",
  },
  // ---- P10's terminal surfaces. Each is `built: true` because the gate's c1 asserts a built route is in
  // contracts/openapi.yaml, and each is named here rather than called by path so a screen cannot reach a route
  // the ledger does not know about. The radar's two routes are `built: false` until the API ships them: the UI
  // refuses with the reason instead of pretending, which is what `whileMissing: "refuses"` means.
  tapeFills: {
    method: "GET",
    path: "/v1/tape/fills",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  tapeFacets: {
    method: "GET",
    path: "/v1/tape/facets",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  whales: {
    method: "GET",
    path: "/v1/whales",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  trader: {
    method: "GET",
    path: "/v1/traders/{anon}",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  copyConfigs: {
    method: "GET",
    path: "/v1/copy/configs",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  createCopyConfig: {
    method: "POST",
    path: "/v1/copy/configs",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  copyGuards: {
    method: "POST",
    path: "/v1/copy/configs/guards",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  // ---- P11 D3: the rating surfaces. `built: true` because the gate's c1 asserts a built route is in
  // contracts/openapi.yaml, and the contract has all four (checked by `tools/p11-gate-check.py` c1). Notes are
  // deliberately one line each: this ledger is loaded by EVERY route, so its copy is on every route's budget.
  leaderboardRank: { method: "GET", path: "/v1/leaderboard/rank", built: true, whileMissing: "refuses", owner: "P11" },
  leaderboardCompare: { method: "GET", path: "/v1/leaderboard/compare", built: true, whileMissing: "refuses", owner: "P11" },
  // D4: the account's own standing on every board, and the identity its rows are published under. All three
  // USER-scoped, and all three one line for the same reason the D3 rows are: this ledger is on every route.
  leaderboardMe: { method: "GET", path: "/v1/leaderboard/me", built: true, whileMissing: "refuses", owner: "P11" },
  leaderboardIdentity: {
    method: "GET",
    path: "/v1/leaderboard/identity",
    built: true,
    whileMissing: "refuses",
    owner: "P11",
  },
  leaderboardIdentitySet: {
    method: "POST",
    path: "/v1/leaderboard/identity",
    built: true,
    whileMissing: "refuses",
    owner: "P11",
  },
  leaderboardFollows: {
    method: "GET",
    path: "/v1/leaderboard/follows",
    built: true,
    whileMissing: "refuses",
    owner: "P11",
  },
  leaderboardFollow: {
    method: "POST",
    path: "/v1/leaderboard/follows",
    built: true,
    whileMissing: "refuses",
    owner: "P11",
  },
  // P11 D6: the public pages. Five rows for six operations: `/v1/public/blocks` is a READ and a WRITE with
  // different status sets, and only the read is reachable from a screen — the write is an operator's lever
  // (admin token), so it is deliberately NOT in this ledger. A screen that could block an address is a screen
  // that can silence a reader.
  publicTraderPage: {
    method: "GET",
    path: "/v1/public/trader/{handle}",
    built: true,
    whileMissing: "refuses",
    owner: "P11",
    // `x-auth: public` in the contract, like the market page: the route ledger is where "nobody needs a session
    // for this" is declared once, and both transports read that declaration (see src/auth/anonymous.ts).
    anonymous: true,
  },
  publicMarketPage: {
    method: "GET",
    path: "/v1/public/market/{slug}",
    built: true,
    whileMissing: "refuses",
    owner: "P11",
    // `x-auth: public` in the contract, and the read that resolves a deep link's slug. A link that only works for
    // somebody who already has a session is a link that fails for the person a shared alert was sent to.
    anonymous: true,
  },
  publicBoardPage: {
    method: "GET",
    path: "/v1/public/leaderboard/{board}",
    built: true,
    whileMissing: "refuses",
    owner: "P11",
    // `x-auth: public` in the contract, like the market page: the route ledger is where "nobody needs a session
    // for this" is declared once, and both transports read that declaration (see src/auth/anonymous.ts).
    anonymous: true,
  },
  // `x-auth: public`, and the one read a crawler makes with no cookies at all.
  publicSitemap: { method: "GET", path: "/v1/public/sitemap", built: true, whileMissing: "refuses", owner: "P11", anonymous: true },
  publicBlocks: { method: "GET", path: "/v1/public/blocks", built: true, whileMissing: "refuses", owner: "P11" },
  // P11 D5: the referral programme. Five rows, and the split is the model's: the terms are PUBLIC (a programme
  // whose terms are discovered after the money moves is a complaint), the link and the funnel are USER-scoped,
  // and the two operator routes are not in this ledger at all — a screen must never be able to reach the accrual
  // run or the review queue, so they are not declared here for a component to call.
  referralsTerms: {
    method: "GET",
    path: "/v1/referrals/terms",
    built: true,
    whileMissing: "refuses",
    owner: "P11",
    // `x-auth: public`, and the comment above says why: the terms are the one part of the programme that must be
    // readable before anything is joined. Declared here so the two transports agree about it.
    anonymous: true,
  },
  referralsMe: {
    method: "GET",
    path: "/v1/referrals/me",
    built: true,
    whileMissing: "refuses",
    owner: "P11",
  },
  referralCode: {
    method: "POST",
    path: "/v1/referrals/code",
    built: true,
    whileMissing: "refuses",
    owner: "P11",
  },
  referralApply: {
    method: "POST",
    path: "/v1/referrals/apply",
    built: true,
    whileMissing: "refuses",
    owner: "P11",
  },
  copySources: {
    method: "GET",
    path: "/v1/copy/sources",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  copyMonitor: {
    method: "GET",
    path: "/v1/copy/configs/monitor",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  portfolio: {
    method: "GET",
    path: "/v1/me/portfolio",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  whaleViews: {
    method: "GET",
    path: "/v1/whale-views",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  createWhaleView: {
    method: "POST",
    path: "/v1/whale-views",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  walletRadar: {
    method: "POST",
    path: "/v1/radar/runs",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  walletRadarJob: {
    method: "GET",
    path: "/v1/radar/runs/{job_id}",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  // ---- P10 D8/D9: the automation and alert surfaces. Both are `built: true` because the API serves them as
  // of the D8/D9 work, and both are declared here rather than called by path so the gate's c1 holds the screens
  // and the contract to the same list.
  automations: {
    method: "GET",
    path: "/v1/automations",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  createAutomation: {
    method: "POST",
    path: "/v1/automations",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  automationPreview: {
    method: "POST",
    path: "/v1/automations/preview",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  automationGuards: {
    method: "POST",
    path: "/v1/automations/guards",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  automationRuns: {
    method: "GET",
    path: "/v1/automations/runs",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  automationTemplates: {
    method: "GET",
    path: "/v1/automations/templates",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  alerts: {
    method: "GET",
    path: "/v1/alerts",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  createAlert: {
    method: "POST",
    path: "/v1/alerts",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  alertTest: {
    method: "POST",
    path: "/v1/alerts/test",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  alertDeliveries: {
    method: "GET",
    path: "/v1/alerts/deliveries",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  alertSettings: {
    method: "POST",
    path: "/v1/alerts/settings",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
  },
  event: {
    method: "GET",
    path: "/v1/events/{event_id}",
    built: true,
    whileMissing: "refuses",
    owner: "P09",
  },
  createOrder: { method: "POST", path: "/v1/orders", built: true, whileMissing: "refuses", owner: "P06" },
  // P12 · the ticket's route: a slug, a side and a budget. The server resolves the outcome token, re-reads the best
  // ask and prices the order — which is the only honest way for a ticket to trade, and the reason the ticket no
  // longer posts `{market_id, amount_cents}` to `/v1/orders` (a 422 on every attempt, from P08 to P12).
  orderByAmount: { method: "POST", path: "/v1/orders/amount", built: true, whileMissing: "refuses", owner: "P12" },
  // The same server-priced order for the Mini App. A separate key from `orderByAmount` because the paths differ —
  // one is authorised by the site's session, the other by the session a signed `initData` minted — while both land
  // on the same conversion and the same risk gate.
  telegramOrder: { method: "POST", path: "/v1/telegram/order", built: true, whileMissing: "refuses", owner: "P12" },
  intent: { method: "GET", path: "/v1/orders/intents/{intent_id}", built: true, whileMissing: "refuses", owner: "P06" },
  addressList: {
    method: "GET",
    path: "/v1/wallet/withdrawal-addresses",
    built: true,
    whileMissing: "refuses",
    owner: "P07",
  },
  addressAdd: { method: "POST", path: "/v1/wallet/withdrawal-addresses/add", built: true, whileMissing: "refuses", owner: "P07" },
  addressRemove: {
    method: "POST",
    path: "/v1/wallet/withdrawal-addresses/remove",
    built: true,
    whileMissing: "refuses",
    owner: "P07",
  },

  // ---- designed in P08, not yet served --------------------------------------------------------------
  signup: { method: "POST", path: "/v1/auth/signup", built: false, whileMissing: "refuses", owner: "P08-L1" },
  passwordReset: {
    method: "POST",
    path: "/v1/auth/password/reset",
    built: false,
    whileMissing: "refuses",
    owner: "P08-L6",
  },
  passwordChange: {
    method: "POST",
    path: "/v1/auth/password/change",
    built: false,
    whileMissing: "refuses",
    owner: "P08-L6",
  },
  recoveryCodes: {
    method: "POST",
    path: "/v1/auth/totp/recovery-codes",
    built: false,
    whileMissing: "refuses",
    owner: "P08-L7",
  },
  // P12 D6 served all six of these, so the launch-list owners (P08-L8/L9/L10) are retired and the ledger says what
  // is true. `built: true` is not a claim about *quality*: the P08 gate pairs each one against an operation in
  // `contracts/openapi.yaml`, so a row that flipped without the route existing fails the gate rather than the user.
  keyExport: { method: "POST", path: "/v1/wallet/keys/export", built: true, whileMissing: "refuses", owner: "P12" },
  balance: { method: "GET", path: "/v1/wallet/balance", built: true, whileMissing: "refuses", owner: "P12" },
  deposit: { method: "POST", path: "/v1/wallet/deposit/quote", built: true, whileMissing: "refuses", owner: "P12" },
  // The progress read is the sixth wallet route and the only one the P08 ledger never declared — the deposit screen
  // cannot be built without it, so it arrives here with the screens rather than being called inline by a component.
  depositProgress: {
    method: "GET",
    path: "/v1/wallet/deposit/{deposit_id}",
    built: true,
    whileMissing: "refuses",
    owner: "P12",
  },
  withdraw: { method: "POST", path: "/v1/wallet/withdraw", built: true, whileMissing: "refuses", owner: "P12" },
  transactions: {
    method: "GET",
    path: "/v1/wallet/transactions",
    built: true,
    whileMissing: "refuses",
    owner: "P12",
  },
  entitlement: { method: "GET", path: "/v1/billing/entitlement", built: false, whileMissing: "refuses", owner: "P08-L11" },
  stripeCheckout: {
    method: "POST",
    path: "/v1/billing/stripe/checkout",
    built: false,
    whileMissing: "refuses",
    owner: "P08-L11",
  },
  starsPurchase: {
    method: "POST",
    path: "/v1/billing/stars/purchase",
    built: false,
    whileMissing: "refuses",
    owner: "P08-L11",
  },
  invoices: { method: "GET", path: "/v1/billing/invoices", built: false, whileMissing: "refuses", owner: "P08-L12" },
  adminGaming: {
    method: "GET",
    path: "/v1/admin/gaming",
    built: true,
    whileMissing: "refuses",
    owner: "P11",
  },
  adminGamingDecide: {
    method: "POST",
    path: "/v1/admin/gaming/decide",
    built: true,
    whileMissing: "refuses",
    owner: "P11",
  },
  referrals: { method: "GET", path: "/v1/billing/referrals", built: false, whileMissing: "refuses", owner: "P08-L12" },
  apiKeys: { method: "GET", path: "/v1/account/api-keys", built: false, whileMissing: "refuses", owner: "P08-L13" },
  notificationPrefs: {
    method: "PUT",
    path: "/v1/account/notifications",
    built: false,
    whileMissing: "refuses",
    owner: "P08-L14",
  },
  tradingDefaults: {
    method: "PUT",
    path: "/v1/account/trading-defaults",
    built: false,
    whileMissing: "refuses",
    owner: "P08-L14",
  },
  displayPrefs: {
    method: "PUT",
    path: "/v1/account/display",
    built: false,
    whileMissing: "refuses",
    owner: "P08-L14",
  },
  deleteAccount: {
    method: "POST",
    path: "/v1/account/delete",
    built: false,
    whileMissing: "refuses",
    owner: "P08-L15",
  },
  linkWallet: {
    method: "POST",
    path: "/v1/wallet/link",
    built: false,
    whileMissing: "refuses",
    owner: "P08-L16",
  },
  globalSearch: { method: "GET", path: "/v1/search", built: false, whileMissing: "hides", owner: "P08-L17" },
} satisfies Record<string, RouteDecl>;

export type RouteKey = keyof typeof ROUTES;

export function isBuilt(key: RouteKey): boolean {
  return ROUTES[key].built;
}

export function missingRoutes(): [RouteKey, RouteDecl][] {
  return (Object.entries(ROUTES) as [RouteKey, RouteDecl][]).filter(([, d]) => !d.built);
}
