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
  book: { method: "GET", path: "/v1/markets/{market_id}/book", built: true, whileMissing: "refuses", owner: "P05" },
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
  keyExport: { method: "POST", path: "/v1/wallet/keys/export", built: false, whileMissing: "refuses", owner: "P08-L8" },
  balance: { method: "GET", path: "/v1/wallet/balance", built: false, whileMissing: "refuses", owner: "P08-L9" },
  deposit: { method: "POST", path: "/v1/wallet/deposit/quote", built: false, whileMissing: "refuses", owner: "P08-L9" },
  withdraw: { method: "POST", path: "/v1/wallet/withdraw", built: false, whileMissing: "refuses", owner: "P08-L10" },
  transactions: {
    method: "GET",
    path: "/v1/wallet/transactions",
    built: false,
    whileMissing: "refuses",
    owner: "P08-L9",
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
