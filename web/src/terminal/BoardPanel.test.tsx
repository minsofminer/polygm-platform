/**
 * D3 · the rating panel, rendered.
 *
 * The assertions are about what a user can SEE, because every rule this panel holds is a sentence: a badge that
 * carries its board, a gap that names the field it is measured in, an empty sparkline that says "no history yet"
 * instead of drawing a line, and a follow list whose button says what it does. The stubs carry the clock
 * (`asOf`/`staleAfter`) — an unstamped body is discarded by the hooks and the panel would render empty, which
 * reads as "element not found" rather than "the fixture was wrong".
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { BoardPanel } from "./BoardPanel";

const asOf = Date.now() - 1_000;
const stamp = { asOf, staleAfter: asOf + 5_000, cache: { ttlMs: 1_000, public: false } };

const standing = {
  ...stamp,
  anon: "w_a945dde868",
  board: "risk_adjusted",
  label: "Risk-adjusted PnL",
  window: "30d",
  state: "ranked",
  rank: 47,
  rankedTotal: 64,
  percentileBps: 7344,
  orderField: "scoreBps",
  orderUnits: "bps",
  rankBadge: { rank: 47, rankedTotal: 64, text: "#47" },
  above: { anon: "w_57c7dcc029" },
  below: { anon: "w_a23a3310e7" },
  gap: {
    rankAbove: 46,
    anonAbove: "w_57c7dcc029",
    field: "scoreBps",
    units: "bps",
    value: 5000,
    valueAbove: 5625,
    delta: 625,
    toPass: 5626,
    note: "",
  },
  reasons: [],
  note: "rank 47 of 64 on risk-adjusted pnl, and both neighbours are served with it",
  formula: "score = net realised after fees EXCLUDING the single best market, per unit of risk",
  gate: "20 settled markets and $500 of verified lifetime turnover",
  history: {
    days: 30,
    points: [
      { tsMs: asOf - 2_000, rank: 51 },
      { tsMs: asOf - 1_000, rank: 47 },
    ],
    snapshots: 2,
    latestRank: 47,
    bestRank: 47,
    worstRank: 51,
    delta: -4,
    note: "",
  },
};

const followRow = {
  anon: "w_32371216f9",
  label: "the twelfth",
  state: "ranked",
  rank: 12,
  rankBadge: { rank: 12, rankedTotal: 64, text: "#12" },
  realisedMicro: 11_800_000_000,
  realised: "11800",
  maxDrawdownMicro: 1_200_000_000,
  drawdown: "1200",
  winRateBps: 8_333,
  insufficientSample: false,
  sampleNote: "",
  reasons: [],
};

const follows = {
  ...stamp,
  board: "risk_adjusted",
  label: "Risk-adjusted PnL",
  window: "30d",
  rows: [followRow],
  total: 1,
  rankedTotal: 64,
  states: { ranked: 1, unranked: 0, absent: 0 },
  note: "a follow is a watch, not a copy: it changes nothing about what gets traded",
};


// D4 fixtures: the panel now carries the reader's own standing. The board row here is deliberately OFF the page
// (`offPage: true`, `rankedOnPage: null`), because that is the state the pin exists for.
const selfBoard = {
  board: "risk_adjusted",
  category: null,
  anon: "w_4ba30c8b77",
  label: "Risk-adjusted PnL",
  window: "30d",
  state: "ranked",
  rank: 87,
  rankedTotal: 412,
  rankBadge: { rank: 87, rankedTotal: 412, text: "#87" },
  percentileBps: 7888,
  orderField: "scoreBps",
  orderUnits: "bps",
  pageSize: 50,
  offPage: true,
  rankedAhead: 86,
  rankedBehind: 325,
  rankedOnPage: null,
  gap: {
    rankAbove: 86,
    anonAbove: "w_11aa22bb33",
    field: "scoreBps",
    units: "bps",
    value: 4100,
    valueAbove: 4350,
    delta: 250,
    toPass: 4351,
    note: "",
  },
  row: { scoreBps: 4100 },
  reasons: [],
  note: "",
};

const selfRank = {
  ...stamp,
  identity: { state: "private", decided: false, handle: "", since: null, note: "" },
  wallets: [
    {
      anon: "w_4ba30c8b77",
      boards: [selfBoard],
      ranked: 1,
      unranked: 0,
      best: { board: "risk_adjusted", label: "Risk-adjusted PnL", rank: 87, rankedTotal: 412, rankBadge: { text: "#87" } },
      nextSteps: [],
      note: "",
    },
  ],
  walletCount: 1,
  primary: {
    anon: "w_4ba30c8b77",
    defaultBoard: "risk_adjusted",
    pin: selfBoard,
    best: null,
    nextSteps: [],
  },
  pageSize: 50,
  links: {},
  note: "",
};

/** The same wallet when it is under the gate: D4's panel then carries the to-do list, not a badge. */
const selfRankUnranked = {
  ...selfRank,
  wallets: [
    {
      ...selfRank.wallets[0],
      boards: [
        {
          ...selfBoard,
          state: "unranked",
          rank: null,
          rankBadge: null,
          gap: null,
          rankedOnPage: null,
          rankedAhead: 0,
          rankedBehind: 0,
          row: null,
          unranked: {
            reasons: ["9 settled markets; this board needs 20", "$0 of $500 verified turnover"],
            settledMarkets: 9,
            verifiedVolumeMicro: 0,
            note: "",
          },
          reasons: ["9 settled markets; this board needs 20"],
        },
      ],
      ranked: 0,
      unranked: 1,
      best: null,
      nextSteps: ["settle 11 more markets to reach the 20 this board needs"],
      note: "",
    },
  ],
  primary: { anon: "w_4ba30c8b77", defaultBoard: "risk_adjusted", pin: null, best: null, nextSteps: [] },
};

const identityView = {
  ...stamp,
  identity: { state: "private", decided: false, handle: "", since: null, note: "" },
  handle: { claimed: "", published: "", rules: "3-24 characters, a-z, 0-9 and _", reserved: ["polygm", "admin"] },
  wallets: [],
  changes: ["your rows would carry the handle"],
  doesNotChange: ["your rows stay on the board", "your rank is the same number"],
  nudge: "listing is how another trader finds you; it is off until you turn it on",
  note: "",
};

function stub(body: Record<string, unknown> = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string | URL) => {
      const href = String(url);
      const json = (payload: unknown) =>
        new Response(JSON.stringify(payload), { status: 200, headers: { "content-type": "application/json" } });
      if (href.includes("/v1/leaderboard/rank")) return json({ ...standing, ...(body.standing ?? {}) });
      if (href.includes("/v1/leaderboard/me")) return json({ ...selfRank, ...(body.selfRank ?? {}) });
      if (href.includes("/v1/leaderboard/identity")) return json({ ...identityView, ...(body.identityView ?? {}) });
      if (href.includes("/v1/leaderboard/compare")) {
        return json({
          ...stamp,
          rows: body.compareRows ?? [],
          unranked: [],
          unknown: body.unknown ?? [],
          order: body.order ?? [],
          verdict: body.verdict ?? "w_a leads this comparison on risk-adjusted pnl at 5000 bps",
          note: "the rows are served in the order asked for",
          disclaimer: "a comparison of published components, not a recommendation",
          window: "30d",
          label: "Risk-adjusted PnL",
        });
      }
      return json({ ...follows, ...(body.follows ?? {}) });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the rating panel", () => {
  it("shows the followers' current standing, and says what a follow is not", async () => {
    stub();
    render(<BoardPanel focus="w_a945dde868" />);
    expect(await screen.findByText("the twelfth")).toBeTruthy();
    expect(screen.getAllByText(/a follow is a watch, not a copy/).length).toBeGreaterThan(0);
    expect(screen.getByText("#12")).toBeTruthy();
  });

  it("renders the standing with its board size, its percentile and the gap in the board's own field", async () => {
    stub();
    render(<BoardPanel focus="w_a945dde868" />);
    expect(await screen.findByText("#47 of 64")).toBeTruthy();
    expect(screen.getByText(/top 73.44% of 64/)).toBeTruthy();
    expect(screen.getByText(/625 bps behind w_57c7dcc029/)).toBeTruthy();
    expect(screen.getByText(/5626 bps would pass them/)).toBeTruthy();
    expect(screen.getByText(/#w_57c7dcc029 is directly above/)).toBeTruthy();
    expect(screen.getByText(/2 snapshots over 30 days/)).toBeTruthy();
  });

  it("draws the sparkline only when there is history to draw", async () => {
    stub();
    render(<BoardPanel focus="w_a945dde868" />);
    const chart = await screen.findByRole("img", { name: /Rank over the last 30 days/ });
    expect(chart.querySelector("polyline")?.getAttribute("points")).toBe("0,24 120,0");
  });

  it("says 'no history yet' and draws nothing when the recompute has not run", async () => {
    stub({ standing: { history: { days: 30, points: [], snapshots: 0, latestRank: null, bestRank: null, worstRank: null, delta: null, note: "" } } });
    render(<BoardPanel focus="w_a945dde868" />);
    expect(await screen.findByText(/no history yet/)).toBeTruthy();
    expect(screen.queryByRole("img", { name: /Rank over the last 30 days/ })).toBeNull();
  });

  it("renders an unranked wallet with the number that refused it rather than a badge", async () => {
    stub({
      standing: {
        state: "unranked",
        rank: null,
        rankBadge: null,
        gap: null,
        percentileBps: null,
        reasons: ["9 settled markets; this board needs 20"],
      },
      selfRank: selfRankUnranked,
    });
    render(<BoardPanel focus="w_a945dde868" />);
    expect(await screen.findByText(/9 settled markets; this board needs 20/)).toBeTruthy();
    expect(screen.queryByText("#47 of 64")).toBeNull();
  });

  it("pins the reader's own row when it is off the page, and says why it is there", async () => {
    stub();
    render(<BoardPanel focus="w_a945dde868" />);
    // Queried by its sentence, not by `role="status"`: the page has several status nodes (freshness markers among
    // them), and the assertion is about the strip.
    const strip = (await screen.findByText(/not on the first 50 rows/)).closest(".pgm-selfrank__pin");
    expect(strip).not.toBeNull();
    expect(strip!.textContent).toContain("Pinned:");
    expect(strip!.textContent).toContain("#87 of 412");
    // The gap that would move the row, in the board's own field — the whole point of pinning it.
    expect(strip!.textContent).toContain("250 bps behind w_11aa22bb33");
  });

  it("offers the comparison only once two wallets are picked", async () => {
    stub({ follows: { rows: [followRow, { ...followRow, anon: "w_second", label: "second" }], total: 2 } });
    render(<BoardPanel focus="w_a945dde868" />);
    const button = await screen.findByRole("button", { name: /Compare selected/ });
    expect(button.hasAttribute("disabled")).toBe(true);
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes.length).toBe(2);
    boxes.forEach((box) => (box as HTMLInputElement).click());
    await waitFor(() => expect(button.hasAttribute("disabled")).toBe(false));
  });

  it("names wallets that have never traded instead of silently comparing fewer", async () => {
    // Two followed wallets, so the comparison is enabled: one checkbox leaves the button disabled and the click
    // would test the disabled state rather than the `unknown` list.
    stub({
      unknown: ["w_0000000000"],
      follows: { rows: [followRow, { ...followRow, anon: "w_second", label: "second" }], total: 2 },
    });
    render(<BoardPanel focus="w_a945dde868" />);
    const boxes = await screen.findAllByRole("checkbox");
    boxes.forEach((box) => (box as HTMLInputElement).click());
    screen.getByRole("button", { name: /Compare selected/ }).click();
    expect(await screen.findByText(/Not seen trading, so there is no standing to compare: w_0000000000/)).toBeTruthy();
  });
});
