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
import { feedQuery, type WhaleFilters } from "./whales";
import type {
  WhalesCounts,
  CopyConfig,
  CopySourceRow,
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
  const [stamp, setStamp] = useState<Stamp | null>(null);
  const [nonce, setNonce] = useState(0);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void (async () => {
      const res = await request<TraderDossier>({ key: "trader", params: { anon }, query: { window } });
      if (cancelled) return;
      setLoading(false);
      if (!res.ok) {
        // The error is shown next to the numbers that are still on screen: a failed refresh of a dossier is not a
        // reason to blank the dossier, and a blank screen reads as "this trader does not exist".
        setErr(res.error.message);
        return;
      }
      setErr(null);
      setData(res.data);
      setStamp(res.stamp);
    })();
    return () => {
      cancelled = true;
    };
  }, [anon, window, nonce]);
  return { data, err, loading, stamp, reload };
}

/**
 * The whale feed (D4). Same freshness discipline as the tape: keep the rows, say when they are late.
 *
 * `multiple` is the view's tunable relative term; when it is null the request omits it rather than sending zero,
 * because the API's own validation refuses a multiple below 2 and a screen that sends 0 would be answered with a
 * 422 the user cannot act on.
 */
export function useWhales(filters: WhaleFilters, limit = 100) {
  const [rows, setRows] = useState<TerminalFill[]>([]);
  const [counts, setCounts] = useState<WhalesCounts | undefined>(undefined);
  const [severityRule, setSeverityRule] = useState<string>("");
  const [stamp, setStamp] = useState<Stamp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const key = JSON.stringify([filters.scope, filters.marketId, filters.windowMs, filters.multiple, filters.minSeverity, limit]);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void (async () => {
      const res = await request<{ rows: TerminalFill[]; counts: WhalesCounts; severityRule: string; thresholds: Record<string, unknown> }>({
        key: "whales",
        query: { ...feedQuery(filters), limit },
      });
      if (cancelled) return;
      setLoading(false);
      if (!res.ok) {
        setErr(res.error.message);
        return;
      }
      setErr(null);
      setRows(res.data.rows ?? []);
      setCounts(res.data.counts);
      setSeverityRule(res.data.severityRule ?? "");
      setStamp(res.stamp);
    })();
    return () => {
      cancelled = true;
    };
    // `key` is the request's identity; the filters object is rebuilt on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return { rows, counts, severityRule, stamp, err, loading };
}

/** A saved view's creation, with the API's own refusal passed through rather than re-worded. */
export function useCreateWhaleView() {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const create = useCallback(async (body: Record<string, unknown>) => {
    setBusy(true);
    const res = await request<WhaleView>({ key: "createWhaleView", body });
    setBusy(false);
    if (!res.ok) {
      setErr(res.error.message);
      return { ok: false as const, error: res.error.message };
    }
    setErr(null);
    return { ok: true as const, view: res.data };
  }, []);
  return { create, busy, err };
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

/**
 * D7's discovery list.
 *
 * The sort is a parameter rather than a client-side reorder: the API ranks the rows (risk-adjusted by default)
 * and states the ranking, and re-sorting in the browser would produce a list whose order and whose stated rule
 * disagree the moment the two implementations drift.
 */
export function useCopySources(windowDays = 30, sort = "riskAdjusted", onlyCopying = false) {
  const [rows, setRows] = useState<CopySourceRow[]>([]);
  const [ranking, setRanking] = useState("");
  const [sortNote, setSortNote] = useState("");
  const [emptyNote, setEmptyNote] = useState("");
  const [stamp, setStamp] = useState<Stamp | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    let res: Awaited<ReturnType<typeof request<{ rows: CopySourceRow[]; ranking: string; sortNote: string; emptyNote: string }>>>;
    try {
      res = await request<{ rows: CopySourceRow[]; ranking: string; sortNote: string; emptyNote: string }>({
        key: "copySources",
        // QUERY, not `params`: `params` fills `{placeholders}` in the path and throws when there is no such
        // segment. That mistake cost an afternoon here — the hook's `void load()` swallowed the rejection, so
        // the screen showed an empty list and no error, which is the worst possible pair of symptoms. The
        // try/catch below is the other half of the lesson: a rejected read must reach the screen.
        query: { windowDays, sort, onlyCopying: onlyCopying ? "true" : "false" },
      });
    } catch (cause) {
      setErr(cause instanceof Error ? cause.message : "the discovery read failed before it was sent");
      return;
    }
    if (!res.ok) {
      setErr(res.error.message);
      return;
    }
    setErr(null);
    setRows(res.data.rows ?? []);
    setRanking(res.data.ranking ?? "");
    setSortNote(res.data.sortNote ?? "");
    setEmptyNote(res.data.emptyNote ?? "");
    setStamp(res.stamp);
  }, [windowDays, sort, onlyCopying]);

  useEffect(() => {
    void load();
  }, [load]);

  const create = useCallback(async (body: Record<string, unknown>) => {
    setBusy(true);
    const res = await request<{ configId: string; dryRun: boolean }>({ key: "createCopyConfig", body });
    setBusy(false);
    if (!res.ok) {
      setErr(res.error.message);
      return { ok: false as const, error: res.error.message };
    }
    setErr(null);
    // The response's own `dryRun` is the field the screen reports, not an assumption that creation was safe:
    // if the API ever answered otherwise, the screen would say so.
    return { ok: true as const, id: res.data.configId, dryRun: res.data.dryRun === true };
  }, []);

  return { rows, ranking, sortNote, emptyNote, stamp, busy, err, create, reload: load };
}

/** The guard rails, including the only path by which a config stops being a dry run. */
export function useSetCopyGuards() {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const set = useCallback(async (body: Record<string, unknown>) => {
    setBusy(true);
    const res = await request<{ configId: string; dryRun: boolean }>({ key: "copyGuards", body });
    setBusy(false);
    if (!res.ok) {
      // The 409s here are sentences from the API (no acknowledgement, no dry-run history) and they are shown as
      // they arrive: a client that rewrote them would be a second place the rule lives.
      setErr(res.error.message);
      return { ok: false as const, error: res.error.message };
    }
    setErr(null);
    return { ok: true as const, dryRun: res.data.dryRun === true };
  }, []);

  /** The global stop: every live config, one guard call each. Sequential, so a refusal stops the rest. */
  const setMany = useCallback(
    async (bodies: Record<string, unknown>[]) => {
      for (const body of bodies) {
        const out = await set(body);
        if (!out.ok) return out;
      }
      return { ok: true as const, dryRun: true };
    },
    [set],
  );

  return { set, setMany, busy, err };
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
  const [err, setErr] = useState<string | null>(null);
  const [stamp, setStamp] = useState<Stamp | null>(null);
  const refresh = useCallback(async () => {
    const res = await request<{ items: WhaleView[] }>({ key: "whaleViews" });
    if (!res.ok) {
      setErr(res.error.message);
      return;
    }
    setErr(null);
    setViews(res.data.items ?? []);
    setStamp(res.stamp);
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  return { views, refresh, err, stamp };
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
  "copySources",
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
