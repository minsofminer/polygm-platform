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
