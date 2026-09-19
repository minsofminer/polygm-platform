/**
 * The watchlist is a local store, so the failure modes are local too: another account's list, a half-written
 * entry, a duplicated id, a list that grew past its cap. Each of those has to cost one item at most.
 */
import { describe, expect, it } from "vitest";
import {
  EMPTY_WATCH,
  WATCHLIST_CAP,
  followedWallets,
  parseWatch,
  serialiseWatch,
  toggleMarket,
  toggleWallet,
  watchedMarketIds,
  watchKey,
} from "./watchlist";

describe("a watchlist on one device", () => {
  it("keys per user and version, so one account never reads another's list", () => {
    expect(watchKey("u1")).not.toBe(watchKey("u2"));
    expect(watchKey("u1")).toContain("v1");
  });

  it("toggles a market on and off with the same call", () => {
    const added = toggleMarket(EMPTY_WATCH, { marketId: "0xM1", question: "Fed cuts in March?" });
    expect(watchedMarketIds(added)).toEqual(["0xM1"]);
    const removed = toggleMarket(added, { marketId: "0xM1", question: "Fed cuts in March?" });
    expect(watchedMarketIds(removed)).toEqual([]);
  });

  it("hands the tape the wallets its sound toggle listens for", () => {
    const store = toggleWallet(EMPTY_WATCH, { anonWallet: "w_5c037b2ab8", label: "big buyer" });
    expect(followedWallets(store)).toEqual(["w_5c037b2ab8"]);
  });

  it("refuses junk instead of trusting it", () => {
    const parsed = parseWatch(
      JSON.stringify({
        markets: [{ marketId: "0xM1", question: "ok" }, { marketId: "", question: "no id" }, { question: "no id" }, "not an object", { marketId: "0xM1", question: "dupe" }],
        wallets: [{ anonWallet: "w_a" }, { anonWallet: 7 }],
      }),
    );
    expect(watchedMarketIds(parsed)).toEqual(["0xM1"]);         // the dupe and the idless entries cost themselves
    expect(parsed.wallets).toEqual([{ anonWallet: "w_a", label: "" }]);
  });

  it("gives back an empty list rather than throwing on unreadable storage", () => {
    expect(parseWatch("{not json")).toEqual(EMPTY_WATCH);
    expect(parseWatch(null)).toEqual(EMPTY_WATCH);
    expect(parseWatch(JSON.stringify({ markets: "no" })).markets).toEqual([]);
  });

  it("round-trips through storage unchanged", () => {
    const store = toggleMarket(toggleWallet(EMPTY_WATCH, { anonWallet: "w_a", label: "a" }), { marketId: "0xM1", question: "q" });
    expect(parseWatch(serialiseWatch(store))).toEqual(store);
  });

  it("caps the list instead of growing without bound", () => {
    let store = EMPTY_WATCH;
    for (let i = 0; i < WATCHLIST_CAP + 20; i += 1) {
      store = toggleMarket(store, { marketId: `0xM${i}`, question: `q${i}` });
    }
    expect(store.markets.length).toBe(WATCHLIST_CAP);
    // The oldest went, the newest stayed: a cap that dropped the row a user just added is a broken cap.
    expect(store.markets.at(-1)?.marketId).toBe(`0xM${WATCHLIST_CAP + 19}`);
  });
});
