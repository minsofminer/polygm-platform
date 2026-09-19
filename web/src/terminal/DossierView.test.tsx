import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { TraderDossierView } from "./DossierView";
import type { TraderDossier } from "./wire";

/**
 * The D3 acceptance claim, as a render test: the screen a user lands on must show the win rate *with its sample*
 * or refuse to show it at all, must show the drawdown beside the PnL, and must carry every behaviour label's rule.
 *
 * The API's own tests prove the server sends those fields; this proves the screen renders them rather than
 * dropping one on the floor — which is the failure a screenshot of a green table hides.
 */
const MICRO = 1_000_000;
const asOf = 1_700_000_000_000;

function dossier(over: Partial<TraderDossier> = {}): TraderDossier {
  const window_ = {
    fills: 120,
    resolvedMarkets: 4,
    wins: 3,
    winRateBps: 7_500,
    insufficientSample: true,
    sampleNote: "insufficient sample: 4 settled markets; a win rate needs 20",
    sampleGate: 20,
    volumeMicro: 12_000 * MICRO,
    realisedMicro: 340 * MICRO,
    unrealisedMicro: -12 * MICRO,
    bestMicro: 200 * MICRO,
    worstMicro: -80 * MICRO,
    maxDrawdownMicro: 150 * MICRO,
    avgHoldMs: 4 * 3_600_000,
    medianHoldMs: 2 * 3_600_000,
    openFills: 6,
    matchedPositions: 3,
    distinctMarkets: 4,
    categories: 2,
    asOfMs: asOf,
  };
  return {
    anonWallet: "w_5c037b2ab8",
    window: "30d",
    windows: ["7d", "30d", "90d", "all"],
    sampleGate: 20,
    metrics: { "7d": window_, "30d": window_, "90d": window_, all: window_ },
    curve: [
      { tsMs: 1, cumMicro: 100 * MICRO, peakMicro: 100 * MICRO, drawdownMicro: 0 },
      { tsMs: 2, cumMicro: -50 * MICRO, peakMicro: 100 * MICRO, drawdownMicro: 150 * MICRO },
    ],
    curveWindow: "30d",
    maxDrawdownMicro: 150 * MICRO,
    breakdown: [{ category: "Politics", notionalMicro: 100 * MICRO, realisedMicro: 10 * MICRO, shareBps: 2_350 }],
    positions: [
      { marketId: "0xM1", marketSlug: "fed", question: "Fed cuts in March?", outcome: "Yes", size: "1200",
        avgEntry: "0.42", mark: "", unrealisedMicro: 0, unrealisedBps: 0, onTick: true, endsInMs: 0,
        markSource: "unknown" as const },
    ],
    fills: [
      { tsMs: asOf - 90_000, conditionId: "0xC1", tokenId: "1", marketId: "0xM1", marketSlug: "fed",
        question: "Fed cuts in March?", category: "Politics", tick: "0.01", side: "BUY", outcome: "Yes",
        price: "0.5", shares: "1200", notionalMicro: 600 * MICRO, anonWallet: "w_5c037b2ab8", labels: [],
        source: "venue", lagMs: 120, thresholdMicro: 500 * MICRO, thresholdRule: "whale = max(...)",
        thresholdReason: "relative" as const, severity: "notice" as const, ratioBps: 12_000,
        rule: "severity is a ratio", isWhale: true, winner: null, resolved: false, realisedMicro: 0 },
    ],
    behaviour: [
      { label: "wash-roundtrips", confidence: 810, publishable: true,
        rule: "bought and sold the same outcome within 60 seconds more than 5 times",
        disclaimer: "may be market-making rather than wash trading; not an accusation" },
      // A label without its rule must not reach the screen: this one is dropped by the renderer.
      { label: "insider-suspect", confidence: 700, publishable: true, rule: "", disclaimer: "" },
    ],
    methodology: { path: "/docs/P10", winRate: "wins over settled markets", drawdown: "peak minus cumulative" },
    asOf,
    staleAfter: asOf + 5_000,
    ...over,
  };
}

function stubRead(payload: TraderDossier) {
  // The client goes through `request()` -> fetch; one stub, and the screen's own hook fills it.
  vi.stubGlobal("fetch", vi.fn(async () =>
    new Response(JSON.stringify(payload), { status: 200, headers: { "content-type": "application/json" } }),
  ));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the dossier screen", () => {
  it("refuses to print a win rate under the gate, and says why instead", async () => {
    stubRead(dossier());
    render(<TraderDossierView anon="w_5c037b2ab8" />);
    await waitFor(() => expect(screen.getByText("insufficient sample")).toBeInTheDocument());
    expect(screen.getByText(/a win rate needs 20/)).toBeInTheDocument();
    expect(screen.queryByText("75%")).toBeNull();
  });

  it("prints the percentage once the window clears the gate, with the settled count beside it", async () => {
    const gated = dossier();
    const window30 = gated.metrics["30d"] as NonNullable<(typeof gated.metrics)["30d"]>;
    gated.metrics["30d"] = { ...window30, winRateBps: 7_631, insufficientSample: false, resolvedMarkets: 40, sampleNote: "" };
    stubRead(gated);
    render(<TraderDossierView anon="w_5c037b2ab8" />);
    await waitFor(() => expect(screen.getByText("76.31%")).toBeInTheDocument());
    expect(screen.getByText(/40 settled markets/)).toBeInTheDocument();
  });

  it("draws the PnL line and its drawdown overlay, and states the worst drawdown in money", async () => {
    stubRead(dossier());
    const { container } = render(<TraderDossierView anon="w_5c037b2ab8" />);
    await waitFor(() => expect(container.querySelector(".pgm-chart-drawdown")).not.toBeNull());
    expect(container.querySelector(".pgm-chart-line")).not.toBeNull();
    expect(screen.getByText(/worst drawdown/)).toBeInTheDocument();
    expect(screen.getByText(/high-water mark/)).toBeInTheDocument();
  });

  it("shows a behaviour label only with its rule and disclaimer, and drops the one without", async () => {
    stubRead(dossier());
    render(<TraderDossierView anon="w_5c037b2ab8" />);
    await waitFor(() => expect(screen.getByText(/bought and sold the same outcome/)).toBeInTheDocument());
    // Twice on purpose: once on the badge, once in the disclaimers list at the foot of the screen. A reader who
    // scrolls to either place gets the same caveat.
    expect(screen.getAllByText(/not an accusation/).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("insider-suspect")).toBeNull();
  });

  it("says the address is not resolvable rather than offering an explorer link", async () => {
    stubRead(dossier());
    render(<TraderDossierView anon="w_5c037b2ab8" />);
    await waitFor(() => expect(screen.getByText(/does not resolve/)).toBeInTheDocument());
    expect(screen.queryByText(/Etherscan/i)).toBeNull();
  });

  it("marks an unsettled fill as open instead of showing a break-even zero", async () => {
    stubRead(dossier());
    render(<TraderDossierView anon="w_5c037b2ab8" />);
    await waitFor(() => expect(screen.getByText("open")).toBeInTheDocument());
    // The position's mark is absent, and "no mark" is words rather than a price of zero.
    expect(screen.getByText("no mark")).toBeInTheDocument();
  });

  it("switches the whole metric set when the window changes", async () => {
    const payload = dossier();
    const w7 = payload.metrics["7d"] as NonNullable<(typeof payload.metrics)["7d"]>;
    const w30 = payload.metrics["30d"] as NonNullable<(typeof payload.metrics)["30d"]>;
    payload.metrics["7d"] = { ...w7, realisedMicro: 5 * MICRO, winRateBps: 9_000, insufficientSample: false, resolvedMarkets: 30, sampleNote: "" };
    payload.metrics["30d"] = { ...w30, realisedMicro: -400 * MICRO, winRateBps: 4_000, insufficientSample: false, resolvedMarkets: 44, sampleNote: "", maxDrawdownMicro: 900 * MICRO };
    stubRead(payload);
    render(<TraderDossierView anon="w_5c037b2ab8" />);
    await waitFor(() => expect(screen.getByText("40%")).toBeInTheDocument());
    expect(screen.getByText("-$ 400.00")).toBeInTheDocument();
    screen.getByRole("button", { name: "7 days" }).click();
    await waitFor(() => expect(screen.getByText("90%")).toBeInTheDocument());
    expect(screen.getByText("+$ 5.00")).toBeInTheDocument();      // the money layer's signed form
  });
});
