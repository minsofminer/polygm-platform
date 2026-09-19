/**
 * D4 · the self-rank panel, rendered.
 *
 * Two things are being proved here that a pure-function test cannot: the panel is fed by the API's own answers
 * (`/me` for the standings, `/identity` for the control), and the interaction — opting in — writes a consent
 * record through the same client every other mutation uses, key and all, then re-reads rather than assuming.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SelfRank } from "./SelfRank";

const asOf = Date.now() - 1_000;
const stamp = { asOf, staleAfter: asOf + 5_000, cache: { ttlMs: 1_000, public: false } };

const boardRow = (over: Record<string, unknown> = {}) => ({
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
  ...over,
});

const identity = { state: "private" as const, decided: false, handle: "", listedMs: null, updatedMs: null, note: "" };

const me = {
  ...stamp,
  identity,
  wallets: [
    {
      anon: "w_4ba30c8b77",
      boards: [
        boardRow(),
        boardRow({ board: "win_rate", label: "Win rate", rank: 41, rankedTotal: 52, rankBadge: { rank: 41, rankedTotal: 52, text: "#41" }, offPage: false, rankedOnPage: 1 }),
        boardRow({
          board: "category",
          category: "Politics",
          label: "Politics specialists",
          state: "unranked",
          rank: null,
          rankBadge: null,
          gap: null,
          rankedOnPage: null,
          row: null,
          unranked: { reasons: ["9 settled Politics markets; this board needs 20"], settledMarkets: 9 },
        }),
      ],
      ranked: 2,
      unranked: 1,
      best: { board: "win_rate", label: "Win rate", rank: 41, rankedTotal: 52, rankBadge: { text: "#41" } },
      nextSteps: [],
      note: "",
    },
  ],
  walletCount: 1,
  primary: { anon: "w_4ba30c8b77", defaultBoard: "risk_adjusted", pin: boardRow(), best: null, nextSteps: [] },
  pageSize: 50,
  links: {},
  note: "",
};

const view = {
  ...stamp,
  identity,
  handle: { claimed: "", published: "", rules: "3-24 characters, a-z, 0-9 and _", reserved: ["polygm", "admin"] },
  wallets: [],
  changes: ["your rows would carry the handle instead of the pseudonym"],
  doesNotChange: ["your rows stay on the board", "your rank does not change"],
  nudge: "listing is how another trader finds you; it is off until you turn it on",
  note: "",
};

type Call = { url: string; method: string; body: unknown; key: string | null };

function stub(opts: { unauthenticated?: boolean; refuse?: string; onWrite?: (call: Call) => Response | null } = {}) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string | URL, init?: RequestInit) => {
      const href = String(url);
      const method = (init?.method ?? "GET").toUpperCase();
      const json = (payload: unknown, status = 200) =>
        new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });
      const call: Call = {
        url: href,
        method,
        body: init?.body ? JSON.parse(String(init.body)) : null,
        key: new Headers(init?.headers).get("idempotency-key"),
      };
      calls.push(call);
      if (opts.unauthenticated) {
        return json({ error: { code: "UNAUTHENTICATED", message: "sign in to see your own standing", retryable: false, requestId: "r" } }, 401);
      }
      if (method === "POST") {
        if (opts.refuse) {
          return json({ error: { code: opts.refuse, message: `refused: ${opts.refuse}`, retryable: false, requestId: "r" } }, opts.refuse === "HANDLE_TAKEN" ? 409 : 422);
        }
        const written = opts.onWrite?.(call);
        if (written) return written;
        return json({ ...stamp, identity: { ...identity, state: "listed", decided: true, handle: "gate_handle" }, note: "" });
      }
      if (href.includes("/v1/leaderboard/me")) return json(me);
      return json(view);
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the self-rank panel", () => {
  it("labels itself as private to the account and lists every board the API answered for", async () => {
    stub();
    render(<SelfRank board="risk_adjusted" />);
    expect(await screen.findByText(/only you can see this/)).toBeTruthy();
    expect(await screen.findByRole("rowheader", { name: "Risk-adjusted PnL" })).toBeTruthy();
    expect(screen.getByRole("rowheader", { name: "Win rate" })).toBeTruthy();
    expect(screen.getByRole("rowheader", { name: "Politics specialists" })).toBeTruthy();
    // Ranked and unranked rows are both present: a board you are not on is still your standing on it.
    expect(screen.getByText("#87 of 412")).toBeTruthy();
    expect(screen.getByText(/not ranked yet/)).toBeTruthy();
    expect(screen.getByText(/private \(by default\)/)).toBeTruthy();
  });

  it("says the listing removes the link, not the row — every time the control is on screen", async () => {
    stub();
    render(<SelfRank board="risk_adjusted" />);
    expect(await screen.findByText(/your rows are on the board under your pseudonym/)).toBeTruthy();
    expect(screen.getByText("What listing does NOT change")).toBeTruthy();
    expect(screen.getByText("your rows stay on the board")).toBeTruthy();
    expect(screen.getByText(view.nudge)).toBeTruthy();
  });

  it("pins the reader's row when it is off the page, and does not when it is on it", async () => {
    stub();
    const { unmount } = render(<SelfRank board="risk_adjusted" />);
    expect(await screen.findByText(/not on the first 50 rows/)).toBeTruthy();
    unmount();
    // The same wallet on a board where it is on page 1: the table still shows the row, and nothing is pinned.
    render(<SelfRank board="win_rate" />);
    expect(await screen.findByRole("rowheader", { name: "Win rate" })).toBeTruthy();
    expect(screen.queryByText(/Pinned:/)).toBeNull();
  });

  it("opts in through the API's consent write and re-reads rather than assuming", async () => {
    const calls = stub();
    render(<SelfRank board="risk_adjusted" />);
    const box = await screen.findByLabelText("Handle");
    fireEvent.change(box, { target: { value: "Gate_Handle" } });
    fireEvent.click(screen.getByRole("button", { name: /List me under a handle/ }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const write = calls.find((c) => c.method === "POST")!;
    expect(write.url).toContain("/v1/leaderboard/identity");
    expect(write.body).toEqual({ state: "listed", handle: "gate_handle" });
    // Keyed like every other mutation, and in the shape the server accepts (no colon: `_IDEM_RE`).
    expect(write.key).toMatch(/^[A-Za-z0-9_-]{8,128}$/);
    // A re-read follows the write: the panel shows the state the server reports, not the state it hoped for.
    await waitFor(() => expect(calls.filter((c) => c.method === "GET").length).toBeGreaterThanOrEqual(4));
  });

  it("renders the server's refusal instead of swallowing it, and stays private", async () => {
    stub({ refuse: "HANDLE_TAKEN" });
    render(<SelfRank board="risk_adjusted" />);
    fireEvent.click(await screen.findByRole("button", { name: /List me under a handle/ }));
    expect(await screen.findByText(/refused: HANDLE_TAKEN/)).toBeTruthy();
    expect(screen.getByText(/private \(by default\)/)).toBeTruthy();
  });

  it("answers a signed-out visitor by saying the panel is per account, not by showing an empty panel", async () => {
    stub({ unauthenticated: true });
    render(<SelfRank board="risk_adjusted" />);
    expect(await screen.findByText(/this panel is served per account/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /List me under a handle/ })).toBeNull();
  });
});
