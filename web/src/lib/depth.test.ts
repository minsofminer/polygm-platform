import { describe, expect, it } from "vitest";
import { aggregateLevels, imbalance, maxCumulative, microOf, microToDecimal, onGrid, oneSidedOf, spreadOf, withDepth, priceUnitsOf, shareUnitsOf } from "./depth";

/** The P01 observation the one-sided treatment exists for: 94 ask levels at 0.001 and nothing else. */
const ASKS_ONLY = Array.from({ length: 94 }, (_, i) => ({
  // The seed's own construction, restated so the two cannot drift: price_i = i x 0.001 and shares_i = S / i,
  // which makes every level's notional equal (S x 0.001) and the ladder sum exactly $21.9M. Getting this wrong
  // in the FIXTURE is easy - a first version here divided the target by (i+1) as a share count and produced a
  // ladder worth $2M, which the test then read as a bug in the product.
  price: microToDecimal((i + 1) * 1000),
  shares: microToDecimal(Math.floor(21_900_000 * 10 ** 9 / 94 / (i + 1))),
}));

const BIDS = [
  { price: "0.499", shares: "117.158728" },
  { price: "0.498", shares: "6.969696" },
];
const ASKS = [
  { price: "0.501", shares: "105.442857" },
  { price: "0.502", shares: "6.272727" },
];

describe("micro units", () => {
  it("parses the decimal strings the API sends without going through a float", () => {
    expect(microOf("0.001")).toBe(1000);
    expect(microOf("0.5")).toBe(500_000);
    expect(microOf("232978723.404255")).toBe(232_978_723_404_255);
    expect(microOf("5")).toBe(5_000_000);
    expect(microOf("-0.25")).toBe(-250_000);
  });

  it("refuses anything that is not a decimal string", () => {
    expect(() => microOf("1e-3")).toThrow(/not a decimal string/);
    expect(() => microOf("")).toThrow();
  });

  it("round-trips through the display form", () => {
    expect(microToDecimal(1000)).toBe("0.001");
    expect(microToDecimal(500_000)).toBe("0.5");
    expect(microToDecimal(-250_000)).toBe("-0.25");
    expect(microToDecimal(0)).toBe("0");
  });
});

describe("aggregateLevels", () => {
  const ladder = [
    { price: "0.001", shares: "1" },
    { price: "0.002", shares: "2" },
    { price: "0.051", shares: "3" },
  ];

  it("rounds bids DOWN and asks UP, so a level never promises a price that is not there", () => {
    const bids = aggregateLevels(ladder, 50_000, "bid");
    expect(bids.map((l) => l.price)).toEqual(["0.05", "0"]);
    const asks = aggregateLevels(ladder, 50_000, "ask");
    expect(asks.map((l) => l.price)).toEqual(["0.05", "0.1"]);
  });

  it("sums sizes and level counts inside a bucket and orders best-first", () => {
    const asks = aggregateLevels(ladder, 50_000, "ask");
    const half = asks.find((l) => l.price === "0.05");
    expect(half?.shares).toBe("3");
    const bids = aggregateLevels(ladder, 50_000, "bid");
    const below = bids.find((l) => l.price === "0");
    expect(below?.shares).toBe("3");        // 0.001 and 0.002 both floor into the 0 bucket
    expect(below?.levels).toBe(2);
  });

  it("passes the raw ladder straight through when the step is null", () => {
    expect(aggregateLevels(ladder, null, "ask")).toHaveLength(3);
  });
});

describe("depth bars", () => {
  it("shares one scale across both sides so a wall cannot look level with a sliver", () => {
    const bids = withDepth([{ price: "0.49", shares: "100", levels: 1 }], maxCumulative(
      [{ price: "0.49", shares: "100", levels: 1 }],
      [{ price: "0.51", shares: "1", levels: 1 }],
    ));
    const asks = withDepth([{ price: "0.51", shares: "1", levels: 1 }], 100_000_000);
    expect(bids[0]?.fraction).toBe(1);
    expect(asks[0]?.fraction).toBeCloseTo(0.01, 5);
  });

  it("accumulates from the top of book", () => {
    const [first, second] = withDepth(
      [{ price: "0.499", shares: "2", levels: 1 }, { price: "0.498", shares: "3", levels: 1 }],
      5_000_000,
    );
    expect(first?.cumShares).toBe("2");
    expect(second?.cumShares).toBe("5");
  });
});

describe("spread", () => {
  it("reports the mid, the spread and its basis points from integer micro units", () => {
    const s = spreadOf(BIDS, ASKS);
    expect(s.bestBid).toBe("0.499");
    expect(s.bestAsk).toBe("0.501");
    expect(s.mid).toBe("0.5");
    expect(s.spread).toBe("0.002");
    expect(s.spreadBp).toBe(40);
    expect(s.spreadCents).toBe(0);          // 0.2 cents rounds to 0¢ and the view renders "0.2¢" instead
  });

  it("has no mid when a side is missing", () => {
    const s = spreadOf([], ASKS);
    expect(s.mid).toBeNull();
    expect(s.spread).toBeNull();
    expect(s.spreadBp).toBeNull();
    expect(s.bestAsk).toBe("0.501");
  });
});

describe("imbalance", () => {
  it("is depth-weighted, not count-weighted", () => {
    const many = Array.from({ length: 20 }, () => ({ price: "0.4", shares: "1" }));
    expect(imbalance(many, [{ price: "0.6", shares: "1000" }])).toBeLessThan(-0.9);
  });

  it("is null on a one-sided book, not zero", () => {
    expect(imbalance(ASKS_ONLY, [])).toBeNull();
    expect(imbalance([], ASKS_ONLY)).toBeNull();
  });
});

describe("oneSidedOf", () => {
  it("describes the P01 market: 94 asks, no bids, $21.9M, and a top of book a user must not buy blind", () => {
    const verdict = oneSidedOf([], ASKS_ONLY);
    expect(verdict).not.toBeNull();
    expect(verdict!.side).toBe("asks-only");
    expect(verdict!.reason).toBe("no-bids");
    expect(verdict!.levels).toBe(94);
    expect(Number(verdict!.notional)).toBeCloseTo(21_900_000, 0);
    expect(verdict!.topOfBook).toBe("0.001");
  });

  it("leaves a two-sided book alone even when one side is thin", () => {
    expect(oneSidedOf(BIDS, ASKS)).toBeNull();
  });
});

describe("onGrid", () => {
  it("accepts tick-aligned prices and rejects the rest", () => {
    expect(onGrid("0.501", "0.001")).toBe(true);
    expect(onGrid("0.42", "0.01")).toBe(true);
    expect(onGrid("0.425", "0.01")).toBe(false);
  });
});

describe("renderer units", () => {
  it("converts a price string to tick units, which is what `kind=\"price\"` takes", () => {
    // The 0.001 market that started all of this: micro-units would render this row as "1.000".
    expect(priceUnitsOf("0.001", "0.001")).toBe(1);
    expect(priceUnitsOf("0.094", "0.001")).toBe(94);
    expect(priceUnitsOf("0.42", "0.01")).toBe(42);
    expect(priceUnitsOf("-0.06", "0.01")).toBe(-6);
  });

  it("refuses a price that is finer than the tick instead of rounding it on screen", () => {
    expect(() => priceUnitsOf("0.425", "0.01")).toThrow();
    expect(() => priceUnitsOf("1e-3", "0.001")).toThrow();
  });

  it("drops the fraction of a share count, never rounds it up", () => {
    // A displayed size may never be larger than the size that is there.
    expect(shareUnitsOf("232978723.404255")).toBe(232_978_723);
    expect(shareUnitsOf("0.999999")).toBe(0);
    expect(shareUnitsOf("-12.5")).toBe(-12);
    expect(() => shareUnitsOf("1e-3")).toThrow();
  });
});
