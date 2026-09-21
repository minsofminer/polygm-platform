/**
 * The trade sheet's rules, tested where they are decided.
 *
 * The four that would cost money if they were wrong: the fee and the worst case are integer arithmetic and agree
 * with the Python side; a confirm key is minted once and reused; a refusal *clears* the key so a retry is possible;
 * and closing mid-flight is refused, because an order that is in the world must not be dismissable.
 */
import { describe, expect, it, vi } from "vitest";
import {
  amountFindings, blockers, chooseSize, close, confirmCopy, confirmKey, feeMicro, fromMicro, initial,
  maxLossMicro, normaliseAmount, open, plainRefusal, primaryAction, priceFromText, READ_ONLY_SENTENCE, refused,
  screenFor, sharesFor, sharesText, SIZES, submitted, toMicro, validAmount, type MarketView,
} from "./trade";

const MARKET: MarketView = {
  slug: "fed-cut-sept",
  marketId: "m-1",
  question: "Will the Fed cut rates in September 2026?",
  yesAsk: "62.0¢",
  noAsk: "38.5¢",
  spread: "1.0¢",
  ageText: "as of 4 seconds ago",
  closesText: "30 Sep",
  minSizeMicro: "5000000",
  endsSoon: false,
};

describe("shares, as text", () => {
  it("truncates, groups, and prints what the chat prints", () => {
    // The three examples both languages agree on. A rounding formatter would say "80.65" and "0.5"; the chat says
    // "80.64" and "0.50", and a surface that disagrees with the message about the same fill is the bug this pair of
    // assertions exists to prevent. The Python twin is pinned in tests/test_telegram_ops_api.py.
    expect(sharesText(80_645_161)).toBe("80.64");
    expect(sharesText(1_290_000_000)).toBe("1,290");
    expect(sharesText(500_000)).toBe("0.50");
    // Under a hundredth of a share truncates away entirely: 1.005 shares prints "1", exactly as the chat does.
    // That is the same rule read the other way — this product never shows a fraction of a share it cannot honour.
    expect(sharesText(1_005_000)).toBe("1");
    expect(sharesText(1_010_000)).toBe("1.01");
    expect(sharesText(0)).toBe("0");
  });
});

describe("money, in integers", () => {
  it("parses decimals into micros without touching a float", () => {
    expect(toMicro("50")).toBe(50_000_000);
    expect(toMicro("12.5")).toBe(12_500_000);
    expect(toMicro("0.01")).toBe(10_000);
    expect(toMicro("nonsense")).toBe(0);
  });

  it("takes the 1% fee floored to a cent, the same number the chat quotes", () => {
    // 50.00 → 0.50; the Python side is `_fee_estimate` and the P12 gate compares the two implementations.
    expect(feeMicro("50")).toBe(500_000);
    expect(fromMicro(feeMicro("50"))).toBe("0.50");
    expect(fromMicro(feeMicro("12.34"))).toBe("0.12");
  });

  it("calls the whole stake the worst case, because in a prediction market that is the truth", () => {
    expect(maxLossMicro("50")).toBe(50_000_000);
    expect(fromMicro(maxLossMicro("50"))).toBe("50.00");
  });

  it("turns an amount into shares at the shown price, floored like the server floors", () => {
    // 50.00 at 0.62 = 80.645161…, floored to 80.645161 shares — the API test asserts the same integer.
    expect(sharesFor("50", "62.0¢")).toBe(80_645_161);
    expect(priceFromText("62.0¢")).toBe(620_000);
    expect(priceFromText("—")).toBe(0);
    expect(sharesFor("50", "—")).toBe(0);
  });

  it("normalises what a thumb types on a numeric keyboard", () => {
    expect(normaliseAmount("050")).toBe("50");
    expect(normaliseAmount("50.")).toBe("50");
    expect(normaliseAmount(" 12.50 ")).toBe("12.50");
  });
});

describe("the amount input", () => {
  it("accepts the chips' values and plain typed amounts", () => {
    for (const size of SIZES) expect(validAmount(size)).toBe(true);
    expect(validAmount("12.50")).toBe(true);
  });

  it("refuses zero, symbols and absurdity, each with its own sentence", () => {
    expect(amountFindings("0")[0]).toContain("not an order");
    expect(amountFindings("$50")[0]).toContain("plain number");
    expect(amountFindings("50,00")[0]).toContain("plain number");
    expect(amountFindings("999999")[0]).toContain("above");
    expect(amountFindings("")[0]).toContain("Enter");
  });
});

describe("the state machine", () => {
  it("opens on the side the user tapped", () => {
    const s = open(initial("yes"), "no");
    expect(s.step).toBe("size");
    expect(s.side).toBe("no");
  });

  it("mints the confirm key once and reuses it for a retry", () => {
    const mint = vi.fn(() => "abc123");
    const first = confirmKey(chooseSize(open(initial(), "yes"), "50"), mint);
    expect(first.actionKey).toBe("tma-abc123");
    expect(first.step).toBe("submitting");
    const again = confirmKey(first, mint);
    expect(again.actionKey).toBe("tma-abc123");
    expect(mint).toHaveBeenCalledTimes(1);
  });

  it("clears the key on a refusal, so the retry is a real retry", () => {
    // The server abandons a refused key precisely so the same intent can be retried; a sheet that kept it would
    // answer the user's second attempt with IDEM_CONFLICT against its own refusal.
    // `DAILY_CAP` is the code the venue's gate actually returns for a daily-limit refusal; `RISK_DAILY` was one of
    // the invented names the first version of the table used.
    const refusedState = refused(confirmKey(chooseSize(open(initial(), "yes"), "50"), () => "k1"), "DAILY_CAP",
                                 plainRefusal("DAILY_CAP"));
    expect(refusedState.actionKey).toBe("");
    expect(refusedState.step).toBe("refused");
    expect(refusedState.error).toContain("daily limit");
  });

  it("refuses to close while an order is in flight", () => {
    const flying = confirmKey(chooseSize(open(initial(), "yes"), "50"), () => "k1");
    expect(close(flying).step).toBe("submitting");
    expect(close(submitted(flying, "oi-1")).step).toBe("closed");
  });

  it("binds the MainButton label to the current step", () => {
    expect(primaryAction(initial("no"))?.label).toBe("Buy NO");
    expect(primaryAction(open(initial("yes"), "yes"))?.kind).toBe("open");
    expect(primaryAction(chooseSize(open(initial(), "yes"), "100"))).toEqual({ label: "Confirm $100",
                                                                               kind: "confirm" });
    expect(primaryAction(confirmKey(chooseSize(open(initial(), "yes"), "50"), () => "k"))).toBeNull();
    expect(primaryAction(refused(chooseSize(open(initial(), "yes"), "50"), "BAD_AMOUNT", "x"))?.kind).toBe("retry");
    expect(primaryAction(submitted(initial(), "oi-1"))?.kind).toBe("done");
  });

  it("keeps the amount the user chose when it opens again", () => {
    let s = chooseSize(open(initial(), "yes"), "100");
    s = close(s);
    s = open(s, "no");
    expect(s.amountUsdc).toBe("100");
  });
});

describe("what the confirm sheet says", () => {
  it("states the size, the price, the fee, the worst case and the age of the quote", () => {
    const copy = confirmCopy(chooseSize(open(initial(), "yes"), "50"), MARKET);
    // "80.64", not "80.65": 50 USDC at 0.62 is 80.645161 shares and this product truncates. The chat prints the
    // same string for the same fill (`_tg_shares`), and `tests/test_telegram_ops_api.py` pins that side — the pair
    // is the property, and the webview reading "80.65" while the fill message read "80.64" is the seam bug.
    expect(copy).toContain("50 USDC buys about 80.64 YES shares at 62.0¢");
    expect(copy).toContain("Fee 0.50 USDC");
    expect(copy).toContain("worst case you lose 50.00 USDC");
    expect(copy).toContain("as of 4 seconds ago");
    expect(copy).toContain("the order does not go");
  });

  it("does not promise an outcome anywhere", () => {
    const copy = confirmCopy(chooseSize(open(initial(), "yes"), "50"), MARKET);
    for (const word of ["guaranteed", "sure thing", "will definitely"]) expect(copy).not.toContain(word);
  });

  it("blocks a market with no quotes at all", () => {
    expect(blockers({ endsSoon: false, yesAsk: "—", noAsk: "—" }).join(" ")).toContain("No quotes");
    expect(blockers({ endsSoon: false, yesAsk: "62.0¢", noAsk: "38.0¢" })).toEqual([]);
  });

  it("warns when a market is about to resolve", () => {
    expect(blockers({ endsSoon: true, yesAsk: "99.0¢", noAsk: "1.0¢" }).join(" ")).toContain("about to resolve");
  });
});

describe("refusals and deep links", () => {
  it("has a sentence for every code the gate can return, and never repeats the code as the answer", () => {
    // The API's real vocabulary, read out of CODES in `services/api/app.py` during the P12 build and pinned here:
    // the *first* version of this table was keyed on invented `RISK_*` names, so every genuine refusal fell through
    // to the fallback. A pinned list in a test is a list somebody has to update; the alternative was a table that
    // looked like coverage and was not.
    const codes = ["RISK_HALT", "HALTED", "RISK_UNAVAILABLE", "SIGNER_UNAVAILABLE", "MARKET_NOT_ACCEPTING",
                   "NOT_FOUND", "NO_ORDER_BOOK", "BAD_MARKET_META", "STALE_QUOTE", "BAD_SIDE",
                   "BAD_AMOUNT", "ZERO_SIZE", "BELOW_MIN_SIZE", "OVER_ORDER_CAP", "DAILY_CAP", "TOO_MANY_OPEN",
                   "PRICE_FAR_FROM_MID", "OFF_TICK", "UNKNOWN_TICK", "IDEM_CONFLICT", "IDEM_IN_PROGRESS",
                   "IDEM_KEY_REQUIRED", "RATE_LIMITED", "UNAUTHENTICATED", "REFUSED"];
    for (const code of codes) {
      const text = plainRefusal(code);
      expect(text.length).toBeGreaterThan(30);
      expect(text).not.toContain(code);
    }
    expect(plainRefusal("SOMETHING_NEW", "the venue said no")).toBe("the venue said no");
    expect(plainRefusal("SOMETHING_NEW")).toContain("Nothing was placed");
  });

  it("answers a browser with the way to act, not with a wall", () => {
    // The one state with no session and no password to type: there is nothing to sign in *with*, so a sentence that
    // says "sign in" would be the only thing this screen could say wrong twice. It names the market (live) and the
    // route to a trade (the bot), and it never shows the reader a code.
    expect(READ_ONLY_SENTENCE).toContain("Open the bot");
    expect(READ_ONLY_SENTENCE).not.toMatch(/[A-Z_]{4,}/);
    expect(plainRefusal("UNAUTHENTICATED")).toContain("read-only");
    expect(plainRefusal("UNAUTHENTICATED")).toContain("Open the bot");
  });

  it("turns a market deep link into the slug to load", () => {
    expect(screenFor({ kind: "market", marketId: "fed-cut-sept" })).toEqual({ slug: "fed-cut-sept", notice: null });
    expect(screenFor({ kind: "none" }).slug).toBeNull();
    expect(screenFor({ kind: "rejected", reason: "bad-characters" }).notice).toContain("did not look right");
    expect(screenFor({ kind: "referral", code: "AB12" }).notice).toContain("Referral recorded");
  });
});
