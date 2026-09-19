import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CopyView } from "./CopyView";

/**
 * D7's on-screen order, as render tests: the ranking is stated before the list, the warning is rendered before
 * the confirm, and the create button will not submit a form the API would refuse.
 *
 * Every stubbed read carries the clock (`asOf`/`staleAfter`): an unstamped body is discarded by the hooks and the
 * screen renders empty, which reads as "element not found" rather than "the fixture was wrong".
 */
const asOf = Date.now() - 1_000;
const MICRO = 1_000_000;

const sourceRow = {
  anonWallet: "w_5c037b2ab8",
  windowDays: 30,
  closedTrades: 31,
  realisedMicro: 1_900 * MICRO,
  feesMicro: 12 * MICRO,
  netAfterFeesMicro: 1_888 * MICRO,
  maxDrawdownMicro: 1_900 * MICRO,
  longestLosingStreak: 6,
  avgLatencyMs: 1_350,
  winRateBps: 5_806,
  insufficientSample: false,
  sampleNote: "",
  sampleGate: 20,
  riskAdjustedBps: 9_936,
  riskAdjustedRule: "net after fees per unit of drawdown",
  copierCount: 2,
  currentlyCopying: false,
  myConfigs: 0,
  updatedMs: asOf,
  rank: 1,
};

// A DISTINCT drawdown, so an assertion naming one row's denominator cannot be satisfied by the other's.
const quietRow = { ...sourceRow, anonWallet: "w_quiet", rank: 3, maxDrawdownMicro: 60 * MICRO, netAfterFeesMicro: 84 * MICRO, riskAdjustedBps: 14_000, closedTrades: 4, winRateBps: null, insufficientSample: true, sampleNote: "insufficient sample: 4 settled markets; a win rate needs 20" };

const config = {
  configId: "cfg-seed01",
  sourceAnon: "w_5c037b2ab8",
  mode: "ratio",
  ratioBps: 2_500,
  maxOrderMicro: 250 * MICRO,
  maxDailyMicro: 1_000 * MICRO,
  enabled: false,
  createdMs: asOf,
  dryRun: true,
  guardsFromRow: true,
  skipIfMovedCents: 2,
  doNotEnterWithinHours: 24,
  categoryFilter: "Politics",
  minPriceMicro: 50_000,
  maxPriceMicro: 900_000,
  takeProfitMicro: 850_000,
  stopLossMicro: 200_000,
  warning: {
    samples: 6, medianSlippageBps: 25, p90SlippageBps: 90, worstSlippageBps: 260, copied: 8, skipped: 4,
    skipRateBps: 3_333, default: "skip instead of chase: a fill that has moved more than 2 cents is skipped, not followed",
    warning: "by the time we see a fill the price has moved",
  },
  sourceStats: {
    ranking: "sources are ranked risk-adjusted",
    windows: [
      { windowDays: 7, closedTrades: 7, netAfterFeesMicro: -122 * MICRO, maxDrawdownMicro: 180 * MICRO, winRateBps: 4_285, insufficientSample: false, riskAdjustedBps: -6_777, avgLatencyMs: 820 },
      { windowDays: 30, closedTrades: 21, netAfterFeesMicro: 631 * MICRO, maxDrawdownMicro: 410 * MICRO, winRateBps: 5_714, insufficientSample: false, riskAdjustedBps: 15_390, avgLatencyMs: 760 },
    ],
  },
};

const monitor = {
  configId: "cfg-seed01",
  sourceAnon: "w_5c037b2ab8",
  live: [{ action: "copied", reason: "", deviationBps: 12, atMs: asOf - 1_000, intentId: "i1", dryRun: false }],
  wouldDo: [{ action: "enter", shares: "120", price: "0.41", sourcePrice: "0.408", deviationBps: 49, reason: "would copy: within the limit", atMs: asOf - 2_000, marketId: "0xM2", dryRun: true }],
  skips: [{ action: "skip", reason: "do_not_enter_within_24h: resolves soon", atMs: asOf - 3_000 }],
  slippage: config.warning,
  sourceStats: config.sourceStats,
  skipReasons: ["skip_if_moved", "resolved_soon"],
};

function stub(body: { rows?: unknown[]; configs?: unknown[]; monitor?: unknown; post?: unknown }) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string | URL, init?: RequestInit) => {
      const href = String(url);
      const stamp = { asOf, staleAfter: asOf + 5_000, cache: { ttlMs: 1_000, public: false } };
      if (init?.method === "POST") {
        return new Response(JSON.stringify({ ...stamp, ...(body.post ?? { configId: "cfg-new", dryRun: true }) }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (href.includes("/v1/copy/sources")) {
        return new Response(JSON.stringify({ ...stamp, rows: body.rows ?? [sourceRow, quietRow], ranking: "this list is ranked risk-adjusted by default - net after fees per unit of drawdown - and NOT by raw PnL", sortNote: "rows are ordered by risk-adjusted return (net after fees per unit of drawdown), descending", emptyNote: "" }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (href.includes("/v1/copy/configs/monitor")) {
        return new Response(JSON.stringify({ ...stamp, ...(body.monitor ?? monitor) }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response(JSON.stringify({ ...stamp, items: body.configs ?? [config] }), { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the copy screen", () => {
  it("states the ranking before the rows it orders", async () => {
    stub({});
    render(<CopyView />);
    await waitFor(() => expect(screen.getByText(/ranked risk-adjusted by default/)).toBeInTheDocument());
    expect(screen.getByText(/NOT by raw PnL/)).toBeInTheDocument();
    expect(screen.getByText(/rows are ordered by risk-adjusted return/)).toBeInTheDocument();
    // The default in the picker is the sort D7 asks for, and the raw-PnL option says what it is.
    expect((screen.getByLabelText("Sort by") as HTMLSelectElement).value).toBe("riskAdjusted");
    expect(screen.getByText(/the sort D7 refuses to default to/)).toBeInTheDocument();
  });

  it("puts the drawdown beside the ratio and refuses a rate below the gate", async () => {
    stub({});
    render(<CopyView />);
    await waitFor(() => expect(screen.getByText("w_quiet")).toBeInTheDocument());
    expect(screen.getByText("insufficient sample")).toBeInTheDocument();
    // Both rows state their own denominator, so the assertion is on all of them rather than on one.
    expect(screen.getAllByText(/per \$ of drawdown/).length).toBe(2);
    expect(screen.getByText(/per \$ of drawdown \(\$ 1 900\.00\)/)).toBeInTheDocument();
    expect(screen.getByText(/per \$ of drawdown \(\$ 60\.00\)/)).toBeInTheDocument();
  });

  it("warns with the measured slippage before the create button, and blocks live on history", async () => {
    stub({});
    render(<CopyView />);
    await waitFor(() => expect(screen.getByText(/ranked risk-adjusted by default/)).toBeInTheDocument());
    // The column header is also called "Monitor": the button is the button.
    fireEvent.click(screen.getByRole("button", { name: "Monitor" }));
    await waitFor(() => expect(screen.getByText(/median 0\.25%/)).toBeInTheDocument());
    expect(screen.getByText(/90th percentile 0\.90%/)).toBeInTheDocument();
    expect(screen.getByText(/4 skipped/)).toBeInTheDocument();
    expect(screen.getByText(/skip instead of chase/)).toBeInTheDocument();
    // Two dry runs in the monitor (one simulated fill, one engine skip) is the history that unblocks live.
    // The sentence is assembled from a count and a clause, so it is matched on its own fragment. The API's
    // first condition is unacknowledged here, so the screen says blocked — and names the reason.
    expect(screen.getByText(/2 recorded evaluations/)).toBeInTheDocument();
    expect(screen.getByText(/live copying is still blocked/)).toBeInTheDocument();
    expect(screen.getByText(/acknowledge the measured slippage above/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(screen.getByText(/nothing is blocking a live switch/)).toBeInTheDocument());
  });

  it("will not submit a form the API would refuse", async () => {
    stub({});
    render(<CopyView />);
    await waitFor(() => expect(screen.getByText(/ranked risk-adjusted by default/)).toBeInTheDocument());
    const create = screen.getByRole("button", { name: "Create it as a dry run" });
    expect(create).toBeDisabled();
    expect(screen.getByText(/pick a source from the list above/)).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "Use" })[0]!);
    await waitFor(() => expect(create).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Per-trade cap (USD)"), { target: { value: "900" } });
    await waitFor(() => expect(create).toBeDisabled());
    expect(screen.getByText(/the daily cap must be at least the per-trade cap/)).toBeInTheDocument();
  });

  it("shows a created config as a dry run, with its monitor open", async () => {
    stub({});
    render(<CopyView />);
    await waitFor(() => expect(screen.getByText(/ranked risk-adjusted by default/)).toBeInTheDocument());
    fireEvent.click(screen.getAllByRole("button", { name: "Use" })[0]!);
    const create = screen.getByRole("button", { name: "Create it as a dry run" });
    await waitFor(() => expect(create).toBeEnabled());
    fireEvent.click(create);
    await waitFor(() => expect(screen.getByText(/Created cfg-new as a dry run/)).toBeInTheDocument());
  });
});
