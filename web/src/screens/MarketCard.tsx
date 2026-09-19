"use client";
import { Number as NumberView } from "@/num/Number";
import { t } from "@/i18n/t";
import { microOf, priceUnitsOf } from "@/lib/depth";
import type { Freshness } from "@/api/envelope";

/**
 * A discovery card.
 *
 * The card's job is to answer three questions without a click: what is it, what does the market think it is
 * worth, and is it alive. That third one is why a card carries its volume and its end time rather than only a
 * price — P01 measured a median event at $19,910/day, and a card that shows "42¢" for a market that trades
 * twice a week is the same card as one that trades all day.
 *
 * **The 128-outcome rule:** when a card carries an event with more than `SUMMARY_AT` markets, it renders the
 * top three BY 24H VOLUME plus a count of how many it left out. Listing them is unreadable; showing the first
 * three is wrong (they are alphabetical, and the alphabetical first of a 128-candidate field is a $400/day
 * market). Sorting by volume is what makes "top three" mean "the three anyone asks about".
 */
export const SUMMARY_AT = 5;

export type MarketRow = {
  id: string;
  question: string;
  category?: string;
  negRisk?: boolean;
  minimumTickSize: string;
  acceptingOrders: boolean;
  endTs?: number | null;
  lastPrice?: string | null;
  change24h?: string | null;
  volume24h?: string;
  liquidity?: string;
  openInterest?: string;
  outcomeCount?: number;
  eventId?: string | null;
  event?: {
    id: string;
    title: string;
    marketCount: number;
    hiddenCount: number;
    totalVolume24h: string;
    topOutcomes: Array<{ marketId: string; question: string; price: string | null; volume24h: string }>;
  } | null;
};

export function MarketCard({
  market,
  onOpen,
  freshness,
  staleMs,
}: {
  market: MarketRow;
  onOpen?: (id: string) => void;
  /** Every price surface carries freshness — the P08 gate's c10 refuses a `kind="price"` without it, because
   *  a number that cannot say how old it is is a number a reader will assume is live. */
  freshness: Freshness;
  staleMs: number | null;
}) {
  // Tick units, the renderer's contract (see `priceUnitsOf`), while the money cells below stay cents.
  const priceUnits = market.lastPrice === null || market.lastPrice === undefined
    ? null
    : priceUnitsOf(market.lastPrice, market.minimumTickSize);
  const changeUnits = market.change24h === null || market.change24h === undefined
    ? null
    : priceUnitsOf(market.change24h, market.minimumTickSize);
  const volumeCents = Math.round(microOf(market.volume24h ?? "0") / 10 ** 4);
  const liquidityCents = Math.round(microOf(market.liquidity ?? "0") / 10 ** 4);
  const summarised = market.event !== null && market.event !== undefined && market.event.marketCount > SUMMARY_AT;

  return (
    <article className="pgm-card" data-neg-risk={market.negRisk ? "true" : "false"}>
      <header>
        <h3>
          <button type="button" className="pgm-card-link" onClick={() => onOpen?.(market.id)}>
            {market.question}
          </button>
        </h3>
        <p className="pgm-card-meta">
          {market.category ? <span className="pgm-chip">{market.category}</span> : null}
          {market.negRisk ? <span className="pgm-chip">{t("markets.card.negRisk")}</span> : null}
          {!market.acceptingOrders ? <span className="pgm-chip">{t("markets.card.closed")}</span> : null}
        </p>
      </header>

      {summarised && market.event ? (
        <section className="pgm-card-event" aria-label={t("markets.card.eventOutcomes")}>
          <p className="pgm-card-event-title">{market.event.title}</p>
          <ul>
            {market.event.topOutcomes.map((outcome) => (
              <li key={outcome.marketId}>
                <button type="button" className="pgm-card-link" onClick={() => onOpen?.(outcome.marketId)}>
                  {outcome.question}
                </button>
                <span>
                  {outcome.price === null ? (
                    <span className="pgm-fine">{t("markets.card.noPrice")}</span>
                  ) : (
                    <NumberView kind="price" value={priceUnitsOf(outcome.price, market.minimumTickSize)} tick={market.minimumTickSize}
                      freshness={freshness} staleMs={staleMs} />
                  )}
                </span>
              </li>
            ))}
          </ul>
          <p className="pgm-fine">{t("markets.card.moreOutcomes", { count: String(market.event.hiddenCount) })}</p>
        </section>
      ) : null}

      <dl className="pgm-card-numbers">
        <div>
          <dt>{t("markets.card.last")}</dt>
          <dd>
            {priceUnits === null ? (
              <span className="pgm-fine">{t("markets.card.noPrice")}</span>
            ) : (
              <NumberView kind="price" value={priceUnits} tick={market.minimumTickSize} freshness={freshness}
                staleMs={staleMs} />
            )}
          </dd>
        </div>
        <div>
          <dt>{t("markets.card.change")}</dt>
          <dd>
            {changeUnits === null ? (
              <span className="pgm-fine">{t("markets.card.noChange")}</span>
            ) : (
              <NumberView kind="price" value={changeUnits} tick={market.minimumTickSize} signed
                freshness={freshness} staleMs={staleMs} />
            )}
          </dd>
        </div>
        <div>
          <dt>{t("markets.card.volume")}</dt>
          <dd>
            <NumberView kind="money" value={volumeCents} />
          </dd>
        </div>
        <div>
          <dt>{t("markets.card.liquidity")}</dt>
          <dd>
            <NumberView kind="money" value={liquidityCents} />
          </dd>
        </div>
      </dl>

      {market.endTs ? (
        <p className="pgm-fine">{t("markets.card.ends", { when: new Date(market.endTs).toISOString().slice(0, 10) })}</p>
      ) : (
        <p className="pgm-fine">{t("markets.card.noEnd")}</p>
      )}
    </article>
  );
}
