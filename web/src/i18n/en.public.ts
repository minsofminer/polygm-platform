/**
 * The public pages' dictionary — the third route family.
 *
 * `/trader/<handle>`, `/market/<slug>` and `/leaderboard/<board>` are read by people who have never opened the
 * app, by crawlers, and by whoever the link was pasted to. That audience decides the split: this file is loaded
 * by the three public routes and by nothing else, so the terminal's 319 keys stay out of a page whose whole job
 * is to answer in one request, and this file's copy stays out of every signed-in document.
 *
 * `scripts/i18n-check.mjs` holds the rule that makes the split safe — a `public.*` key asked for by a component
 * that does not import `@/i18n/public` renders the key itself, and the check fails the build on it.
 */
export const enPublic: Record<string, string> = {
  "public.card.footnoteLabel": "what qualifies this number",
  "public.chrome.backToBoard": "the boards",
  "public.chrome.methodology": "how this is computed",
  "public.chrome.shareNote":
    "this page is public because a handle was listed; the rank is the same row the trader sees privately",
  "public.trader.title": "@{handle} on Openout",
  "public.trader.lede": "rank {rank} of {total} on {board}",
  "public.trader.unranked": "not on a board yet",
  "public.trader.standing": "every board this trader is ranked on",
  "public.trader.headlineRealised": "realised, after fees",
  "public.trader.headlineDrawdown": "worst drawdown",
  "public.trader.headlineSettled": "settled markets",
  "public.trader.headlineWinRate": "win rate",
  "public.trader.winRateGated": "behind the sample gate",
  "public.trader.updated": "ranked on data as of {when}",
  "public.market.title": "{question} — odds and volume on Openout",
  "public.market.odds": "odds",
  "public.market.age": "quoted {age}",
  "public.market.volume": "24h volume",
  "public.market.liquidity": "liquidity",
  "public.market.openInterest": "open interest",
  "public.market.accepting": "this market is accepting orders",
  "public.market.closed": "this market is closed to new orders",
  "public.market.outcomes": "outcomes",
  "public.market.resolution": "how this market resolves",
  "public.market.endDate": "closes {when}",
  "public.board.title": "{board} leaderboard — the rules and the rows on Openout",
  "public.board.formula": "the formula",
  "public.board.gate": "who is eligible",
  "public.board.tieBreaks": "how ties break",
  "public.board.rank": "rank",
  "public.board.trader": "trader",
  "public.board.score": "score",
  "public.board.settled": "settled",
  "public.board.winRate": "win rate",
  "public.board.drawdown": "drawdown",
  "public.board.window": "window {window}",
  "public.board.excluded": "{count} wallets are excluded from this board by the integrity rules",
  "public.board.blewUp": "{count} wallets on this board lost more than they deposited and are shown, not dropped",
  "public.board.empty": "this board has no rows yet",
  "public.board.sample": "sample {count}",
  "public.state.errorTitle": "we cannot load this page",
  "public.state.errorBody": "the service answered with an error, so nothing on this page would be a fact",
  "public.state.notFound": "no such page",
};
