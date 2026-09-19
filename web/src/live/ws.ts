/**
 * The live feed. Three rules make this more than `new WebSocket`:
 *
 *  1. Reconnect is exponential with a ceiling and a jitter, and a reconnect never resumes the old view:
 *     it resnapshots first. The gap the user missed is counted and shown ("612 fills not shown"), because a
 *     silently resumed tape is a tape that lied about what you saw.
 *  2. A heartbeat with a deadline. Without one, a half-open socket over a mobile carrier's NAT looks
 *     identical to a connected one for minutes — `onclose` never fires, so the deadline is the only thing
 *     that turns the trade button off.
 *  3. When no WebSocket exists in this deployment (`env.hasWebSocket` is false today — the API serves the
 *     tape over REST only, launch item P08-L18), the feed runs in REST mode: rows are tagged `rest`, the
 *     flash policy turns off, and the freshness clock keeps running so the stale indicator still gets to
 *     the blocking state. Polling is a degraded mode, not a substitute, and it is labelled as one.
 */
import type { Freshness } from "@/api/envelope";
import { freshnessOf, stampFrom, type Stamp } from "@/api/envelope";

export type Transport = {
  connect(url: string, handlers: {
    onOpen: () => void;
    onMessage: (raw: string) => void;
    onClose: (why: "closed" | "error" | "heartbeat-deadline") => void;
  }): { send: (text: string) => void; close: () => void };
  now: () => number;
  schedule: (ms: number, fn: () => void) => () => void;
};

export type FeedSnapshot<T> = {
  data: T | null;
  stamp: Stamp | null;
  freshness: Freshness;
  mode: "ws" | "rest" | "down";
  connected: boolean;
  /** Fills missed in the current discontinuity. Cleared when the resync lands. */
  gapCount: number;
  /** The number behind "612 fills not shown", kept after the resync so the banner can finish its sentence. */
  lastGapCount: number;
  resyncing: boolean;
  /** What to say, in words, when trading is off. A control that explains itself is worth the copy. */
  blockReason: string | null;
};

export const HEARTBEAT_INTERVAL_MS = 5_000;
export const HEARTBEAT_DEADLINE_MS = 12_000;
export const BACKOFF_MS = [250, 600, 1_200, 2_500, 5_000, 10_000];
export const REST_POLL_MS = 2_000;
/** Past this many missed frames the feed stops pretending a resync is cheap. */
export const GAP_RESYNC_AFTER = 1;

export type FeedOptions = {
  transport: Transport;
  url: string;
  resync: () => Promise<{ body: string }>;
  parse: (raw: string) => { payload: unknown; stamp?: Stamp; seq?: number };
  onSnapshot: (state: FeedSnapshot<unknown>) => void;
  /** Blocking threshold multiplier lives in the stamp; this is the wall-clock tick. */
  tickMs?: number;
};

export function createFeed(opts: FeedOptions) {
  let attempt = 0;
  let socket: ReturnType<Transport["connect"]> | null = null;
  let state: FeedSnapshot<unknown> = {
    data: null, stamp: null, freshness: "unknown", mode: "down", connected: false, gapCount: 0, lastGapCount: 0, resyncing: false, blockReason: "the feed has not started",
  };
  let lastSeq = 0;
  let lastBeat = 0;
  let cancelled = false;
  const clears: (() => void)[] = [];

  const publish = (patch: Partial<FeedSnapshot<unknown>>) => {
    state = { ...state, ...patch };
    opts.onSnapshot(state);
  };

  const recomputeFreshness = () => {
    const f = freshnessOf(state.stamp, opts.transport.now());
    const blockReason =
      state.mode === "down" || !state.connected
        ? `disconnected (${state.mode}) — orders are disabled until the feed returns; cancel-all stays available`
        : f === "blocking"
          ? "the feed is too old to trade against"
          : f === "stale"
            ? "the feed is late; you can still trade but the quote may have moved"
            : null;
    if (f !== state.freshness || blockReason !== state.blockReason) {
      publish({ freshness: f, blockReason });
    }
  };

  const handleRaw = (raw: string, from: "ws" | "rest") => {
    let parsed: { payload: unknown; stamp?: Stamp; seq?: number };
    try {
      parsed = opts.parse(raw);
    } catch {
      return;
    }
    if (typeof parsed.seq === "number") {
      if (lastSeq && parsed.seq > lastSeq + 1) {
        const missed = parsed.seq - lastSeq - 1;
        // The gap is published before the resync so the UI can say "612 fills not shown" *while* it is
        // fetching, and the number survives in lastGapCount after: a banner that vanishes in the same frame
        // as it appears is a banner nobody reads.
        publish({ gapCount: state.gapCount + missed, lastGapCount: state.lastGapCount + missed, resyncing: true });
        if (missed >= GAP_RESYNC_AFTER) void resync(from);
      }
      lastSeq = Math.max(lastSeq, parsed.seq);
    }
    const stamp = parsed.stamp ?? stampFrom((parsed.payload ?? {}) as Record<string, unknown>) ?? state.stamp;
    publish({ data: parsed.payload, ...(stamp ? { stamp } : {}), mode: from });
    recomputeFreshness();
  };

  const resync = async (_from: "ws" | "rest") => {
    try {
      const { body } = await opts.resync();
      handleRaw(body, _from);
      publish({ gapCount: 0, resyncing: false });
    } catch {
      publish({ mode: "down", connected: false, freshness: "blocking", resyncing: false, blockReason: "resync failed — the tape is not showing you the market" });
    }
  };

  const beat = () => {
    if (!state.connected) return;
    if (opts.transport.now() - lastBeat > HEARTBEAT_DEADLINE_MS) {
      socket?.close();
      return;
    }
    socket?.send(JSON.stringify({ type: "ping", at: opts.transport.now() }));
  };

  const connectWs = () => {
    if (cancelled) return;
    socket = opts.transport.connect(opts.url, {
      onOpen: () => {
        if (cancelled) return;
        attempt = 0;
        lastBeat = opts.transport.now();
        publish({ connected: true, mode: "ws" });
        // Snapshot first, always. A resumed socket that appends to a view it has not re-read is how a
        // reconnect shows a book that is minutes old and calls it live.
        void resync("ws");
      },
      onMessage: (raw) => {
        if (cancelled) return;
        lastBeat = opts.transport.now();
        try {
          const msg = JSON.parse(raw) as { type?: string };
          if (msg.type === "pong") return;
        } catch {
          /* not JSON: a frame we cannot read is a closed connection, not a silent one */
        }
        handleRaw(raw, "ws");
      },
      onClose: (why) => {
        if (cancelled) return;   // a feed the page stopped must not schedule a reconnect behind its back
        socket = null;
        publish({ connected: false, mode: "down", freshness: freshnessOf(state.stamp, opts.transport.now()), blockReason: `disconnected (${why})` });
        const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)] ?? 10_000;
        attempt++;
        clears.push(opts.transport.schedule(delay, connectWs));
      },
    });
  };

  const startRest = () => {
    const run = async () => {
      try {
        const { body } = await opts.resync();
        handleRaw(body, "rest");
      } catch {
        publish({ mode: "down", connected: false, freshness: "blocking", blockReason: "the feed is unreachable" });
      }
      if (!cancelled) clears.push(opts.transport.schedule(REST_POLL_MS, run));
    };
    void run();
  };

  return {
    start: () => {
      if (cancelled) return;
      if (opts.url) connectWs();
      else startRest();
      const tick = () => {
        if (cancelled) return;
        beat();
        recomputeFreshness();
        // Pushed into `clears` on every pass, so stop() can cancel the *pending* one. A self-rearming
        // schedule that is not registered anywhere is a timer outliving the component that owned it.
        clears.push(opts.transport.schedule(opts.tickMs ?? 1_000, tick));
      };
      tick();
    },
    stop: () => {
      cancelled = true;
      // Clear the timers *before* closing: close() can deliver onClose synchronously, and an onClose that
      // runs while the list still holds armed callbacks would push a cancel handle nobody will ever pop.
      for (const c of clears.splice(0)) c();
      const dying = socket;
      socket = null;
      dying?.close();
      // The state moves without publishing: the subscriber is gone, and calling back into an unmounted
      // component to announce a teardown is a warning, not information.
      state = { ...state, connected: false, mode: "down", resyncing: false, blockReason: "the feed is stopped" };
    },
    state: () => state,
  };
}
