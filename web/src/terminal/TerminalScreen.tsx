"use client";
/**
 * D1 · the terminal, assembled.
 *
 * `TerminalLayout` owns the geometry (fractions of the viewport, keyboard-resizable, persisted per user);
 * this file owns what goes IN the three columns, which is the part the kit actually specifies:
 *
 *  left   holdings (with the exit path), the watchlist, the followed wallets, the trending list;
 *  centre the chart, then Activity / Traders / Holders, then the ticket;
 *  right  the market's own facts (resolution criteria, prices, labels) and the top holders.
 *
 * Two rules drove the composition:
 *
 *  - **Nothing is dropped at a breakpoint.** `TerminalLayout` turns the columns into tabs on a phone, and every
 *    panel it tabs to is the same component with the same data — no `display: none` copy of the screen.
 *  - **The watchlist is local, and the rail says so.** The P10 contract has no watchlist resource, so shift-
 *    clicking a tape row stores it on this device and the rail labels the list "on this device" instead of
 *    implying a sync that does not exist. The followed wallets are the same list the tape's sound toggle reads,
 *    which is what makes that toggle do something on a first visit.
 *
 * The Traders tab is the tape aggregated by wallet, computed here rather than fetched: it is a different view of
 * rows already on screen, and a second endpoint for one grouping would be a second set of numbers that could
 * disagree with the first. Its label says "from this window's tape" for the same reason.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { t } from "@/i18n/terminal";
import { freshnessOf, stampFrom, type Freshness } from "@/api/envelope";
import { request } from "@/api/client";
import { Number } from "@/num/Number";
import { StaleIndicator } from "@/num/StaleIndicator";
import { MarketRail, type Holder, type MarketDetail } from "@/screens/MarketRail";
import { PriceChart } from "@/screens/PriceChart";
import { TradeTicket } from "@/screens/TradeTicket";
import { microOf } from "@/lib/depth";
import { microToCents } from "@/money/cents";
import { TapePanel } from "./TapePanel";
import { TerminalLayout } from "./TerminalLayout";
import { useNow, usePortfolio, useTape } from "./useTerminal";
import { EMPTY_FILTERS } from "./tape";
import {
  EMPTY_WATCH,
  followedWallets,
  parseWatch,
  serialiseWatch,
  toggleMarket,
  toggleWallet,
  watchedMarketIds,
  watchKey,
  type WatchStore,
} from "./watchlist";
import type { Candle, Interval } from "@/lib/ladders";

export type TerminalMarketRef = { marketId: string; question: string; slug?: string; volume24h?: string };

const TAB_LABEL: Record<string, string> = {
  activity: t("terminal.tabs.activity"),
  traders: t("terminal.tabs.traders"),
  holders: t("terminal.tabs.holders"),
};

export function TerminalScreen({
  userId,
  markets,
  initialMarketId = "",
}: {
  userId: string;
  markets: TerminalMarketRef[];
  initialMarketId?: string;
}) {
  const [marketId, setMarketId] = useState(initialMarketId || markets[0]?.marketId || "");
  const [store, setStore] = useState<WatchStore>(EMPTY_WATCH);
  const [tab, setTab] = useState<"activity" | "traders" | "holders">("activity");
  const now = useNow(1_000);
  const { data: book } = usePortfolio();

  // Read once, then own it: the store is the state, localStorage is the durability, and a disabled storage must
  // not take the watchlist away for the session.
  useEffect(() => {
    setStore(parseWatch(window.localStorage.getItem(watchKey(userId))));
  }, [userId]);

  const persist = useCallback(
    (next: WatchStore) => {
      setStore(next);
      try {
        window.localStorage.setItem(watchKey(userId), serialiseWatch(next));
      } catch {
        /* see above: this is durability, not state */
      }
    },
    [userId],
  );

  const addMarket = useCallback(
    (id: string) => {
      const known = markets.find((m) => m.marketId === id);
      persist(toggleMarket(store, { marketId: id, question: known?.question ?? id }));
    },
    [markets, persist, store],
  );

  const market = useMarket(marketId);
  const positions = book?.positions ?? [];

  return (
    <TerminalLayout
      userId={userId}
      header={
        <header className="pgm-tape__head">
          <h1>{t("terminal.screen.title")}</h1>
          <span className="pgm-whales__counts">{market.question || t("terminal.screen.noMarket")}</span>
          <StaleIndicator freshness={market.freshness} ageMs={market.stamp ? now - market.stamp.asOf : null} />
          <select
            aria-label={t("terminal.screen.pickMarket")}
            onChange={(e) => setMarketId(e.target.value)}
            value={marketId}
          >
            {markets.map((m) => (
              <option key={m.marketId} value={m.marketId}>
                {m.question || m.marketId}
              </option>
            ))}
          </select>
        </header>
      }
      left={
        <div className="pgm-terminal__panel">
          <section>
            <h2>{t("terminal.rail.holdings")}</h2>
            {positions.length === 0 ? (
              <p className="pgm-whales__counts">{t("terminal.rail.noHoldings")}</p>
            ) : (
              <ul className="pgm-dossier__badges">
                {positions.slice(0, 8).map((p) => (
                  <li className="pgm-badge" key={p.tokenId}>
                    <a href={`/market/${encodeURIComponent(p.marketId)}`}>{p.outcome}</a>
                    <small>
                      {p.size} ·{" "}
                      {p.markSource === "unknown" ? (
                        t("terminal.rail.noMark")
                      ) : (
                        <Number kind="pnl" value={microToCents(p.unrealisedMicro)} />
                      )}
                    </small>
                    {/* One-tap exit: it goes to the market screen, where the book is — a sell button in a
                        15-second-old rail sells into whatever the book is by the time it lands. */}
                    <a href={`/market/${encodeURIComponent(p.marketId)}`}>{t("terminal.rail.exit")}</a>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section>
            <h2>{t("terminal.rail.watchlist")}</h2>
            <p className="pgm-whales__counts">{t("terminal.rail.watchlistRule")}</p>
            {store.markets.length === 0 ? (
              <p className="pgm-whales__counts">{t("terminal.rail.watchlistEmpty")}</p>
            ) : (
              <ul className="pgm-dossier__badges">
                {store.markets.map((m) => (
                  <li className="pgm-badge" key={m.marketId}>
                    <button type="button" onClick={() => setMarketId(m.marketId)}>
                      {m.question}
                    </button>
                    <button
                      type="button"
                      aria-label={t("terminal.rail.remove", { what: m.question })}
                      onClick={() => persist(toggleMarket(store, m))}
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section>
            <h2>{t("terminal.rail.following")}</h2>
            <p className="pgm-whales__counts">{t("terminal.rail.followingRule")}</p>
            {store.wallets.length === 0 ? (
              <p className="pgm-whales__counts">{t("terminal.rail.followingEmpty")}</p>
            ) : (
              <ul className="pgm-dossier__badges">
                {store.wallets.map((w) => (
                  <li className="pgm-badge" key={w.anonWallet}>
                    <a href={`/trader/${encodeURIComponent(w.anonWallet)}`}>{w.label || w.anonWallet}</a>
                    <button
                      type="button"
                      aria-label={t("terminal.rail.remove", { what: w.label || w.anonWallet })}
                      onClick={() => persist(toggleWallet(store, w))}
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section>
            <h2>{t("terminal.rail.trending")}</h2>
            <ol className="pgm-dossier__badges">
              {markets.slice(0, 8).map((m) => (
                <li key={m.marketId}>
                  <button type="button" onClick={() => setMarketId(m.marketId)}>
                    {m.question || m.marketId}
                  </button>
                </li>
              ))}
            </ol>
          </section>
        </div>
      }
      center={
        <div className="pgm-terminal__panel">
          <PriceChart
            candles={market.candles}
            interval={market.interval}
            legend={t("terminal.chart.legend")}
          />
          <div className="pgm-terminal__tabs" role="tablist" aria-label={t("terminal.tabs.activityTitle")}>
            {(["activity", "traders", "holders"] as const).map((id) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                onClick={() => setTab(id)}
                className={tab === id ? "is-active" : ""}
              >
                {TAB_LABEL[id]}
              </button>
            ))}
          </div>
          {tab === "activity" ? (
            <section role="tabpanel" aria-label={TAB_LABEL.activity}>
              <TapePanel
                userId={userId}
                followed={followedWallets(store)}
                watchlist={watchedMarketIds(store)}
                onWatch={addMarket}
                onSelectMarket={setMarketId}
                initialMarketId={marketId}
              />
            </section>
          ) : null}
          {tab === "traders" ? (
            <section role="tabpanel" aria-label={TAB_LABEL.traders}>
              <TradersTab marketId={marketId} />
            </section>
          ) : null}
          {tab === "holders" ? (
            <section role="tabpanel" aria-label={TAB_LABEL.holders}>
              <HoldersTab market={market} />
            </section>
          ) : null}
        </div>
      }
      right={
        <div className="pgm-terminal__panel">
          {market.detail ? (
            <MarketRail
              market={market.detail}
              holders={market.holders}
              freshness={market.freshness}
              staleMs={market.stamp ? market.stamp.staleAfter - market.stamp.asOf : null}
            />
          ) : (
            <p className="pgm-whales__counts" role="status">
              {t("terminal.market.loading")}
            </p>
          )}
          <TradeTicket marketId={marketId} />
        </div>
      }
    />
  );
}

/**
 * The wallet aggregation the Traders tab shows.
 *
 * It reuses the tape's own hook with one market filter, so the numbers here and the numbers in the Activity tab
 * come from one read path — the alternative (a second endpoint) is two sets of numbers that drift.
 */
function TradersTab({ marketId }: { marketId: string }) {
  const { rows, stamp } = useTape({ ...EMPTY_FILTERS, marketId }, 86_400_000, 200);
  const fresh: Freshness = stamp ? freshnessOf(stamp, Date.now()) : "unknown";
  const traders = useMemo(() => {
    const byWallet = new Map<string, { anonWallet: string; fills: number; notionalMicro: number; bought: number; sold: number }>();
    for (const row of rows) {
      const entry = byWallet.get(row.anonWallet) ?? { anonWallet: row.anonWallet, fills: 0, notionalMicro: 0, bought: 0, sold: 0 };
      entry.fills += 1;
      entry.notionalMicro += row.notionalMicro;
      if (row.side === "BUY") entry.bought += 1;
      else entry.sold += 1;
      byWallet.set(row.anonWallet, entry);
    }
    return [...byWallet.values()].sort((a, b) => b.notionalMicro - a.notionalMicro).slice(0, 25);
  }, [rows]);

  return (
    <div>
      <p className="pgm-whales__counts">
        {t("terminal.traders.rule", { n: rows.length })} <StaleIndicator freshness={fresh} ageMs={null} />
      </p>
      <table className="pgm-table">
        <thead>
          <tr>
            <th>{t("terminal.traders.col.wallet")}</th>
            <th>{t("terminal.traders.col.fills")}</th>
            <th>{t("terminal.traders.col.bought")}</th>
            <th>{t("terminal.traders.col.sold")}</th>
            <th>{t("terminal.traders.col.notional")}</th>
          </tr>
        </thead>
        <tbody>
          {traders.map((w) => (
            <tr key={w.anonWallet}>
              <td>
                <a href={`/trader/${encodeURIComponent(w.anonWallet)}`}>{w.anonWallet}</a>
              </td>
              <td>{w.fills}</td>
              <td>{w.bought}</td>
              <td>{w.sold}</td>
              <td>
                <Number kind="money" value={microToCents(w.notionalMicro)} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {traders.length === 0 ? <p role="status">{t("terminal.traders.empty")}</p> : null}
    </div>
  );
}

function HoldersTab({ market }: { market: MarketRead }) {
  if (!market.holders) {
    return <p role="status">{t("terminal.holders.loading")}</p>;
  }
  return (
    <div>
      {/* The provenance sentence is the server's, because a holder count that silently means two different
          things is worse than one that admits which it is. */}
      <p className="pgm-whales__counts">{market.holders.provenance}</p>
      <table className="pgm-table">
        <thead>
          <tr>
            <th>{t("terminal.holders.col.wallet")}</th>
            <th>{t("terminal.holders.col.notional")}</th>
            <th>{t("terminal.holders.col.share")}</th>
            <th>{t("terminal.holders.col.labels")}</th>
          </tr>
        </thead>
        <tbody>
          {market.holders.holders.slice(0, 25).map((h: Holder) => (
            <tr key={h.anonWallet}>
              <td>
                <a href={`/trader/${encodeURIComponent(h.anonWallet)}`}>{h.anonWallet}</a>
              </td>
              <td>
                <Number kind="money" value={Math.trunc(microOf(h.notional ?? "0") / 10 ** 4)} />
              </td>
              <td>{Math.trunc((h.shareBp ?? 0) / 100)}%</td>
              <td>{(h.labels ?? []).join(", ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

type MarketRead = {
  detail: MarketDetail | null;
  holders: { holders: Holder[]; provenance: string; holderCount: number } | null;
  candles: Candle[];
  interval: Interval;
  freshness: Freshness;
  stamp: { asOf: number; staleAfter: number } | null;
  question: string;
};

/**
 * One read per market, three endpoints, one clock.
 *
 * The freshness the panel shows is the OLDEST of the three: a rail that reported the newest of its own reads
 * would say "live" while two of its numbers were a minute old.
 */
function useMarket(marketId: string): MarketRead {
  const [detail, setDetail] = useState<MarketDetail | null>(null);
  const [holders, setHolders] = useState<MarketRead["holders"]>(null);
  const [candles, setCandles] = useState<Candle[]>([]);
  const [stamps, setStamps] = useState<number[]>([]);

  useEffect(() => {
    if (!marketId) return;
    let cancelled = false;
    const load = async () => {
      const [m, h, c] = await Promise.all([
        request<MarketDetail>({ key: "market", params: { market_id: marketId } }),
        request<{ holders: Holder[]; provenance: string; holderCount: number }>({ key: "holders", params: { market_id: marketId } }),
        request<{ candles: Candle[] }>({ key: "history", params: { market_id: marketId, interval: "1h", limit: 200 } }),
      ]);
      if (cancelled) return;
      const marks: number[] = [];
      if (m.ok) {
        setDetail(m.data);
        const stamp = stampFrom(m.data as Record<string, unknown>);
        if (stamp) marks.push(stamp.asOf);
      }
      if (h.ok) setHolders(h.data);
      if (c.ok) setCandles(c.data.candles ?? []);
      setStamps(marks);
    };
    void load();
    const timer = window.setInterval(() => void load(), 10_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [marketId]);

  const oldest = stamps.length ? Math.min(...stamps) : 0;
  const stamp = oldest ? { asOf: oldest, staleAfter: oldest + 10_000 } : null;
  return {
    detail,
    holders,
    candles,
    interval: "1h",
    freshness: stamp ? freshnessOf({ ...stamp, ttlMs: 10_000 }, Date.now()) : "unknown",
    stamp,
    question: (detail as { question?: string } | null)?.question ?? "",
  };
}

/** Exported for the route's server-side first paint of the market label. */
export function marketTitle(ref: TerminalMarketRef): string {
  return ref.question || ref.marketId;
}

/** The volume column the trending list sorts by, in the rail's own units (cents). */
export function volumeCents(ref: TerminalMarketRef): number {
  return Math.trunc(microOf(ref.volume24h ?? "0") / 10 ** 4);
}

export type { Interval };
