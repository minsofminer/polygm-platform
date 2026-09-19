"use client";
import { useCallback, useEffect, useState } from "react";
import { request } from "@/api/client";
import { t } from "@/i18n/t";
import { EventTable } from "./EventTable";
import { OrderBook, type BookPayload } from "./OrderBook";
import { type EventRow } from "@/lib/ladders";
import { freshnessOf, stampFrom, type Freshness } from "@/api/envelope";

/**
 * The negRisk layout: a sortable outcome table, and a book for the selected row.
 *
 * The book follows the SELECTION rather than the page: a 128-outcome event has 128 books, and loading them
 * all to show one is how an event page becomes slower than the market page it summarises. Selection is local
 * state with the first row as the default, so the panel is never empty on arrival.
 *
 * `probabilitySum`, `deviation` and `tolerance` come from the server for the header; the table recomputes them
 * from the rows it is displaying (`eventInvariant`) because a user who sorts by volume is looking at a
 * different subset, and a header that describes a set the reader cannot see is a header that lies.
 */
export function EventView({
  event,
  outcomes,
  initialBook,
  stamp,
}: {
  event: { id: string; title: string; negRisk: boolean; category?: string | null; marketCount: number };
  outcomes: EventRow[];
  initialBook: BookPayload | null;
  stamp: { asOf?: number; staleAfter?: number } | null;
}) {
  const freshness: Freshness = freshnessOf(stampFrom((stamp ?? {}) as Record<string, unknown>), Date.now());
  const staleMs = stamp && stamp.asOf !== undefined && stamp.staleAfter !== undefined
    ? stamp.staleAfter - stamp.asOf
    : null;
  const [selected, setSelected] = useState<string | null>(outcomes[0]?.marketId ?? null);
  const [book, setBook] = useState<BookPayload | null>(initialBook);
  const [loading, setLoading] = useState(false);

  const selectedRow = outcomes.find((row) => row.marketId === selected) ?? null;

  const loadBook = useCallback(async (marketId: string) => {
    setLoading(true);
    const out = await request<BookPayload & Record<string, unknown>>({
      key: "book",
      params: { market_id: marketId, depth: 400 },
    });
    setBook(out.ok ? out.data : null);
    setLoading(false);
  }, []);

  useEffect(() => {
    if (selected && selected !== outcomes[0]?.marketId) void loadBook(selected);
  }, [selected, loadBook, outcomes]);

  return (
    <div className="pgm-event-view">
      <header className="pgm-market-head">
        <h1>{event.title}</h1>
        <p className="pgm-fine">
          {event.negRisk ? <span className="pgm-chip">{t("markets.card.negRisk")}</span> : null}
          {event.category ? <span className="pgm-chip">{event.category}</span> : null}
          <span>{t("event.summary.count", { count: String(event.marketCount) })}</span>
        </p>
      </header>

      <div className="pgm-event-main">
        <EventTable
          rows={outcomes}
          tick={selectedRow?.minimumTickSize ?? "0.01"}
          onSelect={setSelected}
          freshness={freshness}
          staleMs={staleMs}
        />
        <aside aria-label={t("event.book.title")}>
          <h2>{selectedRow ? selectedRow.question : t("event.book.select")}</h2>
          {loading ? <p className="pgm-fine">{t("common.state.loading")}</p> : null}
          <OrderBook book={book} tick={selectedRow?.minimumTickSize ?? "0.01"} />
        </aside>
      </div>
    </div>
  );
}
