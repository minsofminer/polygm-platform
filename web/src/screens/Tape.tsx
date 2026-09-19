"use client";
import { useLive } from "@/live/useLive";
import { Number as NumberView } from "@/num/Number";
import { WidgetBoundary } from "@/ui/WidgetBoundary";
import { t } from "@/i18n/t";

/**
 * The tape, in the shell, before the tape *screen* exists (P09). What is being proven here is the
 * infrastructure claim: rows render through `Number`, the REST path is tagged so it cannot flash, and the
 * whole list is `aria-live="off"` with the summary in a separate status region (web/DESIGN.md §7 — a polite
 * live region at 20 updates a second makes the product unusable for a screen-reader user).
 */
/** web/DESIGN.md §4: the tape has a DOM budget, `--pgm-tape-rows` per breakpoint with a hard cap of 64 rows.
 *  A caller asking for 500 is a bug, not a feature, and the cap belongs here rather than in each screen. */
export const TAPE_ROW_CAP = 64;

export function Tape({ rows = 12 }: { rows?: number }) {
  const feed = useLive<{ items?: Record<string, unknown>[] }>("tape");
  const items = (feed.data?.items ?? []).slice(0, Math.max(1, Math.min(rows, TAPE_ROW_CAP)));
  return (
    <WidgetBoundary label={t("shell.nav.tape")}>
      <section aria-label={t("shell.nav.tape")}>
        <p role="status" className="refusal">
          {feed.mode === "rest" ? t("feed.mode.rest") : t("feed.mode.ws")} · {feed.says}
        </p>
        <div aria-live="off">
          <table style={{ inlineSize: "100%", borderCollapse: "collapse" }}>
            <tbody>
              {items.length === 0 ? (
                <tr>
                  <td>{feed.lastGapCount > 0 ? t("shell.connection.gap", { count: feed.lastGapCount }) : t("common.state.loading")}</td>
                </tr>
              ) : null}
              {items.map((row, i) => (
                <tr key={String(row.id ?? i)}>
                  <td>{String(row.outcome ?? row.side ?? "—")}</td>
                  <td>
                    <NumberView
                      kind="price"
                      value={typeof row.price_units === "number" ? row.price_units : 0}
                      tick={typeof row.tick_size === "string" ? row.tick_size : "0.001"}
                      source={feed.source}
                      freshness={feed.freshness}
                      staleMs={feed.stamp ? Math.max(0, Date.now() - feed.stamp.asOf) : null}
                      previous={i === 0 ? (row.prev_units as number | undefined ?? null) : null}
                    />
                  </td>
                  <td>
                    <NumberView kind="size" value={typeof row.size_units === "number" ? row.size_units : 0} source={feed.source} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </WidgetBoundary>
  );
}
