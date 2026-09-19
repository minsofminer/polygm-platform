"use client";
import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { Number as NumberView } from "@/num/Number";
import { WidgetBoundary } from "@/ui/WidgetBoundary";
import { t } from "@/i18n/t";
import {
  aggregateLevels, maxCumulative, microOf, oneSidedOf, priceUnitsOf, shareUnitsOf, spreadOf, withDepth, type RawLevel,
} from "@/lib/depth";
import { useConnection } from "@/shell/connection";
import type { SuccessBody } from "@/api/client";
import { captureAnchor, restoreScrollTop, type Anchor, type RowBox } from "@/lib/anchor";
import type { Freshness } from "@/api/envelope";

/**
 * The ladder. Three things here are decisions rather than layout, and each one is a thing a naive ladder gets
 * wrong in a way that loses money or trust:
 *
 *  1. **Aggregate-by defaults to the raw tick, and the control exists because of 0.001 markets.** A thousand
 *     levels per dollar is a wall of three-decimal prices; 1¢ and 5¢ fold it, and the folding is directional
 *     (see `src/lib/depth.ts`) so an aggregated level never shows a price that was not available.
 *  2. **A one-sided book says so, in numbers.** P01 measured 94 ask levels at 0.001 totalling $21.9M against
 *     zero bids. The treatment is a banner with the level count, the notional and an explicit statement that
 *     there is no bid at any price — because the alternative ("empty area on the left") reads as a broken
 *     feed, and a user who believes the feed is broken will still try to buy at 0.001.
 *  3. **Clicking a level is the ONLY way this component hands a price to the ticket.** `onPick` carries the
 *     price string straight off the ladder, so an off-grid or invented price cannot be submitted from here.
 *
 * Every number rendered goes through `Number` with the market's tick, and there is no `Number(` coercion
 * inside a `value={...}` — the micro-units are computed here, at the parse site, where the contract is
 * checked (the P08 gate's c4 fails the build on the other spelling).
 *
 * Three more obligations are wired here rather than left to a later phase:
 *
 *  4. **Flash on change, and never on a re-ladder.** The size cell gets `previous` from the previous paint of
 *     the SAME bucket, keyed by the aggregate step, so folding the ladder to 1¢ cannot flash forty cells for a
 *     market that did not move — and the flash decision itself belongs to `src/num/flash.ts`, which refuses
 *     REST-sourced rows. This component therefore passes `source` through untouched: today the book arrives by
 *     REST poll and nothing flashes, by policy, which is the correct behaviour rather than a missing feature.
 *  5. **A stale book says so over the numbers, not instead of them.** An empty ladder reads as "no liquidity",
 *     which is a trading decision; a dimmed ladder with a stale overlay reads as "we cannot see", which is a
 *     different decision. `freshness` is a prop so the book cannot disagree with the header about the age of
 *     the same response.
 *  6. **Re-laddering keeps the reader's place** (`src/lib/anchor.ts`): the price under the top edge stays under
 *     the top edge, including when its bucket merged away.
 */
const AGGREGATES = [
  { id: "raw", label: t("markets.book.aggTick"), stepMicro: null },
  { id: "1c", label: t("markets.book.agg1c"), stepMicro: 10_000 },
  { id: "5c", label: t("markets.book.agg5c"), stepMicro: 50_000 },
] as const;

/**
 * The book body, from the contract rather than from this file: `npm run gen:api` produces
 * `src/api/schema.gen.ts` from `contracts/openapi.yaml`, and `tools/p08-gate-check.py` c2 fails the build on a
 * hand-typed `*Payload`. That is not ceremony — writing this shape by hand is how P09 shipped a ladder reading
 * `minimumTickSize` while the API sends `tickSize`, and how the response's own `cumShares` went undeclared until
 * the client needed it.
 */
export type BookPayload = SuccessBody<"/v1/markets/{market_id}/book", "get">;

const SIDE_TEXT = { bid: t("markets.book.bids"), ask: t("markets.book.asks") };
const EMPTY_TEXT = { bid: t("markets.book.noBids"), ask: t("markets.book.noAsks") };
const ONE_SIDED_TEXT = { "no-bids": t("markets.oneSided.noBids"), "no-asks": t("markets.oneSided.noAsks") };

export function OrderBook({
  book,
  tick,
  onPick,
  stacked = false,
  freshness = "unknown",
  staleMs = null,
  source = "rest",
}: {
  book: BookPayload | null;
  tick: string;
  onPick?: (price: string, side: "bid" | "ask") => void;
  stacked?: boolean;
  freshness?: Freshness;
  staleMs?: number | null;
  /** Where the levels came from. "rest" is today's poll; the live tape will pass "ws" and the flash wakes up. */
  source?: "ws" | "rest";
}) {
  const [aggregate, setAggregate] = useState<(typeof AGGREGATES)[number]["id"]>("raw");
  const step = AGGREGATES.find((option) => option.id === aggregate)?.stepMicro ?? null;

  const view = useMemo(() => {
    if (!book) return null;
    const bids = aggregateLevels(book.bids, step, "bid");
    const asks = aggregateLevels(book.asks, step, "ask");
    const scale = maxCumulative(bids, asks);
    return {
      bids: withDepth(bids, scale),
      asks: withDepth(asks, scale),
      spread: spreadOf(book.bids, book.asks),
      oneSided: oneSidedOf(book.bids, book.asks),
    };
  }, [book, step]);

  const canTrade = useConnection((state) => state.canTrade());

  // Previous sizes per (aggregate, side, price). Keyed by the step so a re-ladder has no "previous" to compare
  // against and therefore flashes nothing: the market did not move, the arithmetic did.
  const previousSizes = useRef<Map<string, number>>(new Map());
  const ladderBox = useRef<HTMLDivElement | null>(null);
  const anchor = useRef<Anchor | null>(null);

  /**
   * One anchor, captured from the BID column, is enough for both: the two columns are siblings in one grid with
   * identical row geometry, so the same price sits at the same offset from the panel's top edge on either side.
   * Capturing per side would produce two numbers that must agree and would have to be reconciled on restore,
   * which is how a "no visual jump" fix turns into a jump between two nearly-equal scroll positions.
   */
  const rowsNow = (): RowBox[] => {
    const box = ladderBox.current;
    const list = box?.querySelector<HTMLElement>('ol[data-side="bid"]') ?? box?.querySelector<HTMLElement>("ol[data-side]");
    if (!list) return [];
    return [...list.querySelectorAll<HTMLElement>("[data-ladder-price]")].map((row) => ({
      price: row.dataset.ladderPrice ?? "",
      top: row.offsetTop,
    }));
  };
  const rememberPlace = () => {
    const box = ladderBox.current;
    if (box) anchor.current = captureAnchor(rowsNow(), box.scrollTop) ?? anchor.current;
  };

  useLayoutEffect(() => {
    // After the new ladder paints, put the anchored price back under the top edge. Runs for the tick AND the
    // aggregate step, because both are re-ladders and neither is a market event.
    const box = ladderBox.current;
    const saved = anchor.current;
    if (!box || !saved) return;
    const next = restoreScrollTop(rowsNow(), saved);
    if (next !== null) box.scrollTop = next;
    anchor.current = null;
  }, [tick, aggregate, view]);

  useLayoutEffect(() => {
    const next = new Map<string, number>();
    for (const [which, levels] of [["bid", view?.bids ?? []], ["ask", view?.asks ?? []]] as const) {
      for (const level of levels) next.set(`${aggregate}:${which}:${level.price}`, shareUnitsOf(level.shares));
    }
    previousSizes.current = next;
  }, [view, aggregate]);

  if (!view) {
    return (
      <WidgetBoundary label={t("markets.book.title")}>
        <p className="refusal" role="status">
          {t("markets.book.noBook")}
        </p>
      </WidgetBoundary>
    );
  }

  const renderSide = (levels: typeof view.bids, which: "bid" | "ask") => (
    <ol className="pgm-ladder" data-side={which} aria-label={SIDE_TEXT[which]}>
      {levels.length === 0 ? <li className="pgm-ladder-empty">{EMPTY_TEXT[which]}</li> : null}
      {levels.map((level) => {
        // Tick units for the price cell and whole shares for the size cells: the renderer's contract, not the
        // ladder's internal micro-units. See `priceUnitsOf`.
        const priceUnits = priceUnitsOf(level.price, tick);
        const sharesUnits = shareUnitsOf(level.shares);
        const cumUnits = shareUnitsOf(level.cumShares);
        const width = `${level.fraction * 100}%`;
        const previous = previousSizes.current.get(`${aggregate}:${which}:${level.price}`) ?? null;
        return (
          <li key={`${which}:${level.price}`}>
            <button
              type="button"
              className="pgm-ladder-row"
              data-ladder-price={level.price}
              disabled={!canTrade}
              onClick={() => onPick?.(level.price, which)}
            >
              {/* the bar is a background, never a width on the number: the price column must not move */}
              <span className="pgm-ladder-bar" style={{ inlineSize: width }} aria-hidden="true" />
              <span className="pgm-ladder-price">
                <NumberView kind="price" value={priceUnits} tick={tick} compact freshness={freshness} staleMs={staleMs} />
              </span>
              <span className="pgm-ladder-size">
                <NumberView kind="size" value={sharesUnits} previous={previous} source={source} />
              </span>
              <span className="pgm-ladder-cum">
                <NumberView kind="size" value={cumUnits} />
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );

  const midUnits = view.spread.mid === null ? null : priceUnitsOf(view.spread.mid, tick);
  // `spreadCents` is already cents (integer), which is what `kind="money"` takes — the first version multiplied
  // by 100 on the way in and printed a 0.2¢ spread as "$20.00".
  const spreadCents = view.spread.spreadCents === null ? null : Math.round(view.spread.spreadCents);
  return (
    <WidgetBoundary label={t("markets.book.title")}>
      <section aria-label={t("markets.book.title")} data-stacked={stacked ? "true" : "false"}>
        <header className="pgm-ladder-head">
          <h2>{t("markets.book.title")}</h2>
          <div role="group" aria-label={t("markets.book.aggregate")} className="pgm-segmented">
            {AGGREGATES.map((option) => (
              <button
                key={option.id}
                type="button"
                aria-pressed={option.id === aggregate}
                onClick={() => {
                  rememberPlace();
                  setAggregate(option.id);
                }}
              >
                {option.label}
              </button>
            ))}
          </div>
        </header>

        {midUnits === null ? (
          <p className="refusal" role="status">
            {t("markets.book.noTwoSided")}
          </p>
        ) : (
          <p className="pgm-spread" role="status">
            <span>{t("markets.book.mid")}</span>
            <NumberView kind="price" value={midUnits} tick={tick} freshness={freshness} staleMs={staleMs} />
            <span>{t("markets.book.spreadLabel")}</span>
            <NumberView kind="money" value={spreadCents ?? 0} />
            <span>{t("markets.book.spreadBp", { bp: String(view.spread.spreadBp ?? 0) })}</span>
          </p>
        )}

        {view.oneSided ? (
          <div className="pgm-one-sided" role="status" data-testid="one-sided">
            <strong>{ONE_SIDED_TEXT[view.oneSided.reason]}</strong>
            <p>{t("markets.oneSided.detail", { levels: String(view.oneSided.levels), notional: view.oneSided.notional })}</p>
            <p>{t("markets.oneSided.noBid")}</p>
            <p className="refusal">{t("markets.oneSided.doNotChase", { price: view.oneSided.topOfBook })}</p>
          </div>
        ) : null}

        <div className="pgm-ladder-scroll" ref={ladderBox}>
          {freshness !== "live" ? (
            <p className="pgm-book-stale" role="status" data-testid="book-stale">
              {t("markets.book.staleOverlay", { state: freshness })}
            </p>
          ) : null}
          <div className="pgm-ladder-pair" data-stale={freshness !== "live" ? "true" : "false"}>
            {renderSide(view.bids, "bid")}
            {renderSide(view.asks, "ask")}
          </div>
        </div>
        <p className="pgm-fine">{t("markets.book.footnote", { tick })}</p>
      </section>
    </WidgetBoundary>
  );
}
