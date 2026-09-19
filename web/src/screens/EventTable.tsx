"use client";
import { useMemo, useState } from "react";
import { Number as NumberView } from "@/num/Number";
import { t } from "@/i18n/t";
import { microOf, microToDecimal, priceUnitsOf } from "@/lib/depth";
import { eventInvariant, invariantCopy, type EventRow } from "@/lib/ladders";
import type { Freshness } from "@/api/envelope";

/**
 * The negRisk outcome table — 128 rows, sortable, with the invariant stated above it.
 *
 * **Why 128 rows are not virtualised here.** P08 declined windowing for the tape because `TAPE_ROW_CAP = 64`
 * keeps the DOM inside budget. At 128 rows that argument is thinner, so the decision is taken from the shape
 * rather than inherited: 128 rows x five cells is ~640 nodes, the table has no per-row subscription (one
 * payload, one render), and a windowing layer would add a scroll listener competing with the shell's own.
 * `MAX_ROWS` still caps what is rendered, and the deferred proof (`[UNVERIFIED]` in
 * docs/P09-frontend-markets.md §4) is a frame timing, not an opinion.
 *
 * The invariant line is the part that is not a table: prices that must sum to about a dollar, compared against
 * what the tick sizes can explain, plus the executable form of the claim (buying one of every outcome at the
 * ask) because that is the only version of an "opportunity" a user can act on.
 */
export const MAX_ROWS = 400;

type SortKey = "volume24h" | "price" | "change24h" | "liquidity" | "question";

const HEADER_TEXT = {
  question: t("event.table.outcome"),
  price: t("event.table.price"),
  volume24h: t("event.table.volume"),
  liquidity: t("event.table.liquidity"),
  change24h: t("event.table.change"),
} as const;

const INVARIANT_TEXT = {
  "event.invariant.within": t("event.invariant.within"),
  "event.invariant.deviates": t("event.invariant.deviates"),
  "event.invariant.opportunity": t("event.invariant.opportunity"),
  "event.invariant.partial": t("event.invariant.partial"),
} as const;

/** The copy's placeholders live in the pure function (it is the part with the logic); the words live in the
 *  dictionary, and both lookups above are literal `t()` calls because the i18n checker refuses a computed
 *  key: a key assembled at runtime is a key nobody can find. */
function fill(template: string, values: Record<string, string>): string {
  return Object.entries(values).reduce((text, [name, value]) => text.split(`{${name}}`).join(value), template);
}

export function EventTable({
  rows,
  tick,
  onSelect,
  freshness,
  staleMs,
}: {
  rows: EventRow[];
  tick: string;
  onSelect?: (marketId: string) => void;
  /** The event payload's stamp. 400 outcome prices share one moment; see the note in MarketsClient. */
  freshness: Freshness;
  staleMs: number | null;
}) {
  const [sort, setSort] = useState<SortKey>("volume24h");
  const [descending, setDescending] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);

  const invariant = useMemo(() => eventInvariant(rows), [rows]);
  const copy = invariantCopy(invariant);
  const invKind = copy.key.slice("event.invariant.".length) as keyof typeof INVARIANT_TEXT;

  const ordered = useMemo(() => {
    const factor = descending ? -1 : 1;
    const value = (row: EventRow): number | string => {
      switch (sort) {
        case "question":
          return row.question.toLowerCase();
        case "price":
          return row.price === null || row.price === undefined ? -1 : microOf(row.price);
        case "change24h":
          return row.change24h === null || row.change24h === undefined ? Number.MIN_SAFE_INTEGER : microOf(row.change24h);
        case "volume24h":
          return microOf(row.volume24h ?? "0");
        default:
          return microOf(row.liquidity ?? "0");
      }
    };
    return [...rows].slice(0, MAX_ROWS).sort((a, b) => {
      const left = value(a);
      const right = value(b);
      if (typeof left === "string" || typeof right === "string") {
        return factor * String(left).localeCompare(String(right));
      }
      return factor * (left - right);
    });
  }, [rows, sort, descending]);

  const header = (key: SortKey) => (
    <th scope="col" aria-sort={sort === key ? (descending ? "descending" : "ascending") : "none"}>
      <button
        type="button"
        className="pgm-sort"
        onClick={() => {
          if (sort === key) setDescending(!descending);
          else {
            setSort(key);
            setDescending(true);
          }
        }}
      >
        {HEADER_TEXT[key]}
      </button>
    </th>
  );

  return (
    <section aria-label={t("event.table.title")}>
      <p className="pgm-invariant" role="status" data-testid="invariant">
        <strong>{fill(INVARIANT_TEXT[invKind], copy.values)}</strong>
      </p>
      <table className="pgm-event-table">
        <caption className="pgm-fine">{t("event.table.caption", { count: String(ordered.length) })}</caption>
        <thead>
          <tr>
            {header("question")}
            {header("price")}
            {header("volume24h")}
            {header("liquidity")}
            {header("change24h")}
          </tr>
        </thead>
        <tbody>
          {ordered.map((row) => {
            const rowTick = row.minimumTickSize || tick;
            // Tick units for the price columns, as `priceUnitsOf` documents; the volume column is cents.
            const priceUnits = row.price === null || row.price === undefined ? null : priceUnitsOf(row.price, rowTick);
            const changeUnits = row.change24h === null || row.change24h === undefined
              ? null
              : priceUnitsOf(row.change24h, rowTick);
            const volumeCents = Math.round(microOf(row.volume24h ?? "0") / 10 ** 4);
            const liquidityCents = Math.round(microOf(row.liquidity ?? "0") / 10 ** 4);
            return (
              <tr
                key={row.marketId}
                aria-selected={selected === row.marketId}
                onClick={() => {
                  setSelected(row.marketId);
                  onSelect?.(row.marketId);
                }}
              >
                <th scope="row">{row.question}</th>
                <td>
                  {priceUnits === null ? (
                    <span className="pgm-fine">{t("event.table.noPrice")}</span>
                  ) : (
                    <NumberView kind="price" value={priceUnits} tick={rowTick}
                      freshness={freshness} staleMs={staleMs} />
                  )}
                </td>
                <td>
                  <NumberView kind="money" value={volumeCents} />
                </td>
                <td>
                  <NumberView kind="money" value={liquidityCents} />
                </td>
                <td>
                  {changeUnits === null ? (
                    <span className="pgm-fine">{t("event.table.noChange")}</span>
                  ) : (
                    <NumberView kind="price" value={changeUnits} tick={rowTick} signed
                      freshness={freshness} staleMs={staleMs} />
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {rows.length > MAX_ROWS ? (
        <p className="pgm-fine">{t("event.table.capped", { total: String(rows.length) })}</p>
      ) : null}
      {invariant.excluded.length > 0 ? (
        <p className="pgm-fine">{t("event.table.excluded", { count: String(invariant.excluded.length) })}</p>
      ) : null}
      <p className="pgm-fine">
        {t("event.table.deviation", {
          deviation: microToDecimal(Math.abs(invariant.deviationMicro), 4),
          tolerance: microToDecimal(invariant.toleranceMicro, 4),
        })}
      </p>
    </section>
  );
}
