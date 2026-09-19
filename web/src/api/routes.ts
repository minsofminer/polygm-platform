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
    note: "server-side HMAC check only; the browser never parses initData.hash",
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
    note: "candles from our fills; 6h/1d are derived on the client from the 1h series",
  },
  holders: {
    method: "GET",
    path: "/v1/markets/{market_id}/holders",
    built: true,
    whileMissing: "refuses",
    owner: "P09",
    note: "tape-derived and pseudonymised; provenance is returned so the rail can say which list it is",
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
    note: "the durable fill log: filtered before the limit, and each row carries its own whale threshold",
  },
  tapeFacets: {
    method: "GET",
    path: "/v1/tape/facets",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "the window's distribution, every filter's counts, and the rule the whale threshold came from",
  },
  whales: {
    method: "GET",
    path: "/v1/whales",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "fills at or above their own market's threshold; the thresholds travel with the feed",
  },
  trader: {
    method: "GET",
    path: "/v1/traders/{anon}",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "four windows of one metric set, the curve with its drawdown, and the methodology",
  },
  copyConfigs: {
    method: "GET",
    path: "/v1/copy/configs",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "each config with the pre-confirm slippage warning and the source's own losing windows",
  },
  createCopyConfig: {
    method: "POST",
    path: "/v1/copy/configs",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "always a dry run: the schema has no field that turns copying live",
  },
  copyGuards: {
    method: "POST",
    path: "/v1/copy/configs/guards",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "going live needs BOTH an acknowledged slippage warning and dry-run history (409 REFUSED otherwise)",
  },
  copySources: {
    method: "GET",
    path: "/v1/copy/sources",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "D7's discovery list, ranked risk-adjusted by default; each row carries its drawdown and its gate",
  },
  copyMonitor: {
    method: "GET",
    path: "/v1/copy/configs/monitor",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "simulations and fills are separate lists, and every skip carries its reason",
  },
  portfolio: {
    method: "GET",
    path: "/v1/me/portfolio",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "positions with their share of the book, negRisk groups, and the orders we could not resolve",
  },
  whaleViews: {
    method: "GET",
    path: "/v1/whale-views",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "saved views, each with the rule it is bound to and that rule's fire budget",
  },
  createWhaleView: {
    method: "POST",
    path: "/v1/whale-views",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "a channel makes the view notify, which needs a market target (409 REFUSED without one)",
  },
  walletRadar: {
    method: "POST",
    path: "/v1/radar/runs",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "up to ten markets, four rankings, cached and quota-limited - the cache is why a repeat is free",
  },
  walletRadarJob: {
    method: "GET",
    path: "/v1/radar/runs/{job_id}",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "the async half: poll the job instead of re-running a scan the user already paid for",
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
    note: "rules with their derived status, the halt banner's data, and the builder's own vocabulary",
  },
  createAutomation: {
    method: "POST",
    path: "/v1/automations",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "always a dry run: the schema has no field that arms a rule, and there is no expression syntax",
  },
  automationPreview: {
    method: "POST",
    path: "/v1/automations/preview",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "the dry run: with a ruleId it is recorded and completes the dry run, without one it is not saved",
  },
  automationGuards: {
    method: "POST",
    path: "/v1/automations/guards",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "arm/pause; arming refuses without a completed dry run (DRY_RUN_REQUIRED) or under a loss halt",
  },
  automationRuns: {
    method: "GET",
    path: "/v1/automations/runs",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "every evaluation with its reason and the trigger's leaf values: the answer to why it did that",
  },
  automationTemplates: {
    method: "GET",
    path: "/v1/automations/templates",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "the 5-minute crypto entry is shown WITH its fee arithmetic when the maths withholds it",
  },
  alerts: {
    method: "GET",
    path: "/v1/alerts",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "each rule with its cooldown stated as a rule and what quiet hours/digest would do to it right now",
  },
  createAlert: {
    method: "POST",
    path: "/v1/alerts",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "create or edit inline; the channel's plan is checked at save time, not at fire time",
  },
  alertTest: {
    method: "POST",
    path: "/v1/alerts/test",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "a test fire that does not spend the rule's own window, recorded under a test rule id",
  },
  alertDeliveries: {
    method: "GET",
    path: "/v1/alerts/deliveries",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "per-channel status and reason; held (digest_scheduled) is not the same fact as refused",
  },
  alertSettings: {
    method: "POST",
    path: "/v1/alerts/settings",
    built: true,
    whileMissing: "refuses",
    owner: "P10",
    note: "quiet hours, digest mode and the default channel; an absent field means leave it",
  },
  event: {
    method: "GET",
    path: "/v1/events/{event_id}",
    built: true,
    whileMissing: "refuses",
    owner: "P09",
    note: "the 128-row table plus the negRisk sum/deviation/tolerance",
  },
  createOrder: { method: "POST", path: "/v1/orders", built: true, whileMissing: "refuses", owner: "P06" },
  intent: { method: "GET", path: "/v1/orders/intents/{intent_id}", built: true, whileMissing: "refuses", owner: "P06" },
  addressList: {
    method: "GET",
    path: "/v1/wallet/withdrawal-addresses",
    built: true,
    whileMissing: "refuses",
    owner: "P07",
    note: "the list carries no full address by design; the add response is the only reveal",
  },
  addressAdd: { method: "POST", path: "/v1/wallet/withdrawal-addresses/add", built: true, whileMissing: "refuses", owner: "P07" },
  addressRemove: {
    method: "POST",
    path: "/v1/wallet/withdrawal-addresses/remove",
    built: true,
    whileMissing: "refuses",
    owner: "P07",
    note: "factor-protected: a missing code is a 403 TOTP_REQUIRED, so the screen asks before it sends",
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
    note: "needs the signature challenge; P07 refused to consume a proof without one, this is where it is made",
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
