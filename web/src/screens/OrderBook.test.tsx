import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { useConnection } from "@/shell/connection";
import { OrderBook, type BookPayload } from "./OrderBook";

/**
 * The three things in this component that a screenshot cannot show, and that a ladder gets wrong in a way which
 * costs money: the one-sided banner's numbers, the stale overlay over (not instead of) the levels, and the
 * re-ladder keeping the reader's place. The 94-ask ladder is P01's measured fixture `0xM9` — every price from
 * 0.001 up, no bids — because a book that is *empty* on one side is the case where a naive render reads as a
 * broken feed.
 */
/** The stamped envelope every read payload carries (contract: `Stamped`), plus the book's own required fields. */
const stamp = { asOf: 1_700_000_000_000, staleAfter: 1_700_000_003_000, cache: { ttlMs: 250, public: true } };
function book(bids: BookPayload["bids"], asks: BookPayload["asks"]): BookPayload {
  return { ...stamp, market: "0xM9", bids, asks, aggregate: "raw", spreadTicks: null, ageMs: 0, oneSided: null };
}

function oneSided(): BookPayload {
  // The API's own fixture, in the API's own units: `shares` is a decimal string of WHOLE shares, so the market's
  // total notional is 0.001 × 21.9e9 shares spread over 94 levels ≈ $21.9M. Getting these units wrong in a test
  // is how a 1000× error ships — the first version of this fixture was micro-shares and happily rendered
  // "232978.7B" shares per level.
  const asks = Array.from({ length: 94 }, (_, i) => {
    const price = "0." + String(i + 1).padStart(3, "0");          // 0.001 .. 0.094, tick-aligned strings
    const whole = Math.trunc(232_978_723 / (i + 1));              // whole shares, integer arithmetic
    const frac = String(Math.trunc(((232_978_723 % (i + 1)) * 1_000_000) / (i + 1))).padStart(6, "0");
    return { price, shares: `${whole}.${frac}`, levels: 1, cumShares: `${whole}.${frac}` };
  });
  return book([], asks);
}

const tradable = () =>
  useConnection.setState({ mode: "ws", connected: true, freshness: "live", blockReason: null });
afterEach(() => useConnection.setState({ mode: "down", connected: false, freshness: "unknown", blockReason: null }));

const level = (price: string, shares: string) => ({ price, shares, levels: 1, cumShares: shares });
const twoSided = book([level("0.40", "1000000000")], [level("0.42", "1000000000")]);

describe("OrderBook", () => {
  it("says a book is one-sided in numbers, not by leaving an area blank", () => {
    render(<OrderBook book={oneSided()} tick="0.001" />);
    const banner = screen.getByTestId("one-sided");
    expect(banner.textContent).toContain("94");
    // ~$21.9M of notional, computed by the same integer arithmetic as the API's `_notional_micro`.
    expect(banner.textContent).toMatch(/\$21[0-9]{6}\./);
    // And it refuses the reading that a low ask with no bids is a discount.
    expect(banner.textContent).toMatch(/asking side|no bid at any price/i);
  });

  it("hands the ticket the venue's own price string, and nothing else", () => {
    // A click on a level only reaches the ticket while trading is allowed at all: the ladder follows the same
    // connection gate as the ticket, so a disconnected reader cannot populate an order from a dead book.
    tradable();
    const onPick = vi.fn();
    render(<OrderBook book={oneSided()} tick="0.001" onPick={onPick} />);
    fireEvent.click(screen.getAllByRole("button", { name: /0?\.001/ })[0]!);
    expect(onPick).toHaveBeenCalledWith("0.001", "ask");
  });

  it("puts a stale book's warning over the levels rather than in place of them", () => {
    const { rerender } = render(<OrderBook book={twoSided} tick="0.01" freshness="live" />);
    expect(screen.queryByTestId("book-stale")).toBeNull();
    rerender(<OrderBook book={twoSided} tick="0.01" freshness="stale" />);
    // The levels are still there: "we cannot see" and "there is nothing" are different states.
    expect(screen.getAllByText(/^\.40$/).length).toBeGreaterThan(0);
    expect(screen.getByTestId("book-stale").textContent).toContain("stale");
  });

  it("flashes a size that changed on a live feed and never on a REST poll", () => {
    const { rerender, container } = render(
      <OrderBook book={twoSided} tick="0.01" source="rest" />,
    );
    const changed = book([level("0.40", "2000000000")], twoSided.asks);
    rerender(<OrderBook book={changed} tick="0.01" source="rest" />);
    expect(container.querySelectorAll('[data-flash]').length).toBe(0);

    rerender(<OrderBook book={twoSided} tick="0.01" source="ws" />);
    rerender(<OrderBook book={changed} tick="0.01" source="ws" />);
    expect(container.querySelectorAll('[data-flash]').length).toBeGreaterThan(0);
  });

  it("keeps the reader's place across a re-ladder, and does not flash one", () => {
    const { container } = render(<OrderBook book={oneSided()} tick="0.001" />);
    const box = container.querySelector<HTMLElement>(".pgm-ladder-scroll")!;
    box.scrollTop = 40;
    fireEvent.click(screen.getByRole("button", { name: "1¢" }));
    expect(box.scrollTop).toBe(40);
    // The market did not move, the arithmetic did: folding the ladder must not flash a single cell.
    expect(container.querySelectorAll('[data-flash]').length).toBe(0);
  });
});
