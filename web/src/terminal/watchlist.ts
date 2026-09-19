/**
 * D1's left rail and D2's shift-click need a watchlist, and the P10 API does not ship one: the contract has no
 * watchlist resource, so this is a local store rather than a pretend server call. That is a decision, not a
 * shortcut, and the rail says which it is — a list that looks synced and is not is worse than a list that says
 * "on this device".
 *
 * Three things it does carefully:
 *
 *  - **The key is per user AND versioned.** A watchlist saved by another account on a shared browser must not
 *    appear, and a stored shape from a build that had a third list must not be applied to this one.
 *  - **Parsing refuses junk instead of trusting it.** localStorage is user-editable and survives upgrades; a
 *    corrupt entry must cost one item, never the whole list. Ids are validated as non-empty strings, deduped,
 *    and capped.
 *  - **Toggling is a pure function**, so "shift-click added it" and "clicking the chip removed it" are the same
 *    operation with the same test.
 */
export const WATCHLIST_VERSION = 1;
export const WATCHLIST_CAP = 200;

export type WatchStore = {
  markets: { marketId: string; question: string }[];
  wallets: { anonWallet: string; label: string }[];
};

export const EMPTY_WATCH: WatchStore = { markets: [], wallets: [] };

export function watchKey(userId: string): string {
  return `pgm.terminal.watch.v${WATCHLIST_VERSION}.${userId}`;
}

function cleanList<T extends Record<string, string>>(input: unknown, fields: [string, string]): T[] {
  if (!Array.isArray(input)) return [];
  const seen = new Set<string>();
  const out: T[] = [];
  for (const item of input) {
    if (typeof item !== "object" || item === null) continue;
    const record = item as Record<string, unknown>;
    const id = record[fields[0]];
    if (typeof id !== "string" || id.length === 0 || seen.has(id)) continue;
    seen.add(id);
    const label = record[fields[1]];
    out.push({ [fields[0]]: id, [fields[1]]: typeof label === "string" ? label : "" } as T);
    if (out.length >= WATCHLIST_CAP) break;
  }
  return out;
}

/** Parse what was stored. Anything unreadable costs the whole list only when the outer JSON is unreadable. */
export function parseWatch(raw: string | null): WatchStore {
  if (!raw) return EMPTY_WATCH;
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return {
      markets: cleanList<WatchStore["markets"][number]>(parsed.markets, ["marketId", "question"]),
      wallets: cleanList<WatchStore["wallets"][number]>(parsed.wallets, ["anonWallet", "label"]),
    };
  } catch {
    return EMPTY_WATCH;
  }
}

export function serialiseWatch(store: WatchStore): string {
  return JSON.stringify(store);
}

export function toggleMarket(store: WatchStore, market: { marketId: string; question: string }): WatchStore {
  const exists = store.markets.some((m) => m.marketId === market.marketId);
  const markets = exists
    ? store.markets.filter((m) => m.marketId !== market.marketId)
    : [...store.markets, market].slice(-WATCHLIST_CAP);
  return { ...store, markets };
}

export function toggleWallet(store: WatchStore, wallet: { anonWallet: string; label: string }): WatchStore {
  const exists = store.wallets.some((w) => w.anonWallet === wallet.anonWallet);
  const wallets = exists
    ? store.wallets.filter((w) => w.anonWallet !== wallet.anonWallet)
    : [...store.wallets, wallet].slice(-WATCHLIST_CAP);
  return { ...store, wallets };
}

/** The tape's sound toggle fires for these, so the rail has to be able to hand them over as a plain list. */
export function followedWallets(store: WatchStore): string[] {
  return store.wallets.map((w) => w.anonWallet);
}

export function watchedMarketIds(store: WatchStore): string[] {
  return store.markets.map((m) => m.marketId);
}
