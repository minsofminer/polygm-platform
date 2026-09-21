/**
 * The Mini App's trade sheet, as a state machine — no JSX, no fetch, no bridge.
 *
 * The bot's chat flow and this sheet are the *same* decision made on two surfaces, so this module deliberately
 * mirrors `packages/polygm_core/telegrambot/router.py`'s order path: side → size → confirm → submit → fill or
 * refusal. The differences are the ones the medium forces, and each is written down where it happens:
 *
 *  - **The sheet can show a live price, the chat cannot.** The chat's confirm card re-reads the book at confirm
 *    time and says so; the sheet shows the book it read and puts its age next to it, because a webview can.
 *  - **The MainButton is the confirm.** `bridge.usesMainButton("trade-confirm")` already fixes that vocabulary:
 *    the big bar means "continue", and nothing else in the app may use it. So `primaryAction()` is what the page
 *    binds to the MainButton, and the sheet's own inline button is a *secondary* affordance (change size).
 *  - **Money is integer micros and strings, never floats.** `money.ts` holds the arithmetic because the money
 *    path's rule does not stop at the API boundary: a `0.1 + 0.2` in a webview is the same bug as in the ledger,
 *    and the number the sheet shows must equal the number the gate will price.
 */
import type { StartPayload } from "@/telegram/startapp";

export type Side = "yes" | "no";
export type Step = "closed" | "size" | "confirm" | "submitting" | "placed" | "refused";

export type MarketView = {
  slug: string;
  marketId: string;
  question: string;
  yesAsk: string;          // "62.0¢", already formatted by the server — the client never formats money
  noAsk: string;
  spread: string;
  ageText: string;         // "as of 4 seconds ago"
  closesText: string;
  minSizeMicro: string;    // smallest order, in micro-shares, as a decimal string
  endsSoon: boolean;
};

export type SheetState = {
  step: Step;
  side: Side;
  amountUsdc: string;      // decimal string, the user's input, never parsed to a float
  actionKey: string;       // idempotency key, minted once per confirm and reused on retry
  error?: string;
  refusalCode?: string;
};

export const SIZES = ["25", "50", "100"] as const;
export const MAX_AMOUNT = 100_000;       // a sanity ceiling for the input, not a risk limit: the gate owns those

export function initial(side: Side = "yes"): SheetState {
  return { step: "closed", side, amountUsdc: "50", actionKey: "" };
}

/** Open the sheet on a side. Re-opening resets the amount to the last default but keeps the side the user tapped. */
export function open(state: SheetState, side: Side): SheetState {
  return { ...state, step: "size", side, error: undefined, refusalCode: undefined };
}

export function close(state: SheetState): SheetState {
  // Closing mid-flight is refused rather than obeyed: the order is in the world by then, and a sheet that vanishes
  // while an order is submitting is a user who cannot tell whether they bought anything.
  if (state.step === "submitting") return state;
  return { ...state, step: "closed", error: undefined, refusalCode: undefined };
}

export function chooseSize(state: SheetState, amount: string): SheetState {
  if (!validAmount(amount)) return { ...state, error: "That is not an amount I can use — try 25, 50 or 100." };
  return { ...state, step: "confirm", amountUsdc: normaliseAmount(amount), actionKey: "", error: undefined };
}

export function amountFindings(amount: string): string[] {
  const raw = (amount ?? "").trim();
  if (!raw) return ["Enter an amount."];
  if (!/^\d{1,9}(\.\d{1,2})?$/.test(raw)) return ["Use a plain number, like 50 or 12.50 — no symbols or commas."];
  const whole = Number(raw.split(".")[0]);
  if (whole > MAX_AMOUNT) return [`That is above the ${MAX_AMOUNT} the sheet accepts; larger orders go through /support.`];
  if (whole === 0 && !/[1-9]/.test(raw)) return ["An order of zero is not an order."];
  return [];
}

export function validAmount(amount: string): boolean {
  return amountFindings(amount).length === 0;
}

/** "50." → "50", "050" → "50", "12.5" → "12.5": what the user typed, made predictable, never padded to cents here
 *  (the server formats money, and a client that rounds is a client that disagrees with the ledger). */
export function normaliseAmount(amount: string): string {
  const raw = (amount ?? "").trim().replace(/^0+(?=\d)/, "");
  return raw.endsWith(".") ? raw.slice(0, -1) : raw;
}

/**
 * The fee and the worst case, in integer micros.
 *
 * 1% of notional, floored to a cent — the same arithmetic `router._fee_estimate` does in Python, deliberately
 * duplicated as *integers* on both sides rather than shared: a shared float would agree with itself and still be
 * wrong, and the two implementations are asserted against each other by the P12 gate.
 */
export function feeMicro(amountUsdc: string): number {
  const micro = toMicro(amountUsdc);
  return Math.floor(micro / 100);
}

export function maxLossMicro(amountUsdc: string): number {
  // A prediction-market share pays 1.00 or 0.00, so the worst case for a buy of $50 is $50: there is no stop that
  // saves the position, and a card that showed something smaller would be lying about the shape of the risk.
  return toMicro(amountUsdc);
}

export function toMicro(amountUsdc: string): number {
  const raw = normaliseAmount(amountUsdc);
  if (!/^\d{1,9}(\.\d{1,2})?$/.test(raw)) return 0;
  const [whole, frac = ""] = raw.split(".");
  return Number(whole) * 1_000_000 + Number((frac + "00").slice(0, 2)) * 10_000;
}

export function fromMicro(micro: number): string {
  const m = Math.max(0, Math.trunc(micro));
  return `${Math.trunc(m / 1_000_000)}.${String(Math.trunc((m % 1_000_000) / 10_000)).padStart(2, "0")}`;
}

/** Shares the amount buys at the price shown, in micro-shares — floored, exactly as the server floors it. */
export function sharesFor(amountUsdc: string, priceText: string): number {
  const priceMicro = priceFromText(priceText);
  if (priceMicro <= 0) return 0;
  return Math.trunc((toMicro(amountUsdc) * 1_000_000) / priceMicro);
}

/**
 * `80_645_161` micro-shares → `"80.64"` — the share count as the chat prints it: truncated, never rounded.
 *
 * `toFixed(2)` rounds, and this line used to use it, so the webview said "80.65" about the same fill whose message
 * in the chat said "80.64" (Python's `_tg_shares` truncates deliberately: "never rounded up into a lie"). Rounding a
 * size up tells somebody they hold more than they do; the two surfaces now print the same string for the same fill,
 * and `tests/test_telegram_ops_api.py` pins the Python half of that pair.
 *
 * Integer arithmetic throughout — the micro value never becomes a float, so `toFixed`'s binary rounding cannot move
 * the last digit of a money-adjacent number.
 */
export function sharesText(sharesMicro: number): string {
  const micro = Math.max(0, Math.trunc(sharesMicro || 0));
  const whole = Math.trunc(micro / 1_000_000);
  const frac = Math.trunc((micro % 1_000_000) / 10_000);          // two places, truncated, like `_tg_shares`
  const grouped = whole.toLocaleString("en-US");                  // "1,290" — the chat's grouping separator
  return frac === 0 ? grouped : `${grouped}.${String(frac).padStart(2, "0")}`;
}

/** "62.0¢" → 620000. Kept here rather than in a formatter because this is the one place a display string is turned
 *  back into arithmetic, and it does so by *parsing digits*, never by a float multiply. */
export function priceFromText(text: string): number {
  const m = /^(\d{1,3})(?:\.(\d))?¢$/.exec((text ?? "").trim());
  if (!m) return 0;
  return Number(m[1]) * 10_000 + Number(m[2] ?? "0") * 1_000;
}

/**
 * Mint the confirm key: once per sheet-open, reused by a retry.
 *
 * The reason it is minted here and not per attempt is the whole idempotency story: a user who taps Confirm twice,
 * or a webview that retries after a cold start, must collapse into one order. The `tma-` prefix is what makes it
 * identifiable in the ledger when an incident asks which surface placed something.
 */
export function confirmKey(state: SheetState, mint: () => string): SheetState {
  if (state.actionKey) return state;
  return { ...state, step: "submitting", actionKey: `tma-${mint()}`, error: undefined };
}

export function submitted(state: SheetState, intentId: string): SheetState {
  return { ...state, step: "placed", actionKey: "", error: undefined, refusalCode: undefined, side: state.side,
           amountUsdc: state.amountUsdc, ...(intentId ? {} : {}) };
}

export function refused(state: SheetState, code: string, plain: string): SheetState {
  // The key is CLEARED on a refusal, because the server abandons a refused key precisely so the user can retry the
  // same intent — and a sheet that kept it would answer the retry with IDEM_CONFLICT against its own refusal.
  return { ...state, step: "refused", actionKey: "", refusalCode: code, error: plain };
}

/** What the MainButton does right now, or null when the sheet is closed or mid-flight (both mean "no button"). */
export type PrimaryAction = { label: string; kind: "open" | "confirm" | "retry" | "done" } | null;

export function primaryAction(state: SheetState): PrimaryAction {
  switch (state.step) {
    case "closed":
      return { label: `Buy ${state.side.toUpperCase()}`, kind: "open" };
    case "size":
      return { label: `Continue · $${state.amountUsdc}`, kind: "open" };
    case "confirm":
      return { label: `Confirm $${state.amountUsdc}`, kind: "confirm" };
    case "submitting":
      return null;
    case "refused":
      return { label: "Try again", kind: "retry" };
    case "placed":
      return { label: "Done", kind: "done" };
  }
}

/**
 * The read-only state, in one sentence, so the screen and the refusal cannot disagree.
 *
 * A deep link opened in a plain browser has no Telegram `initData`, so there is no session and nothing can be
 * placed — and "sign in" is not an offer this product can make, because there is no password to sign in with. The
 * sentence therefore names the only route to a trade instead of describing a failure: the market is public, and a
 * visitor who followed a shared alert should be able to read it and then go act on it.
 */
export const READ_ONLY_SENTENCE =
  "Read-only: the market is live, but a trade can only be placed from Telegram. Open the bot and tap Trade.";

/**
 * The refusal, in words, for a code the API returned.
 *
 * The same table exists in Python (`app._tg_plain_refusal`) for the chat, and the duplication is deliberate on both
 * sides: the webview must not fetch a dictionary to render an error, and the two dictionaries are compared by the
 * P12 gate so they cannot drift silently. The fallback is the server's own sentence when it sent one — better than
 * a generic apology, and visible rather than swallowed.
 */
export function plainRefusal(code: string, detail?: string): string {
  const table: Record<string, string> = {
    // The keys are the codes `CODES` in `services/api/app.py` can actually return — not names that read well. The
    // first version of this table was keyed on `RISK_*` names that appear nowhere in the API's vocabulary, so every
    // refusal the venue really produces fell through to the fallback and the phase's "plain language" promise, in
    // the product, was one sentence about a code. The Python twin is keyed on the same list and asserts its own keys
    // are registered codes; the P12 gate compares the two tables by key.
    RISK_HALT: "Trading is paused right now. Nothing you did caused it — try again shortly.",
    HALTED: "Your account is stopped for the day: your own daily-loss limit was hit. Read what happened and lift it.",
    RISK_UNAVAILABLE: "The risk check could not run, so nothing was placed. Try again in a moment.",
    SIGNER_UNAVAILABLE: "Signing is unavailable right now, so nothing can be placed. Try again shortly.",
    MARKET_NOT_ACCEPTING: "That market is not taking orders at the moment. Nothing was placed.",
    NOT_FOUND: "I could not find that market — it may have closed.",
    NO_ORDER_BOOK: "That market has no order book, so there is nothing to trade against.",
    BAD_MARKET_META: "I could not read that market's limits, and I will not guess them. Nothing was placed.",
    STALE_QUOTE: "The book is stale, so I will not price an order from it. Nothing was placed — try again in a moment.",
    BAD_SIDE: "That side is not one this market trades.",
    BAD_AMOUNT: "That amount cannot be turned into a whole number of shares at this price.",
    ZERO_SIZE: "That works out to no shares at all, so there was nothing to place.",
    BELOW_MIN_SIZE: "That is below the smallest order this market accepts.",
    OVER_ORDER_CAP: "That order is larger than the per-order limit on your account.",
    DAILY_CAP: "That would take you past your own daily limit. It resets at midnight UTC.",
    TOO_MANY_OPEN: "You have too many open orders — cancel one and try again.",
    PRICE_FAR_FROM_MID: "That price is too far from the market's current price for the venue to accept. Nothing was placed.",
    OFF_TICK: "That price does not sit on this market's tick size.",
    UNKNOWN_TICK: "I could not read that market's tick size, so I will not guess where a price belongs.",
    IDEM_CONFLICT: "That tap was for a different order than the one on file, so I stopped.",
    IDEM_IN_PROGRESS: "That order is already on its way — no second one was sent.",
    IDEM_KEY_REQUIRED: "That order arrived without a way to tell a retry from a new order, so I did not send it.",
    RATE_LIMITED: "That is more requests than can be sent for you at once. Give it a second.",
    UNAUTHENTICATED: "Nothing was placed: this view is read-only. Open the bot and tap Trade to place an order.",
    REFUSED: "The venue refused the order. Nothing was placed.",
  };
  return table[code] ?? detail ?? `That did not go through (${code}). Nothing was placed.`;
}

/** Where a `startapp` payload lands: the slug to load, or the reason there is nothing to load. */
export function screenFor(payload: StartPayload): { slug: string | null; notice: string | null } {
  switch (payload.kind) {
    case "market":
      return { slug: payload.marketId, notice: null };
    case "trader":
      return { slug: null, notice: "opening a trader…" };
    case "referral":
      return { slug: null, notice: "Referral recorded — here is what is moving today." };
    case "rejected":
      return { slug: null, notice: "That link's payload did not look right, so here is the market list." };
    case "none":
      return { slug: null, notice: null };
  }
}

/**
 * The sentence on the confirm sheet, which is the product's honesty in one string: what will be bought, at what
 * price, what it costs, what it can lose, and how old the price is.
 */
export function confirmCopy(state: SheetState, market: MarketView): string {
  const price = state.side === "yes" ? market.yesAsk : market.noAsk;
  const shares = sharesFor(state.amountUsdc, price);
  return [
    `${state.amountUsdc} USDC buys about ${sharesText(shares)} ${state.side.toUpperCase()} shares at ${price}`,
    `Fee ${fromMicro(feeMicro(state.amountUsdc))} USDC · worst case you lose ${fromMicro(maxLossMicro(state.amountUsdc))} USDC`,
    `${market.ageText} — if the price moves before you confirm, the order does not go`,
  ].join("\n");
}

/** When the sheet must not even open: a market that has stopped trading, or one about to resolve. */
export function blockers(market: Pick<MarketView, "endsSoon" | "yesAsk" | "noAsk">): string[] {
  const out: string[] = [];
  if (market.yesAsk === "—" && market.noAsk === "—") out.push("No quotes on this market right now.");
  if (market.endsSoon) out.push("This market is about to resolve; trading may be halted at any moment.");
  return out;
}
