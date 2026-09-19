import { describe, expect, it } from "vitest";
import { captureAnchor, restoreScrollTop, type RowBox } from "./anchor";

const rows = (prices: [string, number][]): RowBox[] => prices.map(([price, top]) => ({ price, top }));

describe("ladder scroll anchoring across a re-ladder", () => {
  it("anchors on the row spanning the top edge, not the first row", () => {
    const box = rows([
      ["0.100", 0],
      ["0.099", 44],
      ["0.098", 88],
      ["0.097", 132],
    ]);
    expect(captureAnchor(box, 100)).toEqual({ price: "0.098", offsetPx: 12 });
    // Half a row into it is 12px, and that 12px is the whole point: capturing only the price would snap the row
    // to the top edge on the next render.
    expect(captureAnchor(box, 88)).toEqual({ price: "0.098", offsetPx: 0 });
  });

  it("anchors on the first row at rest, and never returns a negative offset from an over-scroll", () => {
    const box = rows([["0.100", 0], ["0.099", 44]]);
    expect(captureAnchor(box, 0)).toEqual({ price: "0.100", offsetPx: 0 });
    expect(captureAnchor(box, -30)?.price).toBe("0.100");
  });

  it("has nothing to anchor when the ladder is empty", () => {
    expect(captureAnchor([], 0)).toBeNull();
  });

  it("restores the same offset when the price survives a re-ladder at a different height", () => {
    const before = rows([["0.100", 0], ["0.099", 44], ["0.098", 88]]);
    const anchor = captureAnchor(before, 100);
    expect(anchor).toEqual({ price: "0.098", offsetPx: 12 });
    // Same price, different geometry: the reader keeps the same price under the same edge, which is the only
    // invariant that survives the ladder changing shape.
    const after = rows([["0.105", 0], ["0.100", 50], ["0.098", 92]]);
    expect(restoreScrollTop(after, anchor!)).toBe(104);
  });

  it("falls back to the nearest surviving price when the anchored bucket merged away", () => {
    const anchor = { price: "0.098", offsetPx: 12 };
    const after = rows([["0.10", 0], ["0.09", 44]]);
    // 0.098 is 0.002 from 0.10 and 0.008 from 0.09: the reader lands on 0.10, the price their bucket merged
    // into, not on the top of the ladder.
    expect(restoreScrollTop(after, anchor)).toBe(12);
    const downOnly = rows([["0.09", 44], ["0.08", 88]]);
    expect(restoreScrollTop(downOnly, anchor)).toBe(56);
  });

  it("returns null when the side emptied, so the caller leaves the scroll alone", () => {
    expect(restoreScrollTop([], { price: "0.098", offsetPx: 12 })).toBeNull();
  });
});
