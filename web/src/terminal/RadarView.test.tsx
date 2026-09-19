import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { RadarView } from "./RadarView";
import type { RadarResult } from "./wire";

/**
 * D5's on-screen promises: the cost is stated before the scan, the gate's exclusions are listed, and a label is
 * rendered as its rule and its disclaimer rather than as a bare chip.
 *
 * The fixture's arithmetic holds: $400 bought and $120 sold is `+$ 55.00` realised in the row, and the wallet
 * under the gate is the one with 2 settled markets, not the one with 21.
 */
const MICRO = 1_000_000;
const asOf = Date.now() - 2_000;

const result: RadarResult = {
  items: [],
  ranking: "active",
  markets: ["0xM1"],
  rankings: {
    active: [
      { anonWallet: "w_5c037b2ab8", matched: [{ marketId: "0xM1", question: "Fed cuts in March?" }], boughtMicro: 400 * MICRO, soldMicro: 120 * MICRO, realisedMicro: 55 * MICRO, winRateBps: 5_714, insufficientSample: false, labels: [{ label: "whale", confidence: 900, publishable: true, rule: "notional >= this market's line", disclaimer: "size is not intent" }], rank: 1, reason: "bought $400 and sold $120" },
      { anonWallet: "w_small", matched: [{ marketId: "0xM1", question: "Fed cuts in March?" }], boughtMicro: 40 * MICRO, soldMicro: 0, realisedMicro: 8 * MICRO, winRateBps: null, insufficientSample: true, labels: [], rank: 2, reason: "bought $40" },
    ],
  },
  rankingsMeta: [
    { id: "active", label: "most active" } as RadarResult["rankingsMeta"][number],
    { id: "profit", label: "highest profit" } as RadarResult["rankingsMeta"][number],
  ],
  unranked: [{ anonWallet: "w_small", fills: 2, markets: ["0xM1"], realisedMicro: 8 * MICRO, winRateBps: null, insufficientSample: true, reason: "2 settled markets is not a profit record" }],
  scanned: 2,
  sampleGate: 20,
  quota: { plan: "free", usedToday: 2, perDay: 20, cached: false, jobId: null, note: "this scan counted against today's allowance" },
  costNote: "a scan reads the durable fill log, not the venue",
};

function stub() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      const stamp = { asOf, staleAfter: asOf + 5_000, cache: { ttlMs: 1_000, public: false } };
      return new Response(JSON.stringify({ ...stamp, ...result }), { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const markets = [{ marketId: "0xM1", question: "Fed cuts in March?" }];

describe("the wallet radar", () => {
  it("will not scan before a market is picked, and says what a scan costs", async () => {
    stub();
    render(<RadarView markets={markets} />);
    expect(screen.getByRole("button", { name: "Scan" })).toBeDisabled();
    expect(screen.getByText(/pick at least one market to scan/)).toBeInTheDocument();
    expect(screen.getByText(/no scan yet: a scan costs one of your daily allowance/)).toBeInTheDocument();
  });

  it("renders the rows, the gate's exclusion and the label's own rule", async () => {
    stub();
    render(<RadarView markets={markets} />);
    fireEvent.change(screen.getByLabelText("Add a market to the scan"), { target: { value: "0xM1" } });
    fireEvent.click(screen.getByRole("button", { name: "Scan" }));
    await waitFor(() => expect(screen.getByText("w_5c037b2ab8")).toBeInTheDocument());
    expect(screen.getByText("57.14%")).toBeInTheDocument();
    expect(screen.getByText("insufficient sample")).toBeInTheDocument();
    // The disclosure is text, not a tooltip: the rule and the caveat are on the screen.
    expect(screen.getByText(/notional >= this market's line · size is not intent/)).toBeInTheDocument();
    expect(screen.getByText(/NOT in the profit ranking/)).toBeInTheDocument();
    expect(screen.getByText(/2 settled markets is not a profit record/)).toBeInTheDocument();
    expect(screen.getByText(/3 of 20 scans used today/)).toBeInTheDocument();
  });

  it("refuses an eleventh market instead of dropping one", async () => {
    stub();
    const many = Array.from({ length: 10 }, (_, i) => ({ marketId: `0xM${i}`, question: `Market ${i}` }));
    render(<RadarView markets={many} />);
    for (let i = 0; i < 10; i += 1) {
      fireEvent.change(screen.getByLabelText("Add a market to the scan"), { target: { value: `0xM${i}` } });
    }
    expect(screen.getByText(/10 of 10 markets picked/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Add a market to the scan"), { target: { value: "0xM0" } });
    // Removing one is the same control, so the cap is never a dead end.
    await waitFor(() => expect(screen.getByText(/9 of 10 markets picked/)).toBeInTheDocument());
  });

  it("shows the same fields in the card view as in the list", async () => {
    stub();
    render(<RadarView markets={markets} />);
    fireEvent.change(screen.getByLabelText("Add a market to the scan"), { target: { value: "0xM1" } });
    fireEvent.click(screen.getByRole("button", { name: "Scan" }));
    await waitFor(() => expect(screen.getByText("w_5c037b2ab8")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Compact cards" }));
    // Realised PnL and the gated win rate survive the change of view: a card view cannot be the flattering one.
    expect(screen.getAllByText(/55\.00/).length).toBeGreaterThan(0);
    expect(screen.getByText(/insufficient sample/)).toBeInTheDocument();
  });
});
