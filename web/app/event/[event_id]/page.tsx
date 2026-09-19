import { notFound } from "next/navigation";
import { t } from "@/i18n/t";
import { serverRead } from "@/api/server-read";
import { EventView } from "@/screens/EventView";
import type { BookPayload } from "@/screens/OrderBook";
import type { EventRow } from "@/lib/ladders";

export const dynamic = "force-dynamic";

/**
 * A negRisk event: the outcome table plus a book for the selected row.
 *
 * The event read and the FIRST row's book are both server-rendered, because the table is the page and an empty
 * table on arrival is a page that has to be scrolled before it means anything. The remaining 127 books are
 * fetched on selection — see the note in `EventView`.
 */
export default async function EventPage({ params }: { params: Promise<{ event_id: string }> }) {
  const { event_id } = await params;
  const read = await serverRead<{
    event: { id: string; title: string; negRisk: boolean; category?: string | null; marketCount: number };
    outcomes: EventRow[];
  }>("GET", `/v1/events/${encodeURIComponent(event_id)}`);
  if (read.ok === false) {
    if (read.status === 404) notFound();
    return (
      <main className="pgm-page">
        <p className="unavailable" role="status">
          <strong>{t("common.state.errorTitle")}</strong>
          <span>{t("markets.state.error")}</span>
        </p>
      </main>
    );
  }
  const first = read.data.outcomes[0]?.marketId;
  const bookRead = first
    ? await serverRead<BookPayload>("GET", `/v1/markets/${encodeURIComponent(first)}/book?depth=400`)
    : null;
  return (
    <main className="pgm-page">
      <EventView
        event={read.data.event}
        outcomes={read.data.outcomes}
        initialBook={bookRead && bookRead.ok !== false ? bookRead.data : null}
        stamp={read.data as { asOf?: number; staleAfter?: number }}
      />
    </main>
  );
}
