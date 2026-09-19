import { describe, expect, it } from "vitest";
import {
  addCents,
  cents,
  centsFromDecimal,
  formatCents,
  formatNumber,
  groupThousands,
  priceToUnits,
  tickToDecimals,
  unitsToPrice,
  withSiSuffix,
} from "./cents";

describe("cents are integers or nothing", () => {
  it("refuses a float at the boundary", () => {
    expect(() => cents(42.5)).toThrowError(/integer/);
    expect(() => cents(Number.NaN)).toThrowError(/integer/);
    expect(() => cents("10" as unknown as number)).toThrowError(/integer/);
  });
  it("refuses three decimals in a money string, because it means a price was multiplied", () => {
    expect(() => centsFromDecimal("0.425")).toThrowError(/more than 2 decimals/);
    expect(centsFromDecimal("0.42")).toBe(42);
    expect(centsFromDecimal("-12.75")).toBe(-1275);
    expect(centsFromDecimal("1,234.50".replace(",", ""))).toBe(123450);
  });
  it("adds without drift where a float loop would drift", () => {
    const one = centsFromDecimal("0.10");
    expect(addCents(...Array.from({ length: 10 }, () => one))).toBe(100);
  });
});

describe("tick decides the precision, not a habit", () => {
  it("maps both tick sizes that occur today", () => {
    expect(tickToDecimals("0.01")).toBe(2);
    expect(tickToDecimals("0.001")).toBe(3);
    expect(tickToDecimals("1")).toBe(0);
  });
  it("refuses a price finer than the tick rather than rounding it", () => {
    expect(() => priceToUnits("0.425", "0.01")).toThrowError(/finer than the tick/);
    expect(priceToUnits("0.425", "0.001")).toBe(425);
    expect(unitsToPrice(425, "0.001")).toBe("0.425");
    expect(unitsToPrice(999, "0.001")).toBe("0.999");
    expect(unitsToPrice(1, "0.01")).toBe("0.01");
  });
});

describe("rendering", () => {
  it("groups with a thin space and keeps a dot", () => {
    expect(groupThousands("1234567")).toBe("1\u2009234\u2009567");
    expect(formatCents(centsFromDecimal("1234.50"))).toBe("1\u2009234.50");
    expect(formatCents(centsFromDecimal("-0.05"), { signed: true })).toBe("-0.05");
  });
  it("labels direction with a sign and never with a colour", () => {
    expect(formatNumber({ value: cents(150), kind: "pnl" })).toBe("+1.50");
    expect(formatNumber({ value: cents(-150), kind: "pnl" })).toBe("-1.50");
  });
  it("keeps money out of the SI suffixes and puts sizes there", () => {
    expect(withSiSuffix(12_345, 1)).toBe("12.3k");
    expect(withSiSuffix(999, 1)).toBe("999");
    expect(formatNumber({ value: 12_345, kind: "size" })).toBe("12.3k");
    expect(() => formatNumber({ value: 1234.5, kind: "money" })).toThrowError(/integer/);
  });
  it("renders per-mille integers as a percent without multiplying a float", () => {
    expect(formatNumber({ value: 12_345, kind: "percent" })).toBe("12.345%");
    expect(formatNumber({ value: -5_000, kind: "percent" })).toBe("-5%");
  });
});

describe("the formatter has no bit width", () => {
  // The regression this pins is invisible to every "looks right in a table" review: `formatCents` used to run
  // its rounding through `| 0`, which is int32, so any balance above 2^31 cents rendered as a wrapped — and
  // usually negative — number. $30,000,000 came out as "-12 949 673.-96". Small-value tests could not catch
  // it; the file whose entire argument is "integer cents are safe" needed a big value in it.
  it("formats a balance above the int32 ceiling", () => {
    expect(formatCents(cents(3_000_000_000))).toBe("30\u2009000\u2009000.00");
    expect(formatCents(cents(2_147_483_649))).toBe("21\u2009474\u2009836.49");
    expect(addCents(cents(2_000_000_000), cents(2_000_000_000))).toBe(4_000_000_000);
    expect(formatCents(cents(4_000_000_000))).toBe("40\u2009000\u2009000.00");
  });
  it("keeps a negative above the ceiling negative instead of wrapping it positive", () => {
    expect(formatCents(cents(-3_000_000_000))).toBe("-30\u2009000\u2009000.00");
  });
});

describe("a non-integer unit is a refusal, never a truncated guess", () => {
  it("count, size, percent and price all throw rather than render NaN or a quietly rounded value", () => {
    expect(() => formatNumber({ kind: "count", value: Number.NaN })).toThrowError(/integer/);
    expect(() => formatNumber({ kind: "size", value: 12.5 })).toThrowError(/integer/);
    expect(() => formatNumber({ kind: "percent", value: Number.NaN })).toThrowError(/integer/);
    expect(() => formatNumber({ kind: "price", value: 42.5, priceUnits: 42.5, tick: "0.01" })).toThrowError(/integer/);
    expect(() => unitsToPrice(Number.NaN, "0.001")).toThrowError(/integer/);
  });
  it("renders the integers it exists for", () => {
    expect(formatNumber({ kind: "count", value: 12_345 })).toBe("12\u2009345");
    expect(formatNumber({ kind: "percent", value: 12_345 })).toBe("12.345%");
    expect(formatNumber({ kind: "size", value: 999 })).toBe("999");
    expect(unitsToPrice(425, "0.001")).toBe("0.425");
  });
});
