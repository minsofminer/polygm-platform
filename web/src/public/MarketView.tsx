/**
 * `/market/<slug>` — the odds page a news story links to.
 *
 * The rule that shapes every line of this page: **an odds page is quoted out of context**. Somebody screenshots
 * the number, somebody else repeats it, and the two facts that decide whether the number means anything —
 * *how old is it* and *is this market still open* — are exactly the two that get cropped. So here they are not
 * a footnote: the price renders through the number layer with its freshness (the P08 rule, enforced by the
 * gate), `quoteNote` states the order-book status in the API's own sentence, and the age is repeated in the
 * card's footnote where the screenshot also keeps it.
 *
 * `resolutionCriteria` is upstream text. It is rendered as TEXT — never interpolated into markup, never parsed
 * as markdown — and the source is linked rather than embedded, which is the standing rule for sanitised
 * upstream strings.
 */
import { t } from "@/i18n/public";
import { Number } from "@/num/Number";
import { StaleIndicator } from "@/num/StaleIndicator";
import { microToCents } from "@/money/cents";
import { priceUnitsOf } from "@/lib/depth";
import { freshnessOf, stampFrom, type Freshness } from "@/api/envelope";
import { JsonLd, type Crumb } from "./JsonLd";
import { PublicChrome } from "./Chrome";
import { ShareCard } from "./ShareCard";
import type { PublicMarketPage } from "./wire";

export function MarketPublicView({ page, now }: { page: PublicMarketPage; now: number }) {
  const stamp = stampFrom(page as unknown as Record<string, unknown>);
  const freshness: Freshness = freshnessOf(stamp, now);
  const staleMs = stamp && stamp.staleAfter ? stamp.staleAfter - stamp.asOf : null;
  const tick = page.minimumTickSize;
  const price = page.odds.lastPrice;
  const units = price === null || price === undefined ? null : priceUnitsOf(String(price), tick);
  const trail: Crumb[] = [
    { label: "Openout", href: "/" },
    { label: page.category || "Markets", href: "/markets" },
    { label: page.question },
  ];
  return (
    <PublicChrome
      crumbs={trail}
      footer={
        <p className="pgm-public__note">
          {page.quoteNote} · {page.odds.ageText} · {page.odds.quotedFrom}
        </p>
      }
    >
      <header className="pgm-public__head">
        <h1>{page.question}</h1>
        <p className="pgm-public__lede">
          {page.acceptingOrders ? t("public.market.accepting") : t("public.market.closed")}
          {page.endDate ? ` · ${t("public.market.endDate", { when: new Date(page.endDate).toISOString().slice(0, 10) })}` : ""}
        </p>
      </header>

      <section className="pgm-public__numbers" aria-label={t("public.market.odds")}>
        <dl>
          <div>
            <dt>{t("public.market.odds")}</dt>
            <dd>
              {units === null ? (
                <span className="pgm-public__muted">—</span>
              ) : (
                <Number kind="price" value={units} tick={tick} freshness={freshness} staleMs={staleMs} source="rest" />
              )}
            </dd>
          </div>
          <div>
            <dt>{t("public.market.volume")}</dt>
            <dd>
              <Number kind="money" value={microToCents(page.odds.volume24hMicro)} />
            </dd>
          </div>
          <div>
            <dt>{t("public.market.liquidity")}</dt>
            <dd>{page.odds.liquidity}</dd>
          </div>
          <div>
            <dt>{t("public.market.openInterest")}</dt>
            <dd>{page.odds.openInterest}</dd>
          </div>
        </dl>
        <p className="pgm-public__stamp">
          <StaleIndicator freshness={freshness} ageMs={page.odds.ageMs} />
          {t("public.market.age", { age: page.odds.ageText })}
        </p>
      </section>

      <section className="pgm-public__outcomes" aria-label={t("public.market.outcomes")}>
        <ul>
          {page.outcomes.map((o) => (
            <li key={o.outcome} data-winner={o.winner === null || o.winner === undefined ? "unresolved" : String(o.winner)}>
              <span>{o.outcome}</span>
              <span className="pgm-public__muted">
                {o.winner === null || o.winner === undefined ? "—" : o.winner ? "yes" : "no"}
              </span>
            </li>
          ))}
        </ul>
      </section>

      {page.resolutionCriteria ? (
        <section className="pgm-public__resolution" aria-label={t("public.market.resolution")}>
          <h2>{t("public.market.resolution")}</h2>
          {/* Plain text, exactly as served. No `dangerouslySetInnerHTML`, no markdown, no linkifying. */}
          <p>{page.resolutionCriteria}</p>
        </section>
      ) : null}

      <ShareCard card={page.card} url={page.url} />
      <JsonLd graph={page.structuredData} trail={trail} self={page.url} />
    </PublicChrome>
  );
}
