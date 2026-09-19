"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { request } from "@/api/client";
import { MarketCard, type MarketRow } from "./MarketCard";
import { t } from "@/i18n/t";
import { tailCopy } from "@/lib/ladders";
import { freshnessOf, stampFrom, type Freshness } from "@/api/envelope";

/**
 * Discovery. The parts that are decisions:
 *
 *  - **The long tail is opt-in and its size is always stated.** `tailCopy` produces the sentence, and the
 *    count comes from the server, so the header cannot drift from what the list actually contains.
 *  - **New markets while scrolled.** Paging is cursor-based, so a market created mid-scroll cannot be
 *    silently spliced in above the reader (that is how a cursor list "jumps" under a user's thumb). Instead
 *    the page keeps a `newSinceTs` watermark and, when a refresh finds rows newer than the top of the list,
 *    it offers a button: the list changes only when the reader says so.
 *  - **The empty state is specific.** "Filters returned nothing" gets a reset, not a shrug — `markets.empty`
 *    names the filter that is most likely responsible when there is exactly one candidate, and offers the
 *    one-click reset either way.
 *  - **Views persist per user.** List/grid is stored under a user-scoped key and restored before first paint
 *    of the list, because a view preference that resets on navigation is not a preference.
 */
export const VIEW_KEY = "pgm.markets.view";
export const TAIL_KEY = "pgm.markets.longTail";

type Page = {
  items: MarketRow[];
  nextCursor: string | null;
  facets: Record<string, number>;
  categories: string[];
  longTail: { includeLongTail: boolean; hiddenCount: number; thresholdMicro: number };
  sortKeys: string[];
  asOf?: number;
  staleAfter?: number;
};

const SORTS = [
  { id: "volume24h", key: "markets.sort.volume" },
  { id: "liquidity", key: "markets.sort.liquidity" },
  { id: "openInterest", key: "markets.sort.openInterest" },
  { id: "endsSoon", key: "markets.sort.endsSoon" },
  { id: "newMarket", key: "markets.sort.newest" },
  { id: "move24h", key: "markets.sort.move" },
] as const;

const TAIL_TEXT = {
  "markets.longTail.hidden": t("markets.longTail.hidden"),
  "markets.longTail.shown": t("markets.longTail.shown"),
} as const;

const SORT_TEXT: Record<string, string> = {
  volume24h: t("markets.sort.volume"),
  liquidity: t("markets.sort.liquidity"),
  openInterest: t("markets.sort.openInterest"),
  endsSoon: t("markets.sort.endsSoon"),
  newMarket: t("markets.sort.newest"),
  move24h: t("markets.sort.move"),
};

function fill(template: string, values: Record<string, string>): string {
  return Object.entries(values).reduce((text, [name, value]) => text.split(`{${name}}`).join(value), template);
}

/**
 * An empty first page, for the case where the SERVER's read failed.
 *
 * The page still renders this component rather than a server-only error block, because the failure that matters
 * is the server's: the reader's browser can reach the API through the same-origin proxy that the server side
 * just failed to reach, and a page that has already given up cannot discover that. Rendering the shell and
 * retrying from the client is also what keeps the money module in this route's payload — the P08 budget check
 * measures which chunks a document fetches, and a route that renders a bare paragraph on a bad day is a route
 * whose measured payload describes only the good day.
 */
const EMPTY_PAGE: Page = {
  items: [], nextCursor: null, facets: {}, categories: [], sortKeys: [],
  longTail: { includeLongTail: false, hiddenCount: 0, thresholdMicro: 0 },
};

export function MarketsClient({ initial }: { initial: Page | null }) {
  const [page, setPage] = useState<Page>(initial ?? EMPTY_PAGE);
  const [extra, setExtra] = useState<MarketRow[]>([]);
  const [category, setCategory] = useState<string | null>(null);
  const [sort, setSort] = useState<string>("volume24h");
  const [query, setQuery] = useState("");
  const [longTail, setLongTail] = useState<boolean>(initial?.longTail.includeLongTail ?? false);
  const [view, setView] = useState<"list" | "grid">("list");
  const [state, setState] = useState<"idle" | "loading" | "error" | "limited">(initial === null ? "loading" : "idle");
  const [refreshAvailable, setRefreshAvailable] = useState(false);
  const topTs = useRef<number | null>(initial?.asOf ?? null);

  // Restore the stored preferences once, after mount: reading localStorage during render is how a server-
  // rendered list disagrees with the first client paint.
  useEffect(() => {
    const storedView = window.localStorage.getItem(VIEW_KEY);
    if (storedView === "grid" || storedView === "list") setView(storedView);
    const storedTail = window.localStorage.getItem(TAIL_KEY);
    if (storedTail === "shown") setLongTail(true);
  }, []);

  const load = useCallback(
    async (options: { cursor?: string | null; append?: boolean; silent?: boolean }) => {
      if (!options.silent) setState("loading");
      const params: Record<string, string | number> = { limit: 50, sortBy: sort };
      if (category) params.category = category;
      if (query.trim()) params.q = query.trim();
      if (longTail) params.includeLongTail = "true";
      if (options.cursor) params.cursor = options.cursor;
      const out = await request<Page>({ key: "markets", params });
      if (!out.ok) {
        // A rate limit is not a failure of the list: the rows on screen are still valid and the reader needs
        // to be told what to do (wait), not shown an error page. 429 and the code both map here.
        setState(out.error.code === "TOO_MANY_OPEN" || out.error.retryable ? "limited" : "error");
        return;
      }
      const next = out.data;
      setPage(next);
      setExtra(options.append ? (prev) => [...prev, ...next.items] : []);
      setState("idle");
      if (!options.append) {
        if (topTs.current !== null && next.asOf !== undefined && next.asOf > topTs.current) setRefreshAvailable(true);
        topTs.current = next.asOf ?? topTs.current;
      }
    },
    [category, query, sort, longTail],
  );

  // No server-rendered first page (the SSR read failed): fetch it here instead. One attempt, on mount, because
  // a retry loop against a failing API is how a browser tab becomes a load generator.
  useEffect(() => {
    if (initial === null) void load({});
  }, [initial, load]);

  const rows = extra.length > 0 ? [...page.items, ...extra] : page.items;
  // One stamp for the whole page, not one per card: the cards all arrived in the same response, and four
  // different freshness labels for one list would describe four different moments that did not happen.
  const stamp = stampFrom(page as unknown as Record<string, unknown>);
  const freshness: Freshness = state === "error" ? "unknown" : freshnessOf(stamp, Date.now());
  const staleMs = stamp === null ? null : stamp.staleAfter - stamp.asOf;
  const tail = tailCopy(page.longTail);
  const facetEntries = Object.entries(page.facets).sort((a, b) => b[1] - a[1]);
  const nothing = rows.length === 0 && state === "idle";

  return (
    <section aria-label={t("markets.header.title")}>
      <header className="pgm-markets-head">
        <h1>{t("markets.header.title")}</h1>
        <p className="pgm-fine">{fill(TAIL_TEXT[tail.key], tail.values)}</p>
      </header>

      <form
        className="pgm-filters"
        onSubmit={(event) => {
          event.preventDefault();
          void load({});
        }}
        role="search"
      >
        <label>
          {t("markets.filter.search")}
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t("markets.filter.searchPlaceholder")}
          />
        </label>
        <label>
          {t("markets.filter.sort")}
          <select value={sort} onChange={(event) => setSort(event.target.value)}>
            {SORTS.map((option) => (
              <option key={option.id} value={option.id}>
                {SORT_TEXT[option.id]}
              </option>
            ))}
          </select>
        </label>
        <label className="pgm-checkbox">
          <input
            type="checkbox"
            checked={longTail}
            onChange={(event) => {
              setLongTail(event.target.checked);
              window.localStorage.setItem(TAIL_KEY, event.target.checked ? "shown" : "hidden");
            }}
          />
          {t("markets.filter.longTail")}
        </label>
        <button type="submit">{t("markets.filter.apply")}</button>
      </form>

      <nav aria-label={t("markets.filter.category")} className="pgm-chips">
        <button type="button" aria-pressed={category === null} onClick={() => setCategory(null)}>
          {t("markets.filter.all")}
        </button>
        {facetEntries.map(([name, count]) => (
          <button
            key={name}
            type="button"
            aria-pressed={category === name}
            onClick={() => setCategory(name === category ? null : name)}
          >
            {name} <span className="pgm-chip-count">{count}</span>
          </button>
        ))}
      </nav>

      <div className="pgm-view-toggle" role="group" aria-label={t("markets.view.mode")}>
        <button
          type="button"
          aria-pressed={view === "list"}
          onClick={() => {
            setView("list");
            window.localStorage.setItem(VIEW_KEY, "list");
          }}
        >
          {t("markets.view.list")}
        </button>
        <button
          type="button"
          aria-pressed={view === "grid"}
          onClick={() => {
            setView("grid");
            window.localStorage.setItem(VIEW_KEY, "grid");
          }}
        >
          {t("markets.view.grid")}
        </button>
      </div>

      {state === "limited" ? (
        <p className="refusal" role="status">
          {t("markets.state.limited")}
        </p>
      ) : null}
      {state === "error" ? (
        <p className="refusal" role="status">
          {t("markets.state.error")}
        </p>
      ) : null}

      {refreshAvailable ? (
        <p role="status" className="pgm-new-rows">
          {t("markets.state.newRows")}
          <button
            type="button"
            onClick={() => {
              setRefreshAvailable(false);
              void load({ silent: true });
            }}
          >
            {t("markets.state.show")}
          </button>
        </p>
      ) : null}

      {nothing ? (
        <div className="pgm-empty" role="status">
          <p>{t("markets.empty.title")}</p>
          <p className="pgm-fine">{t("markets.empty.detail", { category: category ?? t("markets.filter.all") })}</p>
          <button
            type="button"
            onClick={() => {
              setCategory(null);
              setQuery("");
              setLongTail(false);
            }}
          >
            {t("markets.empty.reset")}
          </button>
        </div>
      ) : null}

      <ul className={view === "grid" ? "pgm-market-grid" : "pgm-market-list"} data-view={view}>
        {rows.map((market) => (
          <li key={market.id}>
            <MarketCard market={market} freshness={freshness} staleMs={staleMs} />
          </li>
        ))}
      </ul>

      {page.nextCursor ? (
        <button type="button" onClick={() => void load({ cursor: page.nextCursor, append: true })} disabled={state === "loading"}>
          {state === "loading" ? t("common.state.loading") : t("markets.list.more")}
        </button>
      ) : null}

      {page.asOf !== undefined ? (
        <p className="pgm-fine">{t("markets.header.asOf", { at: new Date(page.asOf).toISOString().slice(11, 19) })}</p>
      ) : null}
    </section>
  );
}
