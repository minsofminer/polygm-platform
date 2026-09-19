import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WhaleTracker } from "./WhaleTracker";
import type { TerminalFill, WhaleView } from "./wire";

/**
 * D4's two on-screen promises, as render tests:
 *
 *  * every whale row carries the sentence that produced its badge (a ratio with no rule is a mood);
 *  * choosing a channel without a market is refused *before* the request, because the API refuses it after.
 *
 * The fixture's own arithmetic has to hold: $12 480 against a $500 line is 24.96x, and the ratio the row shows is
 * computed from basis points rather than taken from the sentence — a fixture whose sentence disagreed with its
 * digits would let a rendering bug pass.
 */
const MICRO = 1_000_000;
const asOf = 1_700_000_000_000;

const row: TerminalFill = {
  tsMs: asOf - 1_000,
  conditionId: "0xC1",
  tokenId: "1",
  marketId: "0xM1",
  marketSlug: "fed",
  question: "Fed cuts in March?",
  category: "Politics",
  tick: "0.01",
  side: "BUY",
  outcome: "Yes",
  price: "0.52",
  shares: "24000",
  notionalMicro: 12_480 * MICRO,
  anonWallet: "w_5c037b2ab8",
  labels: [{ label: "whale", confidence: 900, publishable: true, rule: "notional >= the market's line", disclaimer: "size is not intent" }],
  source: "venue",
  lagMs: 120,
  thresholdMicro: 500 * MICRO,
  thresholdRule: "whale = max(the p99.5 fill of this market's 900 fills ($150.00), $500.00 absolute floor) = $500.00",
  thresholdReason: "relative",
  severity: "urgent",
  ratioBps: 249_600,
  rule: "severity is the fill's ratio to the threshold: urgent at 4x, notice at 1.5x",
  isWhale: true,
};

const savedView: WhaleView = {
  viewId: "wv-seed-01",
  name: "big fills",
  filters: { minSeverity: "notice", multiple: 5 },
  channel: null,
  severity: "notice",
  scope: "global",
  marketId: null,
  ruleId: null,
  createdMs: 1,
  notifies: false,
  firesPerWindow: null,
  ruleWindowMs: null,
  ruleEnabled: null,
};

function stub(routes: { whales: unknown; views: unknown; created?: unknown }) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string | URL, init?: RequestInit) => {
      const href = String(url);
      const stamp = { asOf, staleAfter: asOf + 2_000, cache: { ttlMs: 1_000, public: true } };
      const body =
        init?.method === "POST"
          ? (routes.created ?? { viewId: "wv-new" })
          : href.includes("/v1/whales")
            ? { ...stamp, ...(routes.whales as Record<string, unknown>) }
            : { ...stamp, ...(routes.views as Record<string, unknown>) };
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the whale tracker", () => {
  it("renders each row's own threshold sentence and the feed's severity rule", async () => {
    stub({
      whales: { rows: [row], counts: { overThreshold: 3, returned: 1, marketsWithFills: 1 }, severityRule: row.rule, thresholds: {} },
      views: { items: [savedView] },
    });
    render(<WhaleTracker markets={[{ marketId: "0xM1", question: "Fed cuts in March?" }]} />);
    await waitFor(() => expect(screen.getByText(/\$500\.00 absolute floor/)).toBeInTheDocument());
    // The rule is stated above the feed AND inside every row's sentence: the second copy is the one a reader
    // checks against the number in front of them, so both are asserted.
    expect(screen.getAllByText(/severity is the fill's ratio to the threshold/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("24.96x")).toBeInTheDocument();                    // 249600 bps, no float on the way
    expect(screen.getByText(/1 of 3 fills over threshold/)).toBeInTheDocument();
    expect(screen.getByText(/size is not intent/)).toBeInTheDocument();        // the label's disclaimer is on the badge
  });

  it("shows a saved view as its knobs plus the rule's fire budget, and loads it", async () => {
    stub({
      whales: { rows: [], counts: { overThreshold: 0, returned: 0, marketsWithFills: 0 }, severityRule: "rule", thresholds: {} },
      views: { items: [{ ...savedView, name: "notify me", scope: "market", marketId: "0xM1", notifies: true, channel: "telegram", firesPerWindow: 4, ruleWindowMs: 3_600_000, ruleEnabled: true }] },
    });
    render(<WhaleTracker markets={[{ marketId: "0xM1", question: "Fed cuts in March?" }]} />);
    await waitFor(() => expect(screen.getByText(/notifies on telegram/)).toBeInTheDocument());
    expect(screen.getByText(/4 fires per 1h/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load" }));
    await waitFor(() => expect(screen.getByText(/the filters above are the ones it was saved with/)).toBeInTheDocument());
  });

  it("refuses a channel without a market before the request, and says so beside the control", async () => {
    stub({
      whales: { rows: [], counts: { overThreshold: 0, returned: 0, marketsWithFills: 0 }, severityRule: "rule", thresholds: {} },
      views: { items: [] },
    });
    render(<WhaleTracker markets={[{ marketId: "0xM1", question: "Fed cuts in March?" }]} />);
    await waitFor(() => expect(screen.getByText(/No saved views yet/)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText(/View name/), { target: { value: "fed whales" } });
    fireEvent.change(screen.getByLabelText(/Notify me/), { target: { value: "telegram" } });
    await waitFor(() => expect(screen.getByText(/a notifying view needs a market/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Save view" })).toBeDisabled();
  });

  it("switches to a market scope and asks the feed for that market only", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string | URL) => {
        calls.push(String(url));
        const stamp = { asOf, staleAfter: asOf + 2_000 };
        const body = String(url).includes("/v1/whales")
          ? { ...stamp, rows: [], counts: { overThreshold: 0, returned: 0, marketsWithFills: 0 }, severityRule: "rule", thresholds: {} }
          : { ...stamp, items: [] };
        return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
      }),
    );
    render(<WhaleTracker markets={[{ marketId: "0xM1", question: "Fed cuts in March?" }]} />);
    await waitFor(() => expect(calls.some((c) => c.includes("/v1/whales"))).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "One market" }));
    fireEvent.change(screen.getByLabelText("Market"), { target: { value: "0xM1" } });
    await waitFor(() => expect(calls.some((c) => c.includes("marketId=0xM1"))).toBe(true));
    expect(calls.some((c) => c.includes("scope=market"))).toBe(true);
  });
});
