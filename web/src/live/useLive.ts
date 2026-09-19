/**
 * `useLive<T>()` — the one hook a screen uses to read anything that can go stale. It exposes
 * `data | stale | disconnected` (P08 D8) plus the reason and the gap, and it is the only place that decides
 * `canTrade`. A screen that fetched a price on its own would be an unguarded price display, which is what
 * the phase's constraint list forbids, so the API surface is deliberately narrow.
 */
"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { canTradeGivenFreshness, type Freshness } from "@/api/envelope";
import { request } from "@/api/client";
import type { RouteKey } from "@/api/routes";
import { env } from "@/lib/env";
import { createFeed, type FeedSnapshot, type Transport } from "./ws";
import { isTma } from "@/telegram/bridge";

const transport: Transport = {
  connect: (url, handlers) => {
    const socket = new WebSocket(url);
    socket.onopen = () => handlers.onOpen();
    socket.onmessage = (event) => handlers.onMessage(String(event.data));
    socket.onclose = () => handlers.onClose("closed");
    socket.onerror = () => handlers.onClose("error");
    return {
      send: (text) => {
        if (socket.readyState === WebSocket.OPEN) socket.send(text);
      },
      close: () => socket.close(),
    };
  },
  now: () => Date.now(),
  schedule: (ms, fn) => {
    const id = window.setTimeout(fn, ms);
    return () => window.clearTimeout(id);
  },
};

export type LiveState<T> = FeedSnapshot<T> & {
  canTrade: boolean;
  /** What the indicator says, in words, including why trading is off. */
  says: string;
  source: "ws" | "rest";
};

export function useLive<T = unknown>(key: RouteKey, params?: Record<string, string | number>): LiveState<T> {
  const [snap, setSnap] = useState<FeedSnapshot<T>>({
    data: null,
    stamp: null,
    freshness: "unknown",
    mode: "down",
    connected: false,
    gapCount: 0,
    lastGapCount: 0,
    resyncing: false,
    blockReason: "the feed has not started",
  });
  const paramsKey = JSON.stringify(params ?? {});

  const feed = useMemo(() => {
    return createFeed({
      transport,
      url: env.hasWebSocket ? `${env.wsOrigin}/v1/live/${key}` : "",
      resync: async () => {
        const out = await request<Record<string, unknown>>({
          key,
          ...(params ? { params: JSON.parse(paramsKey) as Record<string, string | number> } : {}),
          timeoutMs: 6_000,
        });
        if (!out.ok) throw new Error(out.error.code);
        // Tagging the source is what stops the REST path from flashing: the row arrived from a cache, and a
        // flash is a claim that something just happened.
        return { body: JSON.stringify({ ...out.data, ...(out.stamp ? { asOf: out.stamp.asOf, staleAfter: out.stamp.staleAfter, cache: { ttlMs: out.stamp.ttlMs } } : {}) }) };
      },
      parse: (raw) => {
        const j = JSON.parse(raw) as Record<string, unknown>;
        return { payload: j, seq: typeof j.seq === "number" ? j.seq : undefined };
      },
      onSnapshot: (next) => setSnap(next as FeedSnapshot<unknown> as FeedSnapshot<T>),
    });
  }, [key, paramsKey]);

  useEffect(() => {
    feed.start();
    return () => feed.stop();
  }, [feed]);

  const freshness: Freshness = snap.freshness;
  const canTrade = snap.mode !== "down" && canTradeGivenFreshness(freshness);
  const says = useMemo(() => {
    if (snap.gapCount > 0) return `${snap.gapCount} fills not shown`;
    if (snap.blockReason) return snap.blockReason;
    return snap.mode === "rest" ? "polling (cached upstream)" : "live";
  }, [snap.gapCount, snap.blockReason, snap.mode]);

  return { ...snap, freshness, canTrade, says, source: snap.mode === "ws" ? "ws" : "rest" };
}

/** The trade gate, as a value the shell renders and the ticket reads — one computation, two consumers.
 *  Inside the webview, a stale feed has an extra consequence: the user cannot leave to check another tab,
 *  which is why the sentence is a full sentence and not a dot. */
export function useTradeGate(feed: { canTrade: boolean; says: string; mode: string }) {
  return {
    canTrade: feed.canTrade,
    whyNot: feed.canTrade ? null : feed.says,
    inWebview: isTma(),
  };
}
