/**
 * D4 · the self-rank and identity rules, exercised as rules.
 *
 * These are the sentences a user reads about their own standing, and every one of them is a decision the API
 * made. The tests hold the two that would be quietly wrong if this module drifted:
 *
 *  - the strip is matched EXACTLY to the board on screen (a fallback would show a risk-adjusted rank under a
 *    win-rate selector, which is worse than showing nothing);
 *  - "private" is described as removing the LINK, never the row, because the opposite is what a user assumes.
 */
import { describe, expect, it } from "vitest";
import {
  boardRows,
  fieldOf,
  gapOf,
  handleDraft,
  identityDetail,
  identityText,
  nudgeText,
  pinFor,
  pinLine,
  selfStateText,
  stepsFor,
  type IdentityView,
  type SelfBoard,
  type SelfRank,
  type SelfWallet,
} from "./selfRank";

const gap = {
  rankAbove: 46,
  anonAbove: "w_57c7dcc029",
  field: "scoreBps",
  units: "bps",
  value: 5000,
  valueAbove: 5625,
  delta: 625,
  toPass: 5626,
  note: "",
};

function board(over: Partial<SelfBoard> = {}): SelfBoard {
  return {
    board: "risk_adjusted",
    category: null,
    anon: "w_a945dde868",
    label: "Risk-adjusted PnL",
    window: "30d",
    state: "ranked",
    rank: 47,
    rankedTotal: 64,
    rankBadge: { rank: 47, rankedTotal: 64, text: "#47" },
    percentileBps: 7344,
    orderField: "scoreBps",
    orderUnits: "bps",
    pageSize: 50,
    offPage: false,
    rankedOnPage: 1,
    rankedAhead: 46,
    rankedBehind: 17,
    gap,
    row: { scoreBps: 5000 },
    reasons: [],
    note: "",
    ...over,
  };
}

const boards: SelfBoard[] = [
  board(),
  board({ board: "win_rate", label: "Win rate", rank: 41, rankedTotal: 52, orderField: "winRateBps", row: { winRateBps: 6021 } }),
  board({
    board: "category",
    category: "Politics",
    label: "Politics specialists",
    rank: null,
    rankBadge: null,
    state: "unranked",
    gap: null,
    offPage: true,
    rankedOnPage: null,
    row: null,
    unranked: { reasons: ["9 settled Politics markets; this board needs 20"], settledMarkets: 9 },
  }),
];

const wallet: SelfWallet = {
  anon: "w_a945dde868",
  boards,
  ranked: 2,
  unranked: 1,
  best: { board: "win_rate", label: "Win rate", rank: 41, rankedTotal: 52, rankBadge: { text: "#41" } },
  nextSteps: [],
  note: "",
};

const me: SelfRank = {
  identity: { state: "private", decided: false, handle: "", listedMs: null, updatedMs: null, note: "" },
  wallets: [wallet],
  walletCount: 1,
  primary: { anon: "w_a945dde868", defaultBoard: "risk_adjusted", pin: boards[0]!, best: wallet.best, nextSteps: [] },
  pageSize: 50,
  links: {},
  note: "",
};

const view: IdentityView = {
  identity: { state: "private", decided: false, handle: "", listedMs: null, updatedMs: null, note: "" },
  handle: { claimed: "", published: "", rules: "3-24 characters, a-z, 0-9 and _", reserved: ["polygm", "admin", "support"] },
  wallets: [],
  changes: ["your rows would carry the handle instead of the pseudonym"],
  doesNotChange: ["your rows stay on the board", "your rank does not change"],
  nudge: "listing is how another trader finds you; it is off until you turn it on",
  note: "",
};

describe("pinning your own row", () => {
  it("matches the board on screen exactly, and draws nothing for a board that has no single row", () => {
    expect(pinFor(me, "risk_adjusted")?.label).toBe("Risk-adjusted PnL");
    expect(pinFor(me, "win_rate")?.rank).toBe(41);
    expect(pinFor(me, "category", "Politics")?.label).toBe("Politics specialists");
    // The category board is four boards wearing one name: with no category chosen there is nothing to pin.
    expect(pinFor(me, "category")).toBeNull();
    // No fallback to the default board, either: a strip about a board the reader did not pick is a lie.
    expect(pinFor({ ...me, wallets: [{ ...wallet, boards: [boards[0]!] }] }, "rising")).toBeNull();
  });

  it("says which page the row is on when it is on one, and why the strip exists when it is not", () => {
    expect(pinLine(board())).toBe("#47 of 64 — on page 1 of the board below");
    const off = board({ offPage: true, rankedOnPage: null, rank: 87 });
    expect(pinLine(off)).toBe("#87 of 64 — not on the first 50 rows, so it is pinned here");
  });

  it("states the gap in the board's own field, reusing D3's sentence", () => {
    expect(gapOf(board())).toContain("625 bps behind w_57c7dcc029");
    expect(gapOf(board())).toContain("5626 bps would pass them");
    expect(gapOf(board({ gap: null }))).toBe("");
    // Integer basis points only: no float ever reaches the sentence.
    expect(gapOf(board({ gap: { ...gap, delta: 1, toPass: 5625 } }))).toContain("1 bps behind");
  });

  it("titles the value it ranked on, from the board's own field and units", () => {
    expect(fieldOf(board())).toBe("50%");
    expect(fieldOf(board({ board: "win_rate", orderField: "winRateBps", row: { winRateBps: 6021 } }))).toBe("60.21%");
    expect(fieldOf(board({ orderField: "volumeMicro", orderUnits: "micro", row: { volumeMicro: 1_850_000 } }))).toBe("1850000");
    expect(fieldOf(board({ orderField: "scoreBps", row: null }))).toBe("");
  });
});

describe("you, on every board", () => {
  it("gives every board the panel reads its own row, with the placing or the reason there is none", () => {
    const rows = boardRows(wallet);
    expect(rows.map((r) => r.key)).toEqual(["risk_adjusted:", "win_rate:", "category:Politics"]);
    expect(rows[0]!.placing).toBe("#47 of 64");
    expect(rows[2]!.placing).toBe("not ranked yet — the board states which number refused this wallet");
    expect(rows[2]!.gap).toBe("");
    // A row that is off its page is flagged, which is what makes the strip worth drawing.
    expect(rows[2]!.pinned).toBe(true);
    expect(rows[0]!.pinned).toBe(false);
  });

  it("turns an unranked wallet into instructions with numbers in them, never into a shrug", () => {
    expect(stepsFor(wallet)).toEqual([]);
    const under = { ...wallet, ranked: 0, nextSteps: ["settle 11 more markets to reach the 20 this board needs"] };
    expect(stepsFor(under)[0]).toContain("11 more markets");
    // A payload that arrives without the API's steps still says where the numbers are, rather than nothing.
    expect(stepsFor({ ...wallet, ranked: 0, nextSteps: [] })[0]).toContain("which number refused it");
    expect(stepsFor(null)).toEqual([]);
  });

  it("labels a wallet that has no placing at all as unranked on that board", () => {
    expect(selfStateText(board())).toBe("#47 of 64");
    expect(selfStateText(board({ state: "unranked", rank: null }))).toContain("not ranked yet");
    expect(selfStateText(board({ board: "category", category: "Sports", state: "unknown", rank: null }))).toContain(
      "not a Sports specialist",
    );
    expect(selfStateText(board({ state: "unknown", rank: null, category: null }))).toBe("no placing on this board");
  });
});

describe("private is not invisible", () => {
  it("says the listing removes the link to the account, not the row", () => {
    expect(identityText(view.identity)).toBe("private (by default)");
    expect(identityText({ ...view.identity, decided: true })).toBe("private");
    expect(identityText({ ...view.identity, state: "listed", handle: "briefly_public" })).toBe("listed as briefly_public");
    expect(identityDetail(view.identity)).toContain("your rows are on the board under your pseudonym");
    expect(identityDetail({ ...view.identity, state: "listed", handle: "briefly_public" })).toContain(
      "your rank is the same number it was private",
    );
    expect(identityText(null)).toBe("");
    expect(identityDetail(undefined)).toBe("");
  });

  it("shows the API's own argument for opting in, not a second copy of it", () => {
    expect(nudgeText(view)).toBe(view.nudge);
    expect(nudgeText(null)).toBe("");
  });

  it("drafts a handle that satisfies the shape the server enforces", () => {
    expect(handleDraft("listed", "  Gate_Handle ", "")).toBe("gate_handle");
    expect(handleDraft("listed", "", "Polygm_Trader")).toBe("polygm_trader");
    // Opting out sends the claimed handle, which is how the server knows which publication to withdraw.
    expect(handleDraft("private", "ignored", "briefly_public")).toBe("briefly_public");
  });
});
