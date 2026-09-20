/**
 * The sheet, rendered. What a pure test cannot prove, and each is a P12 rule:
 *
 *  1. the price's **age** is on screen with the price, and the worst case is on the confirm, before the tap;
 *  2. one tap on a chip and one on confirm sends **exactly one** order, with the idempotency key the sheet minted;
 *  3. a refusal is rendered as the gate's sentence plus its code — never as "something went wrong";
 *  4. a fill buzzes once and a refusal buzzes once, and nothing else buzzes;
 *  5. the sheet cannot be dismissed while the order is in flight.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TradeSheet } from "./TradeSheet";
import { READ_ONLY_SENTENCE, type MarketView } from "./trade";

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

/**
 * A fake Telegram bridge, installed *into* the real jsdom window rather than replacing it.
 *
 * The first version of this helper replaced `window` outright and every test in the file failed with "Expected
 * container to be an Element" — because the replacement had no `document`, and `@testing-library` reads the
 * document off `window`. Augmenting is what a real webview does too: it adds `Telegram` to the page's window.
 */
function bridge() {
  const buzzes: string[] = [];
  const calls: { label: string }[] = [];
  const w = window as unknown as Record<string, unknown>;
  w.Telegram = {
    WebApp: {
      initData: "signed",
      notificationOccurred: (t: string) => buzzes.push(t),
      MainButton: {
        setText: (t: string) => calls.push({ label: t }),
        onClick: () => undefined,
        offClick: () => undefined,
        show: () => undefined,
        hide: () => undefined,
      },
    },
  };
  if (!window.matchMedia) {
    w.matchMedia = () => ({ matches: false, addEventListener: () => undefined, removeEventListener: () => undefined });
  }
  return { buzzes, calls };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("TradeSheet", () => {
  it("shows both sides with their prices and the age of those prices", () => {
    bridge();
    render(<TradeSheet market={MARKET} place={vi.fn()} />);
    expect(screen.getByRole("heading", { name: MARKET.question })).toBeTruthy();
    // The price appears twice on purpose — once as the side button, once beside its age — and the assertion is that
    // *both* readings are on screen, because the rule is that a price never appears without its timestamp.
    expect(screen.getAllByText(/YES 62\.0¢/).length).toBeGreaterThan(1);
    expect(screen.getAllByText(/as of 4 seconds ago/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Odds are a market, not a forecast/)).toBeTruthy();
  });

  it("sends exactly one order for one confirm, with the key the sheet minted", async () => {
    bridge();
    const place = vi.fn().mockResolvedValue({ ok: true, intentId: "oi-1" });
    render(<TradeSheet market={MARKET} place={place} />);
    fireEvent.click(screen.getByRole("button", { name: /Buy YES/ }));
    fireEvent.click(screen.getByRole("button", { name: "$50" }));
    expect(screen.getByText(/Fee 0\.50 USDC/)).toBeTruthy();
    expect(screen.getByText(/worst case you lose 50\.00 USDC/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm $50" }));
    await waitFor(() => expect(place).toHaveBeenCalledTimes(1));
    const arg = place.mock.calls[0]![0] as { idempotencyKey: string; amountUsdc: string; side: string };
    expect(arg.idempotencyKey).toMatch(/^tma-[0-9a-f]{8,}$/);
    expect(arg.amountUsdc).toBe("50");
    expect(arg.side).toBe("yes");
  });

  it("renders a refusal as the gate's sentence and its code", async () => {
    bridge();
    const place = vi.fn().mockResolvedValue({ ok: false, code: "DAILY_CAP", detail: "" });
    render(<TradeSheet market={MARKET} place={place} />);
    fireEvent.click(screen.getByRole("button", { name: /Buy NO/ }));
    fireEvent.click(screen.getByRole("button", { name: "$25" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm $25" }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.getByRole("alert").textContent).toContain("daily limit");
    expect(screen.getByRole("alert").textContent).toContain("DAILY_CAP");
  });

  it("buzzes once for a fill and once for a refusal, and never for a tap", async () => {
    const { buzzes } = bridge();
    const place = vi.fn().mockResolvedValueOnce({ ok: true, intentId: "oi-1" })
                         .mockResolvedValueOnce({ ok: false, code: "NO_LIQUIDITY", detail: "" });
    render(<TradeSheet market={MARKET} place={place} />);
    fireEvent.click(screen.getByRole("button", { name: /Buy YES/ }));
    fireEvent.click(screen.getByRole("button", { name: "$50" }));
    expect(buzzes).toEqual([]);                     // choosing a side and a size is not news
    fireEvent.click(screen.getByRole("button", { name: "Confirm $50" }));
    await waitFor(() => expect(buzzes).toEqual(["success"]));
    fireEvent.click(screen.getByRole("button", { name: /Buy YES/ }));
    fireEvent.click(screen.getByRole("button", { name: "$50" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm $50" }));
    await waitFor(() => expect(buzzes).toEqual(["success", "error"]));
  });

  it("keeps the sheet open while the order is in flight", async () => {
    bridge();
    let release: (v: { ok: true; intentId: string }) => void = () => undefined;
    const place = vi.fn(() => new Promise<{ ok: true; intentId: string }>((resolve) => { release = resolve; }));
    render(<TradeSheet market={MARKET} place={place} />);
    fireEvent.click(screen.getByRole("button", { name: /Buy YES/ }));
    fireEvent.click(screen.getByRole("button", { name: "$100" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm $100" }));
    await waitFor(() => expect(screen.getByText(/Placing your order/)).toBeTruthy());
    release({ ok: true, intentId: "oi-9" });
    await waitFor(() => expect(screen.getByText(/Order accepted/)).toBeTruthy());
    expect(place).toHaveBeenCalledTimes(1);
  });

  it("warns instead of pricing when a market has no quotes", () => {
    bridge();
    render(<TradeSheet market={{ ...MARKET, yesAsk: "—", noAsk: "—" }} place={vi.fn()} />);
    expect(screen.getByRole("alert").textContent).toContain("No quotes");
  });
});

describe("read-only", () => {
  it("renders the market and offers the bot instead of a confirm it cannot honour", async () => {
    const { buzzes, calls } = bridge();
    const place = vi.fn();
    render(<TradeSheet market={MARKET} place={place} readOnly />);

    // The market is still legible — prices, the age next to them, the side buttons — because a link that arrives
    // read-only is a market somebody shared, not an error.
    expect(screen.getByText(MARKET.question)).toBeTruthy();
    expect(screen.getByText(/as of 4 seconds ago/)).toBeTruthy();
    fireEvent.click(screen.getByText(`Buy YES ${MARKET.yesAsk}`));
    // A size chip is what takes the sheet to its confirm slot — where the difference has to show.
    fireEvent.click(screen.getByRole("button", { name: "$50" }));

    await waitFor(() => expect(screen.getByText(READ_ONLY_SENTENCE)).toBeTruthy());
    expect(screen.getByText("Open the bot")).toBeTruthy();
    expect(screen.queryByText(/^Confirm \$/)).toBeNull();

    // Nothing was sent, nothing was minted, and nothing buzzed: a view that cannot place an order must not pretend
    // to try — a haptic here would be the product's most expensive lie.
    expect(place).not.toHaveBeenCalled();
    expect(buzzes).toEqual([]);
    expect(calls).toEqual([]);
  });

  it("still binds the MainButton when a session exists", async () => {
    const { calls } = bridge();
    render(<TradeSheet market={MARKET} place={vi.fn()} />);
    fireEvent.click(screen.getByText(`Buy YES ${MARKET.yesAsk}`));
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
  });
});
