/**
 * The connection status the whole shell reads from. One store, three consequences, because the failure mode
 * of "connection indicator" is that the dot and the button ask two different questions:
 *   1. the dot in the top bar always shows it (including "no timestamp", which is a real state we ship);
 *   2. trading is disabled while disconnected *and while the feed is too old*, in words;
 *   3. cancel-all stays available — a client that disables the escape hatch is worse than no indicator.
 *
 * The authority for #2 is the server's risk gate (P06); this is the UX that stops people discovering that
 * authority by having an order refused. The sentence says so.
 */
import { create } from "zustand";
import type { Freshness } from "@/api/envelope";

export type Connection = {
  mode: "ws" | "rest" | "down";
  connected: boolean;
  freshness: Freshness;
  gapCount: number;
  lastGapCount: number;
  resyncing: boolean;
  blockReason: string | null;
  staleMs: number | null;
};

export const IDLE: Connection = {
  mode: "down",
  connected: false,
  freshness: "unknown",
  gapCount: 0,
  lastGapCount: 0,
  resyncing: false,
  blockReason: "the feed has not started",
  staleMs: null,
};

type Store = Connection & {
  set: (next: Partial<Connection>) => void;
  canTrade: () => boolean;
  whyNot: () => string | null;
  /** The escape hatch is never gated on the connection. */
  cancelAllAvailable: () => boolean;
};

export const useConnection = create<Store>((set, get) => ({
  ...IDLE,
  set: (next) => set(next),
  canTrade: () => {
    const s = get();
    if (s.mode === "down") return false;
    return s.freshness === "live";
  },
  whyNot: () => {
    const s = get();
    if (s.mode === "down") return s.blockReason ?? "disconnected";
    if (s.freshness === "blocking") return "the feed is too old to trade against";
    if (s.freshness === "stale") return "the feed is late; the quote may have moved";
    if (s.freshness === "unknown") return "no timestamp on the data yet";
    return null;
  },
  cancelAllAvailable: () => true,
}));

export function connectionLabel(s: Connection): "live" | "stale" | "blocking" | "down" | "unknown" {
  if (s.mode === "down") return "down";
  if (s.freshness === "unknown") return "unknown";
  return s.freshness;
}
