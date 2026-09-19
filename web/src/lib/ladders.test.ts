import { describe, expect, it } from "vitest";
import { deriveCandles, eventInvariant, invariantCopy, tailCopy, type Candle } from "./ladders";

function candle(t: number, o: string, h: string, l: string, c: string, shares: string, trades: number): Candle {
  return { t, o, h, l, c, shares, trades, notional: "1" };
}

const HOUR = 3_600_000;

describe("deriveCandles", () => {
  it("merges 1h candles into 6h buckets on the venue clock", () => {
    const base = 1_700_000_000_000 - (1_700_000_000_000 % (6 * HOUR));
    const series = [0, 1, 2, 3, 4, 5, 6].map((i) =>
      candle(base + i * HOUR, "0.5", `0.6${i}`, "0.4", `0.5${i}`, "10", 3),
    );
    const sixHourly = deriveCandles(series, "6h");
    expect(sixHourly).toHaveLength(2);
    expect(sixHourly[0]!.t % (6 * HOUR)).toBe(0);
    expect(sixHourly[0]?.o).toBe("0.5");
    expect(sixHourly[0]?.c).toBe("0.55");                 // the bucket's LAST source candle is the close
    expect(sixHourly[0]?.trades).toBe(18);
    expect(Number(sixHourly[0]?.shares)).toBe(60);        // sizes sum
    expect(sixHourly[1]?.trades).toBe(3);
  });

  it("keeps a gap absent instead of flattening it", () => {
    const base = 1_700_000_000_000 - (1_700_000_000_000 % (6 * HOUR));
    const series = [candle(base, "0.5", "0.5", "0.5", "0.5", "1", 1),
                    candle(base + 12 * HOUR, "0.6", "0.6", "0.6", "0.6", "1", 1)];
    const sixHourly = deriveCandles(series, "6h");
    expect(sixHourly.map((c) => c.t)).toEqual([base, base + 12 * HOUR]);
    expect(sixHourly).toHaveLength(2);                   // NOT three with a fabricated middle bucket
  });

  it("takes the extremes across the merged candles", () => {
    const base = 0;
    const series = [candle(base, "0.5", "0.55", "0.45", "0.5", "1", 1),
                    candle(base + HOUR, "0.5", "0.9", "0.2", "0.4", "1", 1)];
    const [merged] = deriveCandles(series, "1d");
    expect(merged?.h).toBe("0.9");
    expect(merged?.l).toBe("0.2");
    expect(merged?.c).toBe("0.4");
  });

  it("refuses a target that is not a whole number of source buckets", () => {
    expect(() => deriveCandles([], "1h", "1h")).toThrow(/merge nothing/);
  });
});

describe("eventInvariant", () => {
  const rows = [
    { marketId: "a", question: "a", price: "0.34", bestAsk: "0.341", minimumTickSize: "0.001", acceptingOrders: true },
    { marketId: "b", question: "b", price: "0.20", bestAsk: "0.201", minimumTickSize: "0.001", acceptingOrders: true },
    { marketId: "c", question: "c", price: "0.462", bestAsk: "0.463", minimumTickSize: "0.001", acceptingOrders: true },
  ];

  it("sums the mids and bounds the deviation by one tick per outcome", () => {
    const inv = eventInvariant(rows);
    expect(inv.sumMicro).toBe(1_002_000);
    expect(inv.deviationMicro).toBe(2000);
    expect(inv.toleranceMicro).toBe(3000);
    expect(inv.withinTolerance).toBe(true);
    expect(inv.excluded).toEqual([]);
  });

  it("excludes a row with no mid rather than borrowing its last trade", () => {
    const inv = eventInvariant([...rows, { marketId: "d", question: "d", price: null,
                                           minimumTickSize: "0.001", acceptingOrders: false }]);
    expect(inv.excluded).toEqual(["d"]);
    expect(inv.sumMicro).toBe(1_002_000);
  });

  it("only prices a buy-all edge when every row has an executable ask", () => {
    const partial = eventInvariant(rows.map((r, i) => (i === 0 ? { ...r, bestAsk: null } : r)));
    expect(partial.buyAllCostMicro).toBeNull();
  });

  it("reports the edge when buying every outcome costs less than a dollar", () => {
    const cheap = rows.map((r) => ({ ...r, bestAsk: "0.30" }));
    const inv = eventInvariant(cheap);
    expect(inv.buyAllCostMicro).toBe(900_000);
    const copy = invariantCopy(inv);
    expect(copy.key).toBe("event.invariant.opportunity");
    expect(copy.values.edge).toBe("0.1");
  });

  it("does not shout about a deviation the tick grid can explain", () => {
    const copy = invariantCopy(eventInvariant(rows));
    expect(copy.key).toBe("event.invariant.within");
  });

  it("says which rows it could not sum", () => {
    const copy = invariantCopy({ sumMicro: 0, deviationMicro: -(10 ** 6), toleranceMicro: 1000,
                                 withinTolerance: false, excluded: ["a", "b"], buyAllCostMicro: null });
    expect(copy.key).toBe("event.invariant.partial");
    expect(copy.values.count).toBe("2");
  });
});

describe("tailCopy", () => {
  it("names the number hidden and the threshold, never just hides the rows", () => {
    const copy = tailCopy({ includeLongTail: false, hiddenCount: 115, thresholdMicro: 1_000_000_000 });
    expect(copy.key).toBe("markets.longTail.hidden");
    expect(copy.values).toEqual({ count: "115", threshold: "1000" });
  });

  it("says the tail is showing when it is", () => {
    expect(tailCopy({ includeLongTail: true, hiddenCount: 0, thresholdMicro: 1_000_000_000 }).key)
      .toBe("markets.longTail.shown");
  });
});
