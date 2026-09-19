/**
 * Where the ladder is looking, across a re-ladder.
 *
 * Changing the aggregate step (tick → 1¢ → 5¢) or the market's tick size rebuilds every row. The rows a reader
 * was watching do not disappear from the market, so the panel must not jump: the price under the top edge of
 * the viewport has to still be under the top edge of the viewport afterwards. Anything else is a scroll position
 * that means "top of the book" one frame and "somewhere in the middle" the next, which is exactly the moment a
 * trader misreads a size.
 *
 * Two pure functions instead of an effect that reads the DOM, because the interesting part is what happens when
 * the anchored price is GONE — the bucket it belonged to merged into a neighbour, or the market moved a tick
 * away from it. That case has an answer (fall back to the nearest surviving price on the same side, and to no
 * scroll at all if the side emptied), and "nearest surviving price" is a claim worth testing rather than a
 * guess buried in a layout effect.
 */
export type RowBox = { price: string; top: number };
export type Anchor = { price: string; offsetPx: number };

/**
 * The row spanning the top edge, plus how far into it the edge sits. `offsetPx` is what makes the restore
 * exact: anchoring on "the price that was at the top" alone would snap the viewport to that row's top even when
 * the reader was half a row into it.
 */
export function captureAnchor(rows: RowBox[], scrollTop: number): Anchor | null {
  if (rows.length === 0) return null;
  let best: RowBox | null = null;
  for (const row of rows) {
    if (row.top <= scrollTop) best = row;
    else break;
  }
  // Above the first row (scrollTop 0 with the first row at 0, or an over-scroll): the first row is the anchor.
  const row = best ?? rows[0];
  if (!row) return null;
  return { price: row.price, offsetPx: scrollTop - row.top };
}

/**
 * The scrollTop that puts the anchor back where it was, or `null` when it cannot: the price is gone and so is
 * every price on its side, which happens when a one-sided book flips sides.
 *
 * `price` is compared as the string the venue sent, never as a parsed number: 0.010 and 0.01 are the same
 * price, but a component that parses to compare has put a float in the layout path, and the ladder's job is to
 * show the venue's own strings.
 */
export function restoreScrollTop(rows: RowBox[], anchor: Anchor): number | null {
  if (rows.length === 0) return null;
  const exact = rows.find((row) => row.price === anchor.price);
  if (exact) return exact.top + anchor.offsetPx;
  // The anchored bucket is gone: the nearest surviving price is the honest target, because it is the price the
  // reader is now looking at the neighbourhood of. Ties break upward (the nearer price on the ask side of the
  // gap), which is where a merged bucket's liquidity went.
  let nearest: RowBox | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  const target = Number(anchor.price);
  for (const row of rows) {
    const distance = Math.abs(Number(row.price) - target);
    if (distance < bestDistance) {
      bestDistance = distance;
      nearest = row;
    }
  }
  return nearest ? nearest.top + anchor.offsetPx : null;
}
