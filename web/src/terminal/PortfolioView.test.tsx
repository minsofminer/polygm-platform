import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { PortfolioView } from "./PortfolioView";
import type { Portfolio, PortfolioPosition } from "./wire";

/**
 * D6's promises as render tests.
 *
 * The one that matters most: a position with no mark shows a sentence, not `$ 0.00` and not `0%`. The fixture
 * is built so that a screen which forgot the check would print both, and both are asserted absent.
 *
 * Every stubbed read carries the clock (`asOf`/`staleAfter`) — a read without one is discarded by the hooks and
 * the screen renders empty, which reads as "element not found" rather than "the fixture was wrong".
 */
const MICRO = 1_000_000;
// The stamp is relative to the wall clock on purpose: this screen renders an "ends in" against `useNow()`, and a
// fixture pinned to a fixed epoch in the past would assert the "ended" branch while claiming to test the live one.
const asOf = Date.now() - 5_000;

function position(over: Partial<PortfolioPosition> = {}): PortfolioPosition {
  return {
    tokenId: "t1",
    marketId: "0xM1",
    marketSlug: "rain",
    question: "Will it rain in Surat on Sunday?",
    outcome: "Yes",
    category: "Weather",
    size: "120.00",
    avgEntry: "0.4",
    mark: "0.55",
    costBasisMicro: 48 * MICRO,
    valueMicro: 66 * MICRO,
    unrealisedMicro: 18 * MICRO,
    unrealisedBps: 3_750,
    onTick: true,
    // A minute of slack, because `endsInText` floors hours: 5h minus a millisecond reads as "4h".
    endsInMs: Date.now() + 5 * 3_600_000 + 60_000,
    markSource: "last_fill",
    shareOfPortfolioBps: 6_600,
    ...over,
  };
}

function portfolio(over: Partial<Portfolio> = {}): Portfolio {
  return {
    positions: [position()],
    negRiskGroups: [
      { eventId: "e1", eventTitle: "Who wins the primary?", legs: 3, exposureMicro: 300 * MICRO, sumValuesMicro: 180 * MICRO, maxPayoutMicro: 120 * MICRO, note: "" },
    ],
    orders: [
      { intentId: "i1", marketId: "0xM1", state: "filled", reason: null, shares: "120.00", price: "0.4000", createdMs: asOf - 60_000, notionalMicro: 48 * MICRO, unknownLifecycle: false },
    ],
    unknownLifecycle: [
      { venueOrderId: "v9", intentId: "i9", state: "unknown", reason: "the venue timed out", showAsWorking: false, atMs: asOf - 1_000, unknownLifecycle: true },
    ],
    pnlCurve: [
      { tsMs: asOf - 172_800_000, cumMicro: 0, peakMicro: 0, drawdownMicro: 0 },
      { tsMs: asOf - 86_400_000, cumMicro: 12 * MICRO, peakMicro: 12 * MICRO, drawdownMicro: 0 },
      { tsMs: asOf, cumMicro: -3 * MICRO, peakMicro: 12 * MICRO, drawdownMicro: 15 * MICRO },
    ],
    maxDrawdownMicro: 15 * MICRO,
    totals: { valueMicro: 66 * MICRO, cashMicro: 20 * MICRO, equityMicro: 86 * MICRO, unrealisedMicro: 18 * MICRO, costBasisMicro: 48 * MICRO },
    benchmark: { kind: "hold_pusd", valueMicro: 100 * MICRO, rateBps: 0, note: "holding pUSD: the deposited cash at 1.0000, unchanged." },
    csv: { columns: ["intentId", "marketId", "state", "shares", "price", "notionalMicro", "createdMs"], note: "columns are in micro-USDC where the name says Micro" },
    emptyState: "/markets - browse markets and place a first order to start a portfolio",
    asOf,
    staleAfter: asOf + 2_000,
    ...over,
  };
}

function stub(book: Portfolio) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(JSON.stringify(book), { status: 200, headers: { "content-type": "application/json" } })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the portfolio screen", () => {
  it("shows the marked position, its share of the book, and the exit", async () => {
    stub(portfolio());
    render(<PortfolioView initial={portfolio()} />);
    await waitFor(() => expect(screen.getByText("Will it rain in Surat on Sunday?")).toBeInTheDocument());
    expect(screen.getByText("66%")).toBeInTheDocument();
    expect(screen.getByText("5h")).toBeInTheDocument();               // ends-in, from the position's own clock
    expect(screen.getByText(/exit 120\.00 @ 0\.55 on the market screen/)).toBeInTheDocument();
  });

  it("says a mark is missing instead of marking the position at zero", async () => {
    const blind = portfolio({ positions: [position({ markSource: "unknown", mark: "0", unrealisedMicro: 0, unrealisedBps: 0, valueMicro: 0 })] });
    stub(blind);
    render(<PortfolioView initial={blind} />);
    await waitFor(() => expect(screen.getByText(/no price to mark it at/)).toBeInTheDocument());
    expect(screen.getByText(/the size and the cost below are real/)).toBeInTheDocument();
    expect(screen.getByText(/PnL is not knowable yet/)).toBeInTheDocument();
    // The lie this guards: an unknown mark rendering as a total loss, or as a 0.00% move.
    expect(screen.queryByText("$ 0.00")).not.toBeInTheDocument();
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
  });

  it("draws the drawdown region and the benchmark line under the cash curve", async () => {
    stub(portfolio());
    const { container } = render(<PortfolioView initial={portfolio()} />);
    await waitFor(() => expect(container.querySelector(".pgm-chart-line")).not.toBeNull());
    expect(container.querySelector(".pgm-chart-drawdown")).not.toBeNull();
    // Cash curve, not equity: the label has to say which, because the unrealised PnL is not in it.
    expect(screen.getByText(/cumulative cash \(realised\)/)).toBeInTheDocument();
    expect(screen.getByText(/Unrealised moves are in the table above, not in this line/)).toBeInTheDocument();
    // Two dashed lines: zero and the pUSD benchmark — both named in the text around the chart.
    expect(container.querySelectorAll(".pgm-chart-zero").length).toBe(2);
    expect(screen.getByText(/holding pUSD/)).toBeInTheDocument();
  });

  it("keeps the order we could not resolve, and states why in text on the row", async () => {
    stub(portfolio());
    render(<PortfolioView initial={portfolio()} />);
    await waitFor(() => expect(screen.getByText(/we do not model/)).toBeInTheDocument());
    expect(screen.getByText(/the venue timed out/)).toBeInTheDocument();
  });

  it("groups the negRisk legs with the event-level exposure", async () => {
    stub(portfolio());
    render(<PortfolioView initial={portfolio()} />);
    await waitFor(() => expect(screen.getByText("Who wins the primary?")).toBeInTheDocument());
    expect(screen.getByText(/3 legs of one event/)).toBeInTheDocument();
    expect(screen.getByText(/at most .*120\.00.* can pay/)).toBeInTheDocument();
  });

  it("points an empty portfolio at the markets page", async () => {
    const empty = portfolio({ positions: [], negRiskGroups: [], pnlCurve: [], totals: { valueMicro: 0, cashMicro: 0, equityMicro: 0, unrealisedMicro: 0, costBasisMicro: 0 } });
    stub(empty);
    render(<PortfolioView initial={empty} />);
    await waitFor(() => expect(screen.getByRole("link", { name: /Browse markets/ })).toBeInTheDocument());
    expect(screen.getByRole("link", { name: /Browse markets/ })).toHaveAttribute("href", "/markets");
    // An empty portfolio says so; it does not render a blank rectangle.
    expect(screen.getByText(/No positions yet/)).toBeInTheDocument();
  });
});
