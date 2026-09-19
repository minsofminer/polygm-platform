"use client";
/**
 * D2 · the live tape.
 *
 * The row is fixed by the prompt: time · wallet (avatar + name + classification badges) · side · outcome ·
 * price · size · USD notional · market link. Everything else here is the part that makes it honest:
 *
 *  - **The whale threshold is relative with an absolute fallback (P5 D5).** The measured distribution was
 *    median $5, p95 $133, max $3,000 — a fixed $1,000 is both too low for a big market and too high for a small
 *    one. The badge's tooltip states the exact rule (`thresholdSentence`), verbatim from the server's sentence,
 *    because a badge whose rule is invisible is an accusation rather than a datum.
 *  - **Filters apply to the whole window, not to the page.** Min notional is absolute *and* relative to the
 *    market's median, and the two are independent.
 *  - **20+ fills/s coalesces, and pausing is an affordance, not a freeze.** The queue's depth is the "paused —
 *    N new" count, and resuming releases it in one frame.
 *  - **Sound is off unless the user turned it on**, and then only for followed wallets.
 *  - **A row is navigation.** Click opens the market, click on the wallet opens the dossier, shift-click adds
 *    the market to the watchlist — a tape you cannot leave is a screenshot.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Number as NumberView } from "@/num/Number";
import { StaleIndicator } from "@/num/StaleIndicator";
import { freshnessOf, type Freshness } from "@/api/envelope";
import { t } from "@/i18n/terminal";
import { useTape } from "./useTerminal";
import {
  EMPTY_FILTERS,
  MAX_ROWS,
  ROW_H,
  arrivalRate,
  formatMicro,
  formatShares,
  formatMultiple,
  sharesWhole,
  labelSentence,
  priceUnitsFor,
  rowTarget,
  sharesMicro,
  shouldChime,
  thresholdSentence,
  virtualWindow,
  walletHref,
  type TapeFilters,
} from "./tape";
import type { TapeFacets, TerminalFill } from "./wire";

/**
 * The tape's copy, in one literal table.
 *
 * These used to be `C.label` calls. The build's i18n check treats a computed key as a failure by
 * design — it cannot verify what it cannot read — and the failure mode it prevents is exactly what had happened:
 * the keys did not exist in the dictionary at all, so every string in this panel rendered as its own key. A
 * literal table is checkable, greppable and has one place to edit.
 */
const C = {
  label: t("terminal.tape.row.label"),
  title: t("terminal.tape.row.title"),
  rateHint: t("terminal.tape.row.rateHint"),
  pause: t("terminal.tape.row.pause"),
  soundHint: t("terminal.tape.row.soundHint"),
  soundOn: t("terminal.tape.row.soundOn"),
  soundOff: t("terminal.tape.row.soundOff"),
  filters: t("terminal.tape.row.filters"),
  minNotional: t("terminal.tape.row.minNotional"),
  relativeHint: t("terminal.tape.row.relativeHint"),
  side: t("terminal.tape.row.side"),
  category: t("terminal.tape.row.category"),
  classification: t("terminal.tape.row.classification"),
  wallet: t("terminal.tape.row.wallet"),
  reset: t("terminal.tape.row.reset"),
  any: t("terminal.tape.row.any"),
  buy: t("terminal.tape.row.buy"),
  sell: t("terminal.tape.row.sell"),
  shares: t("terminal.tape.row.shares"),
  watched: t("terminal.tape.row.watched"),
  windowNote: t("terminal.tape.row.windowNote"),
  disconnected: t("terminal.tape.row.disconnected"),
  error: t("terminal.tape.row.error"),
};

export function TapePanel({
  userId,
  followed = [],
  watchlist = [],
  onWatch,
  onSelectMarket,
  initialMarketId = "",
}: {
  userId: string;
  followed?: string[];
  watchlist?: string[];
  onWatch?: (marketId: string) => void;
  onSelectMarket?: (marketId: string) => void;
  initialMarketId?: string;
}) {
  const [filters, setFilters] = useState<TapeFilters>({ ...EMPTY_FILTERS, marketId: initialMarketId });
  const { rows, facets, queuedCount, paused, setPaused, status, stamp, err } = useTape(filters);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportH, setViewportH] = useState(520);
  const [sound, setSound] = useState(false);
  const [openTooltip, setOpenTooltip] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const chime = useRef<{ count: number }>({ count: 0 });

  // The sound preference is read once, and the default is off: `soundEnabled(null) === false`.
  useEffect(() => {
    setSound(window.localStorage.getItem(`pgm.terminal.sound.${userId}`) === "1");
  }, [userId]);

  const toggleSound = useCallback(() => {
    setSound((on) => {
      try {
        window.localStorage.setItem(`pgm.terminal.sound.${userId}`, on ? "0" : "1");
      } catch {
        /* storage is a convenience here, not the state */
      }
      return !on;
    });
  }, [userId]);

  // Chime for followed wallets only, and never more than one per batch: a tape that beeps per fill is a
  // tape users turn off, which is the same as not having the feature.
  useEffect(() => {
    if (!sound || rows.length === 0) return;
    const newest = rows[0] as TerminalFill | undefined;
    if (!newest) return;
    if (shouldChime(newest, followed, true) || followed.includes(newest.anonWallet)) {
      if (chime.current.count !== newest.tsMs) {
        chime.current.count = newest.tsMs;
        const el = document.getElementById("pgm-tape-chime") as HTMLAudioElement | null;
        void el?.play().catch(() => undefined);
      }
    }
  }, [rows, sound, followed]);

  const fresh: Freshness = stamp ? freshnessOf(stamp, Date.now()) : "unknown";
  // The age rides with the freshness: a row's price cannot say "stale" without saying how old, and P08's c10
  // fails a `kind="price"` that has a freshness and no age. Same quantity the header shows, same clock.
  const ageMs = stamp ? Math.max(0, Date.now() - stamp.asOf) : null;
  const win = useMemo(() => virtualWindow(rows.length, scrollTop, viewportH), [rows.length, scrollTop, viewportH]);
  const median = filters.marketId
    ? (facets?.markets.find((m) => m.marketId === filters.marketId)?.thresholdMicro ?? 0)
    : 0;

  return (
    <section className="pgm-tape" aria-label={C.label}>
      <header className="pgm-tape__head">
        <h2>{C.title}</h2>
        <StaleIndicator freshness={fresh} ageMs={ageMs} />
        <span className="pgm-tape__rate" title={C.rateHint}>
          {t("terminal.tape.row.rate", { n: arrivalRate(rows, Date.now()) })}
        </span>
        <button type="button" className="pgm-tape__pause" onClick={() => setPaused(!paused)} aria-pressed={paused}>
          {/* "paused — N new" is the affordance: a frozen feed with no counter reads as a broken feed. */}
          {paused ? t("terminal.tape.row.resume", { n: queuedCount }) : C.pause}
        </button>
        <button type="button" className="pgm-tape__sound" onClick={toggleSound} aria-pressed={sound}
                title={C.soundHint}>
          {sound ? C.soundOn : C.soundOff}
        </button>
      </header>

      {err ? (
        <p className="unavailable" role="status">
          {C.error}
        </p>
      ) : null}

      <FilterBar filters={filters} setFilters={setFilters} facets={facets} median={median} />

      <div
        ref={listRef}
        className="pgm-tape__list"
        role="grid"
        aria-rowcount={rows.length}
        onScroll={(e) => setScrollTop((e.target as HTMLDivElement).scrollTop)}
      >
        <div style={{ height: win.topPad }} aria-hidden="true" />
        <div className="pgm-tape__viewport" style={{ maxHeight: viewportH }} />
        {rows.slice(win.start, win.end).map((row) => (
          <TapeRow
            key={`${row.conditionId}-${row.tsMs}-${row.anonWallet}-${row.notionalMicro}`}
            row={row}
            fresh={fresh}
            ageMs={ageMs}
            open={openTooltip === row.anonWallet + row.tsMs}
            onOpen={setOpenTooltip}
            onSelectMarket={onSelectMarket}
            onWatch={onWatch}
            watched={watchlist.includes(row.marketId)}
          />
        ))}
        <div style={{ height: win.bottomPad }} aria-hidden="true" />
      </div>

      <footer className="pgm-tape__foot">
        <span>
          {t("terminal.tape.row.counts", {
            n: rows.length,
            whales: rows.filter((r) => r.isWhale).length,
            cap: MAX_ROWS,
          })}
        </span>
        {facets ? (
          <span className="pgm-tape__window" title={facets.whale.rule}>
            {t("terminal.tape.row.window", {
              fills: facets.fills,
              median: formatMicro(facets.medianNotionalMicro),
              p95: formatMicro(facets.p95NotionalMicro),
              max: formatMicro(facets.maxNotionalMicro),
            })}
          </span>
        ) : null}
        <span className="pgm-tape__note">{C.windowNote}</span>
        {status === "disconnected" ? <span role="status">{C.disconnected}</span> : null}
      </footer>
      <audio id="pgm-tape-chime" preload="none" aria-hidden="true" />
    </section>
  );
}

function FilterBar({
  filters,
  setFilters,
  facets,
  median,
}: {
  filters: TapeFilters;
  setFilters: (next: TapeFilters) => void;
  facets: TapeFacets | null;
  median: number;
}) {
  return (
    <div className="pgm-tape__filters" role="group" aria-label={C.filters}>
      <label>
        {C.minNotional}
        <input
          type="number"
          inputMode="numeric"
          min={0}
          step={100}
          value={Math.floor(filters.minNotionalMicro / 1_000_000) || ""}
          onChange={(e) =>
            setFilters({ ...filters, minNotionalMicro: Math.max(0, Number(e.target.value) || 0) * 1_000_000 })
          }
        />
      </label>
      <label title={C.relativeHint}>
        {t("terminal.tape.row.relative", { median: formatMicro(median) })}
        <input
          type="number"
          inputMode="decimal"
          min={0}
          step={0.5}
          value={filters.relativeToMedian || ""}
          onChange={(e) => setFilters({ ...filters, relativeToMedian: Math.max(0, Number(e.target.value) || 0) })}
        />
      </label>
      <label>
        {C.side}
        <select value={filters.side} onChange={(e) => setFilters({ ...filters, side: e.target.value as TapeFilters["side"] })}>
          <option value="">{C.any}</option>
          <option value="BUY">{C.buy}</option>
          <option value="SELL">{C.sell}</option>
        </select>
      </label>
      <label>
        {C.category}
        <select value={filters.category} onChange={(e) => setFilters({ ...filters, category: e.target.value })}>
          <option value="">{C.any}</option>
          {(facets?.categories ?? []).map((c) => (
            <option key={c.value} value={c.value}>
              {c.value} ({c.fills})
            </option>
          ))}
        </select>
      </label>
      <label>
        {C.classification}
        <select value={filters.label} onChange={(e) => setFilters({ ...filters, label: e.target.value })}>
          <option value="">{C.any}</option>
          {(facets?.classifications ?? []).map((c) => (
            <option key={c.label} value={c.label} title={c.rule}>
              {c.label} ({c.fills})
            </option>
          ))}
        </select>
      </label>
      <label>
        {C.wallet}
        <select value={filters.wallet} onChange={(e) => setFilters({ ...filters, wallet: e.target.value })}>
          <option value="">{C.any}</option>
          {(facets?.wallets ?? []).map((w) => (
            <option key={w.anonWallet} value={w.anonWallet}>
              {w.anonWallet} ({w.fills})
            </option>
          ))}
        </select>
      </label>
      <button type="button" onClick={() => setFilters({ ...EMPTY_FILTERS })}>
        {C.reset}
      </button>
    </div>
  );
}

function TapeRow({
  row,
  fresh,
  ageMs,
  open,
  onOpen,
  onSelectMarket,
  onWatch,
  watched,
}: {
  row: TerminalFill;
  fresh: Freshness;
  ageMs: number | null;
  open: boolean;
  onOpen: (key: string | null) => void;
  onSelectMarket?: (marketId: string) => void;
  onWatch?: (marketId: string) => void;
  watched: boolean;
}) {
  const key = row.anonWallet + row.tsMs;
  const [shifting, setShifting] = useState(false);
  return (
    <div
      role="row"
      className={`pgm-tape__row${row.isWhale ? ` is-whale is-${row.severity}` : ""}`}
      style={{ height: ROW_H }}
      tabIndex={0}
      onMouseDown={(e) => setShifting(e.shiftKey)}
      onClick={(e) => {
        const target = rowTarget(row, e.shiftKey || shifting);
        if (target.kind === "watchlist") onWatch?.(row.marketId);
        else onSelectMarket?.(row.marketId);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") onSelectMarket?.(row.marketId);
      }}
    >
      <span className="pgm-tape__time" title={new Date(row.tsMs).toISOString()}>
        {new Date(row.tsMs).toISOString().slice(11, 19)}
      </span>
      <span className="pgm-tape__wallet">
        <a
          href={walletHref(row.anonWallet)}
          onClick={(e) => e.stopPropagation()}
          aria-label={t("terminal.tape.row.openTrader", { wallet: row.anonWallet })}
        >
          {row.anonWallet}
        </a>
        {watched ? <span className="pgm-tape__watched" title={C.watched}>★</span> : null}
      </span>
      {row.labels.map((fact) => (
        // A chip that expands. The sentence it expands to is rendered as text in the cell below the row, and the
        // title is only a convenience for a pointer: a tooltip is not a disclosure.
        <button
          key={fact.label}
          type="button"
          className="pgm-badge"
          aria-expanded={open}
          title={labelSentence(fact)}
          onClick={(e) => {
            e.stopPropagation();
            onOpen(open ? null : key);
          }}
        >
          {fact.label}
        </button>
      ))}
      <span className={`pgm-tape__side is-${row.side.toLowerCase()}`}>{row.side === "BUY" ? C.buy : C.sell}</span>
      <span className="pgm-tape__outcome">{row.outcome}</span>
      {/* The row's own freshness, never a hardcoded "live": a tape whose stamp has gone stale must render its
          prices as such, which is the P05/P08 rule this phase inherits. */}
      <NumberView kind="price" value={priceUnitsFor(row.price, row.tick)} tick={row.tick} freshness={fresh}
        staleMs={ageMs} />
      <NumberView kind="size" value={sharesWhole(row.shares)} />
      <span className="pgm-tape__notional" title={thresholdSentence(row)}>
        {formatMicro(row.notionalMicro)}
        {row.isWhale ? <span className="pgm-tape__multiple">{formatMultiple(row.ratioBps)}</span> : null}
      </span>
      <a
        className="pgm-tape__market"
        href={`/market/${encodeURIComponent(row.marketSlug || row.marketId)}`}
        onClick={(e) => e.stopPropagation()}
        title={row.question}
      >
        {row.marketSlug || row.marketId}
      </a>
      {/* The rules travel with the row: the threshold that made this fill a whale, and — for each label on it —
          the rule and the disclaimer, as TEXT rather than as a title attribute. A disclosure a user can only see
          by hovering is not a disclosure, and a classification screenshot without its caveat is how a label
          becomes an accusation. */}
      {open ? (
        <div className="pgm-tape__tooltip" role="tooltip">
          <span>{thresholdSentence(row)}</span>
          {row.labels.map((fact) => (
            <span key={`why-${fact.label}`} className="pgm-tape__why">
              <strong>{fact.label}</strong>: {labelSentence(fact)}
            </span>
          ))}
        </div>
      ) : null}
      <span className="pgm-visually-hidden">
        {formatShares(sharesMicro(row.shares))} {C.shares}
      </span>
    </div>
  );
}
