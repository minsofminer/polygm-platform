import type { RouteKey } from "./routes";

/**
 * The prose half of the route ledger — why each capability is missing, and what owning it means.
 *
 * It lives in its own module on purpose. `src/api/routes.ts` is imported by `src/api/client.ts`, so every key,
 * path and flag in the ledger is fetched by every signed-in document: the notes, which nothing reads at
 * runtime, were 2.9 KB of prose on a phone's first load for a page that never shows them. This file is
 * imported by `tools/p08-gate-check.py` and by `route-notes.test.ts`, never by a screen, so moving the notes
 * here costs the wire nothing.
 *
 * The rule, enforced by the gate and the test: an entry here must name a key in `ROUTES`, and every
 * `built: false` route must have one — an unbuilt capability that nobody explains is a capability that
 * quietly stops being planned.
 */
export const ROUTE_NOTES: Readonly<Partial<Record<RouteKey, string>>> = {
  telegramAuth: "server-side HMAC check only; the browser never parses initData.hash",
  history: "candles from our fills; 6h/1d are derived on the client from the 1h series",
  holders: "tape-derived and pseudonymised; provenance is returned so the rail can say which list it is",
  tapeFills: "the durable fill log: filtered before the limit, and each row carries its own whale threshold",
  tapeFacets: "the window's distribution, every filter's counts, and the rule the whale threshold came from",
  whales: "fills at or above their own market's threshold; the thresholds travel with the feed",
  trader: "four windows of one metric set, the curve with its drawdown, and the methodology",
  copyConfigs: "each config with the pre-confirm slippage warning and the source's own losing windows",
  createCopyConfig: "always a dry run: the schema has no field that turns copying live",
  copyGuards: "going live needs BOTH an acknowledged slippage warning and dry-run history (409 REFUSED otherwise)",
  leaderboardIdentitySet: "the listing toggle writes a consent record",
  // D7. The internal dashboard: the one P11 surface that returns a raw wallet, paired with the pseudonym a human
  // quotes later, and the two buttons whose only effect is an append-only row.
  adminGaming:
    "four detectors, each serving its rule AND the innocent reading of the same shape; the only read that returns a raw wallet, paired with the pseudonym",
  adminGamingDecide:
    "one append-only row carrying the reason and the finding kind; exclude/flag/include/clear, reversible because the boards replay the newest row",
  leaderboardFollows: "a follow is a watch, not a copy config",
  leaderboardFollow: "keyed by pseudonym: an address is not followable",
  copySources: "D7's discovery list, ranked risk-adjusted by default; each row carries its drawdown and its gate",
  copyMonitor: "simulations and fills are separate lists, and every skip carries its reason",
  portfolio: "positions with their share of the book, negRisk groups, and the orders we could not resolve",
  whaleViews: "saved views, each with the rule it is bound to and that rule's fire budget",
  createWhaleView: "a channel makes the view notify, which needs a market target (409 REFUSED without one)",
  walletRadar: "up to ten markets, four rankings, cached and quota-limited - the cache is why a repeat is free",
  walletRadarJob: "the async half: poll the job instead of re-running a scan the user already paid for",
  automations: "rules with their derived status, the halt banner's data, and the builder's own vocabulary",
  createAutomation: "always a dry run: the schema has no field that arms a rule, and there is no expression syntax",
  automationPreview: "the dry run: with a ruleId it is recorded and completes the dry run, without one it is not saved",
  automationGuards: "arm/pause; arming refuses without a completed dry run (DRY_RUN_REQUIRED) or under a loss halt",
  automationRuns: "every evaluation with its reason and the trigger's leaf values: the answer to why it did that",
  automationTemplates: "the 5-minute crypto entry is shown WITH its fee arithmetic when the maths withholds it",
  alerts: "each rule with its cooldown stated as a rule and what quiet hours/digest would do to it right now",
  createAlert: "create or edit inline; the channel's plan is checked at save time, not at fire time",
  alertTest: "a test fire that does not spend the rule's own window, recorded under a test rule id",
  alertDeliveries: "per-channel status and reason; held (digest_scheduled) is not the same fact as refused",
  alertSettings: "quiet hours, digest mode and the default channel; an absent field means leave it",
  event: "the 128-row table plus the negRisk sum/deviation/tolerance",
  addressList: "the list carries no full address by design; the add response is the only reveal",
  addressRemove: "factor-protected: a missing code is a 403 TOTP_REQUIRED, so the screen asks before it sends",
  linkWallet: "needs the signature challenge; P07 refused to consume a proof without one, this is where it is made",
  // D6's five public rows. Their notes are one line each for the same reason every other note here is: this
  // module is read by the gate and by its own test, never by a screen — but a public route is worth a sentence
  // precisely because nothing in the app calls it.
  publicTraderPage: "the row, its nine standings, the qualifiers and the card; a handle that is not listed is a 404",
  publicMarketPage: "the odds with their age, the order-book state and the resolution text as plain text",
  publicBoardPage: "the board's formula, gate and tie-breaks above rows that each carry their own sample size",
  publicSitemap: "handles, boards and markets, with the caps and the truncation served beside the URLs",
  publicBlocks: "operator-only: the live blocks, each with its reason, listed by digest and never by address",
};
