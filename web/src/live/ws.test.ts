import { describe, expect, it, vi } from "vitest";
import { BACKOFF_MS, createFeed, HEARTBEAT_DEADLINE_MS, type Transport } from "./ws";

/** A clock and a transport we control, so reconnect/gap/stale are tested rather than described. */
function harness(opts: { url?: string; frames?: string[]; resyncBodies?: string[] } = {}) {
  let now = 1_000_000;
  const timers: { at: number; fn: () => void; id: number }[] = [];
  let nextId = 1;
  const sent: string[] = [];
  const handlers: { onOpen: () => void; onMessage: (raw: string) => void; onClose: (w: never) => void }[] = [];
  const transport: Transport = {
    connect: (_url, h) => {
      handlers.push(h as never);
      return { send: (t) => void sent.push(t), close: () => void h.onClose("closed" as never) };
    },
    now: () => now,
    schedule: (ms, fn) => {
      const id = nextId++;
      timers.push({ at: now + ms, fn, id });
      return () => {
        const i = timers.findIndex((t) => t.id === id);
        if (i >= 0) timers.splice(i, 1);
      };
    },
  };
  const snapshots: { gapCount: number; lastGapCount: number; resyncing: boolean; mode: string; connected: boolean }[] = [];
  let resyncs = 0;
  const bodies = opts.resyncBodies ?? [];
  const feed = createFeed({
    transport,
    url: opts.url ?? "",
    resync: async () => {
      const body = bodies[Math.min(resyncs, bodies.length - 1)] ?? JSON.stringify({ items: [], asOf: now, staleAfter: now + 4_000, cache: { ttlMs: 0 } });
      resyncs++;
      return { body };
    },
    parse: (raw) => {
      const j = JSON.parse(raw);
      return { payload: j, seq: typeof j.seq === "number" ? j.seq : undefined, stamp: j.asOf ? { asOf: j.asOf, staleAfter: j.staleAfter, ttlMs: 0 } : undefined };
    },
    onSnapshot: (snap) => snapshots.push(snap),
  });
  const advance = (ms: number) => {
    now += ms;
    for (let i = 0; i < 6; i++) {
      const due = timers.filter((t) => t.at <= now).sort((a, b) => a.at - b.at);
      if (!due.length) break;
      due.forEach((t) => {
        const idx = timers.indexOf(t);
        if (idx >= 0) timers.splice(idx, 1);
        t.fn();
      });
    }
  };
  const frame = (payload: Record<string, unknown>) => handlers[handlers.length - 1]?.onMessage(JSON.stringify(payload));
  const open = () => handlers[handlers.length - 1]?.onOpen();
  const close = () => handlers[handlers.length - 1]?.onClose("closed" as never);
  const flush = () => new Promise((r) => setTimeout(r, 0));
  return { feed, advance, frame, open, close, sent, handlers, snapshots, flush, nowFn: () => now, resyncCount: () => resyncs };
}

describe("the reconnect contract", () => {
  it("resnapshots on open before accepting the stream, so a resume never shows an old view as live", () => {
    const h = harness({ url: "wss://example.invalid/tape" });
    h.feed.start();
    const before = h.resyncCount();
    h.open();
    expect(h.resyncCount()).toBe(before + 1);
    expect(h.feed.state().mode).toBe("ws");
    expect(h.feed.state().connected).toBe(true);
  });

  it("counts the gap, shows it during the resync, and keeps the number after", async () => {
    const h = harness({ url: "wss://example.invalid/tape" });
    h.feed.start();
    h.open();
    await h.flush();
    h.frame({ items: [], seq: 1, asOf: h.nowFn(), staleAfter: h.nowFn() + 4_000 });
    h.frame({ items: [], seq: 613, asOf: h.nowFn(), staleAfter: h.nowFn() + 4_000 });
    const midGap = h.snapshots.find((s) => s.gapCount > 0);
    expect(midGap?.gapCount).toBe(611);
    expect(midGap?.resyncing).toBe(true);
    await h.flush();
    expect(h.feed.state().gapCount).toBe(0);
    // The banner's number is the one the user was shown, and it does not evaporate with the spinner.
    expect(h.feed.state().lastGapCount).toBe(611);
    expect(h.feed.state().resyncing).toBe(false);
  });

  it("backs off on the published schedule and stops at the ceiling", () => {
    const h = harness({ url: "wss://example.invalid/tape" });
    h.feed.start();
    const seen: number[] = [];
    for (let i = 0; i < 8; i++) {
      const before = h.handlers.length;
      h.close();
      const opened = h.handlers.length > before;
      seen.push(opened ? 1 : 0);
      h.advance(BACKOFF_MS[Math.min(i, BACKOFF_MS.length - 1)] ?? 10_000);
    }
    expect(h.handlers.length).toBeGreaterThan(1);
    expect(BACKOFF_MS[BACKOFF_MS.length - 1]).toBe(10_000);
  });

  it("treats a missed heartbeat as a dead socket, because a half-open connection sends no close event", () => {
    const h = harness({ url: "wss://example.invalid/tape" });
    h.feed.start();
    h.open();
    expect(h.feed.state().connected).toBe(true);
    h.advance(HEARTBEAT_DEADLINE_MS + 2_000);
    expect(h.feed.state().connected).toBe(false);
    expect(h.feed.state().blockReason).toContain("disconnected");
  });

  it("turns trading off by freshness alone, while the connection is still up", () => {
    const h = harness({ url: "wss://example.invalid/tape" });
    h.feed.start();
    h.open();
    h.frame({ items: [], asOf: h.nowFn(), staleAfter: h.nowFn() + 2_000 });
    expect(h.feed.state().freshness).toBe("live");
    h.advance(3_000);
    expect(h.feed.state().freshness).toBe("stale");
    h.advance(6_000);
    expect(h.feed.state().freshness).toBe("blocking");
    expect(h.feed.state().blockReason).toContain("too old to trade");
  });
});

describe("stop() means stopped", () => {
  // Both of these were real leaks: the 1s heartbeat tick re-armed itself through a schedule() that was
  // never registered anywhere, so it outlived the component; and onClose, which the socket's close()
  // delivers synchronously in this transport, pushed a reconnect timer into a feed nobody wanted.
  it("cancels the periodic tick, so a torn-down page stops asking for the clock", () => {
    const h = harness({ url: "wss://example.invalid/tape" });
    h.feed.start();
    h.open();
    h.advance(3_000);
    const seen = h.snapshots.length;
    const pings = h.sent.length;
    h.feed.stop();
    h.advance(60_000);
    expect(h.snapshots.length).toBe(seen);
    expect(h.sent.length).toBe(pings);
  });

  it("does not reconnect, and does not publish, after the owner stopped it", () => {
    const h = harness({ url: "wss://example.invalid/tape" });
    h.feed.start();
    h.open();
    expect(h.handlers.length).toBe(1);
    const seen = h.snapshots.length;
    h.feed.stop();
    expect(h.feed.state().connected).toBe(false);
    expect(h.snapshots.length).toBe(seen);   // teardown is not an announcement to a subscriber that left
    for (const wait of BACKOFF_MS) h.advance(wait + 1_000);
    expect(h.handlers.length).toBe(1);
  });
});

describe("REST mode is a labelled degraded mode", () => {
  it("runs without a socket URL and never claims to be ws", async () => {
    const h = harness({ url: "" });
    h.feed.start();
    h.advance(0);
    await h.flush();
    expect(h.snapshots.some((s) => s.mode === "rest")).toBe(true);
    expect(h.feed.state().mode).toBe("rest");
    expect(h.feed.state().connected).toBe(false);
    expect(h.sent.length).toBe(0);
  });
});
