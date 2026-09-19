"use client";
import { Number as NumberView } from "@/num/Number";
import { t } from "@/i18n/t";
import { microOf, priceUnitsOf, shareUnitsOf } from "@/lib/depth";
import { sanitiseUpstreamText, safeSourceUrl } from "@/lib/upstream";
import type { Freshness } from "@/api/envelope";
import type { SuccessBody } from "@/api/client";
import type { Schemas } from "@/api/types";

/**
 * The market info rail.
 *
 * **Resolution criteria are attacker-influenced text.** Whoever created the market wrote them, so they are the
 * one string on the page an outsider controls, and they are rendered as sanitised PLAIN TEXT — never as HTML,
 * and never through `dangerouslySetInnerHTML`. `sanitiseUpstreamText` strips markup rather than escaping it, so
 * a reader sees the sentence the creator wrote instead of a screenful of entities, and `safeSourceUrl` allows
 * only http(s) so a `javascript:` link cannot be dressed up as the resolution source.
 *
 * The rail is one round trip because five endpoints would produce five `asOf` stamps for one panel, and the
 * freshness indicator answers "how old is what I am looking at" — singular.
 *
 * The holders list says where it comes from ("seen trading" — OUR tape), because a holder count that silently
 * means two different things is worse than one that admits which it is.
 */
/** From the contract, like every other body: see the note on `BookPayload` in `OrderBook.tsx`. */
export type MarketDetail = Schemas["Market"];

/** The holders array's item shape, spelled once here and checked against the contract's item schema. */
export type Holder = NonNullable<SuccessBody<"/v1/markets/{market_id}/holders", "get">["holders"]>[number];

const LABEL_TEXT: Record<string, string> = {
  whale: t("markets.rail.labelWhale"),
  smart_money: t("markets.rail.labelSmart"),
  new_wallet: t("markets.rail.labelNew"),
  cluster: t("markets.rail.labelCluster"),
  wash_like: t("markets.rail.labelWash"),
};

export function MarketRail({
  market,
  holders,
  freshness = "unknown",
  staleMs = null,
}: {
  market: MarketDetail;
  holders: { holders: Holder[]; provenance: string; holderCount: number } | null;
  /** The stamp of the read this panel came from; the panel has one, so it shows one. */
  freshness?: Freshness;
  staleMs?: number | null;
}) {
  const cents = (value?: string) => Math.round(microOf(value ?? "0") / 10 ** 4);
  // Tick units for the two price cells (see `priceUnitsOf`); everything else in this rail is money, i.e. cents.
  const priceUnits = market.lastPrice === null || market.lastPrice === undefined
    ? null
    : priceUnitsOf(market.lastPrice, market.minimumTickSize);
  const changeUnits = market.change24h === null || market.change24h === undefined
    ? null
    : priceUnitsOf(market.change24h, market.minimumTickSize);
  const source = market.resolutionSource ? safeSourceUrl(market.resolutionSource) : null;
  const criteria = market.resolutionCriteria ? sanitiseUpstreamText(market.resolutionCriteria) : null;

  return (
    <aside aria-label={t("markets.rail.title")} className="pgm-rail">
      <section>
        <h2>{t("markets.rail.resolution")}</h2>
        {criteria ? <p data-testid="resolution-criteria">{criteria}</p> : <p className="pgm-fine">{t("markets.rail.noCriteria")}</p>}
        {source ? (
          <p>
            <a href={source} rel="noopener noreferrer nofollow" target="_blank">
              {t("markets.rail.source")}
            </a>
          </p>
        ) : null}
      </section>

      <section>
        <h2>{t("markets.rail.market")}</h2>
        <dl className="pgm-rail-numbers">
          <div>
            <dt>{t("markets.rail.last")}</dt>
            <dd>
              {priceUnits === null ? (
                <span className="pgm-fine">{t("markets.card.noPrice")}</span>
              ) : (
                <NumberView kind="price" value={priceUnits} tick={market.minimumTickSize} freshness={freshness} staleMs={staleMs} />
              )}
            </dd>
          </div>
          <div>
            <dt>{t("markets.rail.change")}</dt>
            <dd>
              {changeUnits === null ? (
                <span className="pgm-fine">{t("markets.card.noChange")}</span>
              ) : (
                <NumberView kind="price" value={changeUnits} tick={market.minimumTickSize} signed freshness={freshness} staleMs={staleMs} />
              )}
            </dd>
          </div>
          <div>
            <dt>{t("markets.card.volume")}</dt>
            <dd>
              <NumberView kind="money" value={cents(market.volume24h)} />
            </dd>
          </div>
          <div>
            <dt>{t("markets.rail.volume7d")}</dt>
            <dd>
              <NumberView kind="money" value={cents(market.volume7d)} />
            </dd>
          </div>
          <div>
            <dt>{t("markets.rail.volume30d")}</dt>
            <dd>
              <NumberView kind="money" value={cents(market.volume30d)} />
            </dd>
          </div>
          <div>
            <dt>{t("markets.card.liquidity")}</dt>
            <dd>
              <NumberView kind="money" value={cents(market.liquidity)} />
            </dd>
          </div>
          <div>
            <dt>{t("markets.rail.openInterest")}</dt>
            <dd>
              <NumberView kind="money" value={cents(market.openInterest)} />
            </dd>
          </div>
          <div>
            <dt>{t("markets.rail.minSize")}</dt>
            <dd>
              <NumberView kind="size" value={shareUnitsOf(market.minimumOrderSize ?? "0")} />
            </dd>
          </div>
        </dl>
        <p className="pgm-fine">
          {t("markets.rail.delay", { seconds: String(market.secondsDelay ?? 0) })} ·{" "}
          {market.enableOrderBook ? t("markets.rail.bookYes") : t("markets.rail.bookNo")}
        </p>
        {market.endDate ? (
          <p className="pgm-fine">{t("markets.card.ends", { when: new Date(market.endDate).toISOString().slice(0, 10) })}</p>
        ) : null}
      </section>

      <section>
        <h2>{t("markets.rail.holders")}</h2>
        {holders === null || holders.holders.length === 0 ? (
          <p className="pgm-fine">{t("markets.rail.noHolders")}</p>
        ) : (
          <>
            <p className="pgm-fine">{t("markets.rail.holdersProvenance", { count: String(holders.holderCount) })}</p>
            <ul className="pgm-holders">
              {holders.holders.map((holder) => (
                <li key={holder.anonWallet}>
                  <code>{holder.anonWallet}</code>
                  <span>
                    <NumberView kind="money" value={cents(holder.notional)} />
                  </span>
                  <span className="pgm-fine">{t("markets.rail.holderShare", { bp: String(holder.shareBp / 100) })}</span>
                  {holder.labels.map((label) => (
                    <span key={label} className="pgm-chip">
                      {LABEL_TEXT[label] ?? t("markets.rail.labelOther")}
                    </span>
                  ))}
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      {market.eventId ? (
        <section>
          <h2>{t("markets.rail.event")}</h2>
          <p>
            <a href={`/event/${encodeURIComponent(market.eventId)}`}>
              {market.eventTitle ?? t("markets.rail.event")}
            </a>
          </p>
          <p className="pgm-fine">{t("markets.rail.outcomes", { count: String(market.outcomeCount ?? 0) })}</p>
        </section>
      ) : null}
    </aside>
  );
}
