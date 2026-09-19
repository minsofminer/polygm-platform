/**
 * Chart and event arithmetic, as pure functions.
 *
 * The client DERIVES 6h and 1d candles from the server's 1h candles rather than asking for them, because the
 * server building two implementations of the same bucketing is two chances to disagree (docs/P09 §D5). That
 * makes this file the only place the derived path exists, which is why it is testable in isolation — and why
 * the tests below care about the two cases a happy path hides: a bucket with no fills, and a series that does
 * not divide evenly into the target width.
 *
 * The event sum is here for the same reason. deviating-by-one-tick is the normal state of a 128-outcome event;
 * the interesting question is never "is the sum exactly 1" but "is the deviation inside what the tick sizes
 * can explain", and that answer has to be computed from the rows the table is showing, not from a second copy
 * of the numbers.
 */
import { microOf, microToDecimal, type Decimal } from "./depth";

export const SERVER_INTERVALS = ["1m", "5m", "15m", "1h"] as const;
export const DERIVED_INTERVALS = ["6h", "1d"] as const;
export type ServerInterval = (typeof SERVER_INTERVALS)[number];
export type DerivedInterval = (typeof DERIVED_INTERVALS)[number];
export type Interval = ServerInterval | DerivedInterval;

export const INTERVAL_MS: Record<Interval, number> = {
  "1m": 60_000,
  "5m": 300_000,
  "15m": 900_000,
  "1h": 3_600_000,
  "6h": 21_600_000,
  "1d": 86_400_000,
};

export type Candle = {
  t: number;
  o: Decimal;
  h: Decimal;
  l: Decimal;
  c: Decimal;
  shares: Decimal;
  trades: number;
  notional: Decimal;
};

export function canServe(interval: Interval): interval is ServerInterval {
  return (SERVER_INTERVALS as readonly string[]).includes(interval);
}

/**
 * Re-bucket a server series into a wider width. The venue clock buckets both times, so a derived bucket's `t`
 * is a multiple of the wider width even when the source started mid-bucket.
 *
 * A bucket appears only when it contains at least one source candle: the gaps stay absent, exactly as they are
 * server-side. The alternative — emitting a flat candle at the previous close — draws a line through a period
 * the market did not trade, which is the one thing a price chart must never do.
 */
export function deriveCandles(candles: Candle[], target: Interval, source: ServerInterval = "1h"): Candle[] {
  const from = INTERVAL_MS[source];
  const to = INTERVAL_MS[target];
  if (to <= from) throw new Error(`deriving ${target} from ${source} would merge nothing`);
  if (to % from !== 0) throw new Error(`${target} is not a whole number of ${source} buckets`);
  const buckets = new Map<number, Candle & { sizeMicro: number; notionalMicro: number }>();
  for (const candle of candles) {
    const key = Math.floor(candle.t / to) * to;
    const existing = buckets.get(key);
    const high = microOf(candle.h);
    const low = microOf(candle.l);
    if (!existing) {
      buckets.set(key, {
        t: key,
        o: candle.o,
        h: microToDecimal(high),
        l: microToDecimal(low),
        c: candle.c,
        shares: candle.shares,
        trades: candle.trades,
        notional: candle.notional,
        sizeMicro: microOf(candle.shares),
        notionalMicro: microOf(candle.notional),
      });
      continue;
    }
    existing.h = microToDecimal(Math.max(microOf(existing.h), high));
    existing.l = microToDecimal(Math.min(microOf(existing.l), low));
    existing.c = candle.c;                       // input is oldest-first, so the last write is the close
    existing.trades += candle.trades;
    existing.sizeMicro += microOf(candle.shares);
    existing.notionalMicro += microOf(candle.notional);
    existing.shares = microToDecimal(existing.sizeMicro);
    existing.notional = microToDecimal(existing.notionalMicro);
  }
  return [...buckets.values()]
    .sort((a, b) => a.t - b.t)
    .map(({ sizeMicro: _s, notionalMicro: _n, ...candle }) => candle);
}

export type EventRow = {
  marketId: string;
  question: string;
  price: Decimal | null;
  bestBid?: Decimal | null;
  bestAsk?: Decimal | null;
  minimumTickSize: Decimal;
  acceptingOrders: boolean;
  volume24h?: Decimal;
  liquidity?: Decimal;
  change24h?: Decimal | null;
  summable?: boolean;
};

export type Invariant = {
  /** Sum of the mids that have one, in micro units. */
  sumMicro: number;
  deviationMicro: number;
  /** N rows x 1 tick each: the widest deviation the quoting grid can explain. */
  toleranceMicro: number;
  withinTolerance: boolean;
  /** Rows with no mid, which are excluded from the sum instead of borrowing a stale price. */
  excluded: string[];
  /** Buying one of every outcome costs this much; under a dollar is an arbitrage, over is the normal case. */
  buyAllCostMicro: number | null;
};

export function eventInvariant(rows: EventRow[]): Invariant {
  let sumMicro = 0;
  let toleranceMicro = 0;
  let buyAllCostMicro = 0;
  let allHaveAsks = rows.length > 0;
  const excluded: string[] = [];
  for (const row of rows) {
    toleranceMicro += microOf(row.minimumTickSize);
    const mid =
      row.price !== null && row.price !== undefined
        ? microOf(row.price)
        : row.bestBid && row.bestAsk
          ? Math.floor((microOf(row.bestBid) + microOf(row.bestAsk)) / 2)
          : null;
    if (mid === null) {
      excluded.push(row.marketId);
      continue;
    }
    sumMicro += mid;
    if (row.bestAsk) buyAllCostMicro += microOf(row.bestAsk);
    else allHaveAsks = false;
  }
  const deviationMicro = sumMicro - 10 ** 6;
  return {
    sumMicro,
    deviationMicro,
    toleranceMicro,
    withinTolerance: Math.abs(deviationMicro) <= toleranceMicro,
    excluded,
    buyAllCostMicro: allHaveAsks && excluded.length === 0 ? buyAllCostMicro : null,
  };
}

/**
 * Which sentence the event header shows, as a decision rather than as a template string in JSX.
 *
 * `opportunity` is a claim about executable orders, so it is only made from the BEST asks/bids (see the API's
 * `buyAllCost`/`sellAllProceeds`) and never from the deviation alone: every event deviates a little, and a
 * banner that shouts at one tick teaches readers to ignore it. The copy keys are returned, not the words.
 */
export function invariantCopy(inv: Invariant): {
  key: "event.invariant.within" | "event.invariant.deviates" | "event.invariant.opportunity" | "event.invariant.partial";
  values: Record<string, string>;
} {
  if (inv.excluded.length > 0) {
    return { key: "event.invariant.partial", values: { count: String(inv.excluded.length) } };
  }
  if (inv.buyAllCostMicro !== null && inv.buyAllCostMicro < 10 ** 6) {
    return { key: "event.invariant.opportunity",
             values: { edge: microToDecimal(10 ** 6 - inv.buyAllCostMicro, 4) } };
  }
  if (!inv.withinTolerance) {
    return { key: "event.invariant.deviates",
             values: { deviation: microToDecimal(Math.abs(inv.deviationMicro), 4),
                       tolerance: microToDecimal(inv.toleranceMicro, 4) } };
  }
  return { key: "event.invariant.within",
           values: { sum: `${microToDecimal(inv.sumMicro, 4)}`, tolerance: microToDecimal(inv.toleranceMicro, 4) } };
}

/**
 * The dead-tail decision for the discovery header.
 *
 * The count is shown, never implied: a filter that silently hides 115 of 135 markets makes every facet and
 * every "loading" state afterwards a lie about how much product there is.
 */
export function tailCopy(longTail: { includeLongTail: boolean; hiddenCount: number; thresholdMicro: number }):
  { key: "markets.longTail.hidden" | "markets.longTail.shown"; values: Record<string, string> } {
  if (longTail.includeLongTail) {
    return { key: "markets.longTail.shown", values: { threshold: microToDecimal(longTail.thresholdMicro, 2) } };
  }
  return { key: "markets.longTail.hidden",
           values: { count: String(longTail.hiddenCount), threshold: microToDecimal(longTail.thresholdMicro, 2) } };
}
