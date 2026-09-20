"use client";
import { useCallback, useEffect, useState } from "react";
import { request } from "@/api/client";
import { freshnessOf, stampFrom, type Freshness } from "@/api/envelope";
import { t } from "@/i18n/t";
import { microOf } from "@/lib/depth";
import type { Candle, Interval } from "@/lib/ladders";
import { TradeTicket } from "./TradeTicket";
import { OrderBook, type BookPayload } from "./OrderBook";
import { PriceChart } from "./PriceChart";
import { MarketRail, type Holder, type MarketDetail } from "./MarketRail";

/**
 * The binary/few-outcome layout: chart-led, book beside it, ticket on the right, rail below.
 *
 * Every price surface here carries a freshness: the chart, the book and the ticket all read the same `asOf`
 * from the same poll, so the page answers "how old is this" once instead of three times with three different
 * answers. The poll is deliberately slow (2s) and tagged `rest` — the tape's WebSocket is the live path, and a
 * second high-frequency REST loop would spend the shared rate budget the ingest exists to protect.
 */
const POLL_MS = 2_000;
const HISTORY_LIMIT = 200;

export function MarketView({
  market,
  stamp = null,
}: {
  market: MarketDetail;
  /** The SSR read's stamp, for the surfaces that are not polled (the rail, the chart's own header). */
  stamp?: { asOf?: number; staleAfter?: number } | null;
}) {
  const [book, setBook] = useState<BookPayload | null>(null);
  const [bookFreshness, setBookFreshness] = useState<Freshness>("unknown");
  const [bookStaleMs, setBookStaleMs] = useState<number | null>(null);
  const marketFreshness: Freshness = freshnessOf(stampFrom((stamp ?? {}) as Record<string, unknown>), Date.now());
  const marketStaleMs =
    stamp && stamp.asOf !== undefined && stamp.staleAfter !== undefined ? stamp.staleAfter - stamp.asOf : null;
  const [candles, setCandles] = useState<Candle[]>([]);
  const [interval, setInterval] = useState<Interval>("1m");
  const [holders, setHolders] = useState<{ holders: Holder[]; provenance: string; holderCount: number } | null>(null);
  const [picked, setPicked] = useState<{ price: string; side: "bid" | "ask" } | null>(null);

  const loadBook = useCallback(async () => {
    const out = await request<BookPayload & Record<string, unknown>>({
      key: "book",
      params: { market_id: market.id, depth: 400 },
    });
    if (!out.ok) {
      setBook(null);
      setBookFreshness("unknown");
      setBookStaleMs(null);
      return;
    }
    setBook(out.data);
    const bookStamp = stampFrom(out.data as Record<string, unknown>);
    setBookFreshness(freshnessOf(bookStamp, Date.now()));
    setBookStaleMs(bookStamp === null ? null : bookStamp.staleAfter - bookStamp.asOf);
  }, [market.id]);

  useEffect(() => {
    void loadBook();
    const timer = window.setInterval(() => void loadBook(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [loadBook]);

  useEffect(() => {
    void (async () => {
      const out = await request<{ candles: Candle[] }>({
        key: "history",
        params: { market_id: market.id, interval, limit: HISTORY_LIMIT },
      });
      if (out.ok) setCandles(out.data.candles);
    })();
  }, [market.id, interval]);

  useEffect(() => {
    void (async () => {
      const out = await request<{ holders: Holder[]; provenance: string; holderCount: number }>({
        key: "holders",
        params: { market_id: market.id },
      });
      if (out.ok) setHolders(out.data);
    })();
  }, [market.id]);

  const positionAverageMicro =
    market.lastPrice === null || market.lastPrice === undefined ? null : microOf(market.lastPrice);

  return (
    <div className="pgm-market-view">
      <header className="pgm-market-head">
        <h1>{market.question}</h1>
        <p className="pgm-fine">
          {market.category ? <span className="pgm-chip">{market.category}</span> : null}
          {market.negRisk ? <span className="pgm-chip">{t("markets.card.negRisk")}</span> : null}
          <span className="pgm-freshness">{t("markets.freshness.book", { state: bookFreshness })}</span>
        </p>
        {!market.acceptingOrders ? (
          <p className="refusal" role="status">
            {t("markets.state.notAccepting")}
          </p>
        ) : null}
        {market.secondsDelay ? (
          <p className="refusal" role="status">
            {t("markets.state.delay", { seconds: String(market.secondsDelay) })}
          </p>
        ) : null}
      </header>

      <div className="pgm-market-main">
        <div className="pgm-market-left">
          <PriceChart
            candles={candles}
            interval={interval}
            endTs={market.endDate ?? null}
            averageCostMicro={positionAverageMicro}
            legend={t("markets.chart.legend")}
          />
          <div role="group" aria-label={t("markets.chart.interval")} className="pgm-segmented">
            {(["1m", "5m", "15m", "1h", "6h", "1d"] as Interval[]).map((option) => (
              <button key={option} type="button" aria-pressed={option === interval} onClick={() => setInterval(option)}>
                {option}
              </button>
            ))}
          </div>
        </div>
        <div className="pgm-market-right">
          <OrderBook
            book={book}
            tick={market.minimumTickSize}
            onPick={(price, side) => setPicked({ price, side })}
            freshness={bookFreshness}
            staleMs={bookStaleMs}
            source="rest"
          />
          {/* The ticket needs a market *slug*, which is what the server-priced route resolves against. It
              used to take an id and default to the string "demo" — a ticket that could post a market nobody
              has. */}
          <TradeTicket slug={market.slug} />
          {picked ? (
            <p className="pgm-fine" role="status" data-testid="picked">
              {t("markets.ticket.picked", { price: picked.price, side: picked.side })}
            </p>
          ) : null}
        </div>
      </div>

      <MarketRail market={market} holders={holders} freshness={marketFreshness} staleMs={marketStaleMs} />
    </div>
  );
}
