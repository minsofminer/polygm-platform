"use client";
/**
 * The terminal's data layer. One hook per surface, and each of them is a *decision about freshness* rather
 * than a fetch:
 *
 *  - **The tape polls at a window, not at a rate.** The poll interval is derived from the measured arrival rate
 *    so a quiet market is not polled 4× a second; the queue, the coalescing and the pause live in
 *    `useTape`/`coalesceFills` and are the only place rows are ordered.
 *  - **A failed read keeps the last good data and says so.** `status: "disconnected"` with the stamp of the data
 *    still on screen — a tape that empties on one dropped request teaches a user that the market stopped.
 *  - **Wallet Radar batches and is quota-aware.** Up to ten markets in one call; over the latency budget the
 *    API answers with a job id and this hook polls it rather than re-running the scan.
 *  - **Every write goes through the ledger's route keys**, never a hand-written path, so the P10 gate can hold
 *    the routes and the screens to the same list.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { request } from "@/api/client";
import type { Stamp } from "@/api/envelope";
import type { RouteKey } from "@/api/routes";
import { arrivalRate, coalesceFills, MAX_ROWS, type TapeFilters } from "./tape";
import type {
  CopyConfig,
  CopyMonitor,
  Portfolio,
  RadarResult,
  TapeFacets,
  TerminalFill,
  TraderDossier,
  WhaleView,
} from "./wire";

export type LiveStatus = "live" | "disconnected";

/** One page of the durable tape, as `/v1/tape/fills` answers it. */
export type TapePage = { rows: TerminalFill[]; nextCursor: number | null; counts: unknown; asOf: number;
                         staleAfter: number };

/** The poll interval for a measured arrival rate: a busy tape is polled more often, and a dead one, less. */
export function pollMsFor(ratePerSecond: number): number {
  if (ratePerSecond >= 40) return 500;
  if (ratePerSecond >= 20) return 750;
  if (ratePerSecond >= 5) return 1_500;
  return 3_000;
}

type TapeState = {
  rows: TerminalFill[];
  facets: TapeFacets | null;
  queuedCount: number;
  paused: boolean;
  setPaused: (v: boolean) => void;
  status: LiveStatus;
  stamp: Stamp | null;
  err: string | null;
};

/** The live tape: windowed reads, coalesced rows, a pause that counts, and the last good stamp on failure. */
export function useTape(filters: TapeFilters, windowMs = 3_600_000, limit = 120): TapeState {
  const [buffer, setBuffer] = useState<TerminalFill[]>([]);
  const [queued, setQueued] = useState<TerminalFill[]>([]);
  const [facets, setFacets] = useState<TapeFacets | null>(null);
  const [paused, setPaused] = useState(false);
  const [status, setStatus] = useState<LiveStatus>("live");
  const [stamp, setStamp] = useState<Stamp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const lastFlush = useRef(0);
  const seen = useRef<Set<string>>(new Set());
  const cursor = useRef<number | undefined>(undefined);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;

  const ingest = useCallback((rows: TerminalFill[], since: number | undefined) => {
    const fresh = rows.filter((row) => {
      const key = `${row.conditionId}:${row.tsMs}:${row.anonWallet}:${row.notionalMicro}`;
      if (seen.current.has(key)) return false;
      seen.current.add(key);
      return true;
    });
    if (seen.current.size > MAX_ROWS * 4) seen.current = new Set([...seen.current].slice(-MAX_ROWS));
    if (since === undefined) {
      setBuffer((prev) => [...fresh, ...prev].slice(0, MAX_ROWS));
      return;
    }
    setQueued((prev) => [...fresh, ...prev]);
  }, []);

  useEffect(() => {
    let cancelled = false;
    seen.current = new Set();
    cursor.current = undefined;
    setBuffer([]);
    setQueued([]);
    const load = async (first: boolean) => {
      const res = await request<TapePage>({
        key: "tapeFills",
        query: {
          windowMs,
          limit,
          marketId: filtersRef.current.marketId || undefined,
          side: filtersRef.current.side || undefined,
          category: filtersRef.current.category || undefined,
          label: filtersRef.current.label || undefined,
          wallet: filtersRef.current.wallet || undefined,
          minNotionalMicro: filtersRef.current.minNotionalMicro || undefined,
          since: first ? undefined : cursor.current,
        },
      });
      if (cancelled) return;
      if (!res.ok) {
        // Keep the rows we have: an empty tape on one dropped request reads as "the market stopped".
        setStatus("disconnected");
        setErr(res.error.message);
        return;
      }
      setStatus("live");
      setErr(null);
      setStamp(res.stamp);
      cursor.current = typeof res.data.nextCursor === "number" ? res.data.nextCursor : undefined;
      ingest(res.data.rows ?? [], first ? undefined : cursor.current);
    };
    void load(true);
    let timer = 0;
    const tick = () => {
      void load(false).finally(() => {
        if (!cancelled) timer = window.setTimeout(tick, pollMsFor(arrivalRate(buffer, Date.now())));
      });
    };
    timer = window.setTimeout(tick, pollMsFor(0));
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // `buffer` is deliberately not a dependency: the interval is re-derived from the arrival rate on each tick,
    // and closing over it here would restart the loop on every batch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ingest, windowMs, limit, filters.marketId, filters.side, filters.category, filters.label, filters.wallet,
      filters.minNotionalMicro]);

  useEffect(() => {
    const load = async () => {
      const res = await request<TapeFacets>({
        key: "tapeFacets",
        query: { windowMs, marketId: filters.marketId || undefined },
      });
      if (res.ok) setFacets(res.data);
    };
    void load();
    const timer = window.setInterval(() => void load(), 5_000);
    return () => window.clearInterval(timer);
  }, [windowMs, filters.marketId]);

  useEffect(() => {
    const release = () => {
      const out = coalesceFills(buffer, queued, {
        ratePerSecond: arrivalRate(buffer, Date.now()),
        nowMs: Date.now(),
        lastFlushMs: lastFlush.current,
        paused,
      });
      if (out.released > 0) lastFlush.current = Date.now();
      if (out.released > 0 || (paused && out.withheld !== queued.length)) {
        setBuffer(out.buffer);
        setQueued(out.queued);
      }
    };
    const timer = window.setInterval(release, 200);
    return () => window.clearInterval(timer);
  }, [buffer, queued, paused]);

  return { rows: buffer, facets, queuedCount: queued.length, paused, setPaused, status, stamp, err };
}

/** A trader's dossier. The window is part of the request, and the whole metric set moves with it. */
export function useDossier(anon: string, window: string) {
  const [data, setData] = useState<TraderDossier | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void (async () => {
      const res = await request<TraderDossier>({ key: "trader", params: { anon }, query: { window } });
      if (cancelled) return;
      setLoading(false);
      if (!res.ok) {
        setErr(res.error.message);
        return;
      }
      setErr(null);
      setData(res.data);
    })();
    return () => {
      cancelled = true;
    };
  }, [anon, window]);
  return { data, err, loading };
}

export function usePortfolio() {
  const [data, setData] = useState<Portfolio | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    const res = await request<Portfolio>({ key: "portfolio" });
    if (!res.ok) {
      setErr(res.error.message);
      return;
    }
    setErr(null);
    setData(res.data);
  }, []);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15_000);
    return () => window.clearInterval(timer);
  }, [refresh]);
  return { data, err, refresh };
}

export function useCopyConfigs() {
  const [configs, setConfigs] = useState<CopyConfig[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    const res = await request<{ items: CopyConfig[] }>({ key: "copyConfigs" });
    if (!res.ok) {
      setErr(res.error.message);
      return;
    }
    setErr(null);
    setConfigs(res.data.items ?? []);
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  return { configs, err, refresh };
}

export function useCopyMonitor(configId: string | null) {
  const [data, setData] = useState<CopyMonitor | null>(null);
  useEffect(() => {
    if (!configId) {
      setData(null);
      return;
    }
    let cancelled = false;
    const load = async () => {
      const res = await request<CopyMonitor>({ key: "copyMonitor", query: { configId } });
      if (!cancelled && res.ok) setData(res.data);
    };
    void load();
    const timer = window.setInterval(() => void load(), 5_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [configId]);
  return data;
}

export function useWhaleViews() {
  const [views, setViews] = useState<WhaleView[]>([]);
  const refresh = useCallback(async () => {
    const res = await request<{ items: WhaleView[] }>({ key: "whaleViews" });
    if (res.ok) setViews(res.data.items ?? []);
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  return { views, refresh };
}

/**
 * Wallet Radar.
 *
 * Cost control is part of the hook: up to ten markets in one request, a per-day quota from the plan, and a job
 * id when the scan would exceed the latency budget. The quota is displayed from the response rather than
 * invented here, because the number a user sees has to be the number the server enforces.
 */
export function useRadar() {
  const [result, setResult] = useState<RadarResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const run = useCallback(async (marketIds: string[]) => {
    setBusy(true);
    const res = await request<RadarResult>({
      key: "walletRadar",
      body: { marketIds: marketIds.slice(0, 10) },
    });
    if (!res.ok) {
      setBusy(false);
      setErr(res.error.message);
      return;
    }
    setErr(null);
    setResult(res.data);
    const data = res.data;
    if (data.quota?.jobId) {
      // Over the latency budget the API answers with a job: poll it rather than re-running a scan whose cost
      // the user has already paid for.
      const poll = window.setInterval(async () => {
        const out = await request<RadarResult>({ key: "walletRadarJob", params: { job_id: data.quota.jobId as string } });
        if (out.ok) {
          setResult(out.data);
          if (!out.data.quota?.jobId) {
            window.clearInterval(poll);
            setBusy(false);
          }
        }
      }, 2_000);
      return;
    }
    setBusy(false);
  }, []);
  return { result, busy, err, run };
}

/** Keys the terminal is allowed to ask for. Exported so the gate can compare this list to the ledger. */
export const TERMINAL_ROUTES: RouteKey[] = [
  "tapeFills",
  "tapeFacets",
  "whales",
  "trader",
  "copyConfigs",
  "createCopyConfig",
  "copyGuards",
  "copyMonitor",
  "portfolio",
  "whaleViews",
  "createWhaleView",
  "walletRadar",
  "walletRadarJob",
];

export function useNow(intervalMs = 1_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/** The columns a CSV export must contain (from the portfolio payload), so the file matches the table. */
export function csvColumns(portfolio: Portfolio | null): string[] {
  return portfolio?.csv.columns ?? [];
}
