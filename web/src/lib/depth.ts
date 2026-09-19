/**
 * The ladder's arithmetic, as pure functions.
 *
 * All of it is integer micro-units. Prices and sizes arrive from the API as DECIMAL STRINGS (`"0.001"`,
 * `"232978723.404255"`) precisely so that a float cannot enter the money path, and the moment a component
 * does `parseFloat(row.price) * parseFloat(row.shares)` that decision is undone — in a place no reviewer looks,
 * because the result renders fine and is wrong in the eighth digit.
 *
 * Two rules from web/DESIGN.md that live here rather than in the view, because they are decisions and not
 * styling:
 *   - aggregation rounds a bid DOWN and an ask UP, so an aggregated level never shows a price the reader could
 *     not have got at the venue;
 *   - an empty side is a STATE. P01 measured a live market with 94 ask levels at 0.001 and zero bids; a ladder
 *     that renders that as a failed fetch tells the reader the market is broken when it is simply finished.
 */

import { priceToUnits, type TickSize } from "@/money/cents";

export type Decimal = string;

/** Parsed micro-units: `"0.001"` -> 1000. Mirrors the API's `_micro_of`, including its refusal to round. */
export function microOf(value: Decimal | number): number {
  const text = typeof value === "number" ? value.toString() : value;
  if (!/^-?\d+(\.\d+)?$/.test(text)) throw new Error(`not a decimal string: ${JSON.stringify(value)}`);
  const negative = text.startsWith("-");
  const [whole, frac = ""] = (negative ? text.slice(1) : text).split(".");
  const micro = Number(whole) * 10 ** 6 + Number((frac + "000000").slice(0, 6));
  return negative ? -micro : micro;
}

/**
 * Tick units for a `kind="price"` renderer: `priceUnitsOf("0.425", "0.001")` is 425.
 *
 * This exists because the ladder's own arithmetic is in micro-units and the renderer's contract is tick units,
 * and getting that wrong is invisible: feeding micro-units to a 0.001-tick renderer prints "1.000" instead of
 * ".001" — a plausible-looking price, ten times the size, on a market where the difference is the whole trade.
 * The conversion is delegated to `src/money/cents.ts` so there is still exactly one parser of price strings.
 */
export function priceUnitsOf(price: Decimal, tick: Decimal): number {
  return priceToUnits(price, tick as TickSize);
}

/**
 * Whole units for a `kind="size"` renderer. Sizes arrive as decimals (`"232978723.404255"`); the ladder shows
 * shares, so the fraction is dropped toward zero rather than rounded up — a displayed size may never be larger
 * than the size that is actually there.
 */
export function shareUnitsOf(value: Decimal | number): number {
  const text = typeof value === "number" ? value.toString() : value;
  if (!/^-?\d+(\.\d+)?$/.test(text)) throw new Error(`not a decimal string: ${JSON.stringify(value)}`);
  const negative = text.startsWith("-");
  const [whole = "0"] = (negative ? text.slice(1) : text).split(".");
  const units = Number(whole);
  return negative ? -units : units;
}

export function microToDecimal(micro: number, decimals = 6): Decimal {
  const negative = micro < 0;
  const abs = Math.abs(micro);
  const whole = Math.floor(abs / 10 ** 6);
  const frac = String(abs % 10 ** 6).padStart(6, "0").slice(0, decimals).replace(/0+$/, "");
  return `${negative ? "-" : ""}${whole}${frac ? "." + frac : ""}`;
}

export type RawLevel = { price: Decimal; shares: Decimal; levels?: number };
export type LadderLevel = {
  price: Decimal;
  shares: Decimal;
  levels: number;
  /** Cumulative size from the top of book, as a decimal string. The bar width is a fraction of `maxCum`. */
  cumShares: Decimal;
  /** 0..1 of the largest cumulative figure on either side, precomputed so the view does no arithmetic. */
  fraction: number;
};

/** A price is on the grid when it is an exact multiple of the tick. Nothing else is tradeable. */
export function onGrid(price: Decimal, tick: Decimal): boolean {
  const p = microOf(price);
  const t = microOf(tick);
  return t > 0 && p % t === 0;
}

/**
 * Fold raw levels into price buckets.
 *
 * `step === null` is the raw tick, which for a 0.001 market is a thousand buckets per dollar — the API serves
 * 24 of them, which is why the default aggregate exists at all. Bucketing is directional (see the header).
 */
export function aggregateLevels(
  levels: RawLevel[],
  stepMicro: number | null,
  side: "bid" | "ask",
): Array<{ price: Decimal; shares: Decimal; levels: number }> {
  if (stepMicro === null) {
    return levels.map((l) => ({ price: l.price, shares: l.shares, levels: l.levels ?? 1 }));
  }
  const buckets = new Map<number, { shares: number; levels: number }>();
  for (const level of levels) {
    const price = microOf(level.price);
    // Ceil for asks, floor for bids: the bucket's number is a promise ("you can trade at least this well
    // inside this band"), and rounding an ask down would promise a price that is not there.
    const key = side === "ask" ? Math.ceil(price / stepMicro) * stepMicro : Math.floor(price / stepMicro) * stepMicro;
    const slot = buckets.get(key) ?? { shares: 0, levels: 0 };
    slot.shares += microOf(level.shares);
    slot.levels += level.levels ?? 1;
    buckets.set(key, slot);
  }
  const ordered = [...buckets.entries()].sort((a, b) => (side === "ask" ? a[0] - b[0] : b[0] - a[0]));
  return ordered.map(([price, slot]) => ({
    price: microToDecimal(price),
    shares: microToDecimal(slot.shares),
    levels: slot.levels,
  }));
}

/** Add cumulative depth and the bar fraction. `scale` is the largest cumulative on EITHER side, so the two
 *  sides share one axis — two axes make a 10x bid wall look level with a 1x ask wall. */
export function withDepth(
  levels: Array<{ price: Decimal; shares: Decimal; levels: number }>,
  scale: number,
): LadderLevel[] {
  let running = 0;
  return levels.map((level) => {
    running += microOf(level.shares);
    return {
      ...level,
      cumShares: microToDecimal(running),
      fraction: scale > 0 ? Math.min(1, running / scale) : 0,
    };
  });
}

export function maxCumulative(...sides: Array<Array<{ price: Decimal; shares: Decimal; levels: number }>>): number {
  let most = 0;
  for (const side of sides) {
    let running = 0;
    for (const level of side) running += microOf(level.shares);
    most = Math.max(most, running);
  }
  return most;
}

export type Spread = {
  bestBid: Decimal | null;
  bestAsk: Decimal | null;
  mid: Decimal | null;
  spread: Decimal | null;
  /** Spread as a percentage of the mid, in basis points, integer. Null when there is no two-sided quote. */
  spreadBp: number | null;
  /** Cents, integer, for the human-readable half of the row. */
  spreadCents: number | null;
};

export function spreadOf(bids: RawLevel[], asks: RawLevel[]): Spread {
  const topBid = bids[0];
  const topAsk = asks[0];
  const bid = topBid ? microOf(topBid.price) : null;
  const ask = topAsk ? microOf(topAsk.price) : null;
  if (bid === null || ask === null) {
    return { bestBid: bid === null ? null : microToDecimal(bid), bestAsk: ask === null ? null : microToDecimal(ask),
             mid: null, spread: null, spreadBp: null, spreadCents: null };
  }
  const mid = Math.floor((bid + ask) / 2);
  const spread = ask - bid;
  return {
    bestBid: microToDecimal(bid),
    bestAsk: microToDecimal(ask),
    mid: microToDecimal(mid),
    spread: microToDecimal(spread),
    spreadBp: mid > 0 ? Math.round((spread * 10_000) / mid) : null,
    // Cents per share, rounded to the cent: 0.001-wide spread is 0.1 cents, which reads as "0.1¢" and not 0.
    spreadCents: Math.round(spread / 10_000),
  };
}

/**
 * Depth-weighted imbalance over the visible ladder: (bids - asks) / (bids + asks), in `-1..1`.
 *
 * Depth-weighted, not count-weighted: twenty one-share bids against one thousand-share ask is not "balanced",
 * and a ratio computed from level COUNTS says it is. Null when either side is empty — zero is a number that
 * means "perfectly balanced", and a one-sided book is the opposite of balanced.
 */
export function imbalance(bids: RawLevel[], asks: RawLevel[]): number | null {
  const sum = (levels: RawLevel[]) => levels.reduce((total, l) => total + microOf(l.shares), 0);
  const bidShares = sum(bids);
  const askShares = sum(asks);
  if (bidShares === 0 || askShares === 0) return null;
  return (bidShares - askShares) / (bidShares + askShares);
}

export type OneSided = {
  side: "asks-only" | "bids-only";
  levels: number;
  notional: Decimal;
  /** The copy key the ladder renders. Machine-readable here; the words live in the dictionary. */
  reason: "no-bids" | "no-asks";
  /** The price the reader must NOT be allowed to believe they can buy at. */
  topOfBook: Decimal;
};

/** Detect the one-sided state, and describe it in numbers rather than in adjectives. */
export function oneSidedOf(bids: RawLevel[], asks: RawLevel[]): OneSided | null {
  const notional = (levels: RawLevel[]) =>
    levels.reduce((total, l) => total + Math.floor((microOf(l.price) * microOf(l.shares)) / 10 ** 6), 0);
  const topAsk = asks[0];
  const topBid = bids[0];
  if (bids.length === 0 && topAsk) {
    return { side: "asks-only", levels: asks.length, notional: microToDecimal(notional(asks)),
             reason: "no-bids", topOfBook: topAsk.price };
  }
  if (asks.length === 0 && topBid) {
    return { side: "bids-only", levels: bids.length, notional: microToDecimal(notional(bids)),
             reason: "no-asks", topOfBook: topBid.price };
  }
  return null;
}

/** How much notional a level represents, for the depth chart's axis (integer micro, then formatted upstream). */
export function levelNotional(level: RawLevel): number {
  return Math.floor((microOf(level.price) * microOf(level.shares)) / 10 ** 6);
}
