/**
 * `/trader/<handle>` — the page a trader shares, and the marketing that pays for the leaderboard.
 *
 * Three decisions shape it:
 *
 *  1. **All nine standings, in the payload's order.** The category board is four boards wearing one name, and a
 *     page that printed only the main five would hide the standing a specialist is most likely to be sharing.
 *     Each entry links to the board it is a row of, so the page cites its own sources.
 *  2. **The qualifiers are part of the layout, not a tooltip.** The provisional window, the sample gate and the
 *     worst drawdown sit beside the headline numbers, in the sentences the API wrote for the trader's own
 *     self-rank view. A public page is the one place where a reader cannot go and look up the caveat.
 *  3. **No address, ever.** The identity on this page is the handle and the pseudonym; the payload cannot carry
 *     an address (the leaderboard pseudonymises before the row is built) and this component adds nothing.
 *
 * The card is rendered here as HTML from the same object the OG route draws, and the structured data is the
 * API's graph verbatim.
 */
import { t } from "@/i18n/public";
import { Number } from "@/num/Number";
import { microToCents } from "@/money/cents";
import { StaleIndicator } from "@/num/StaleIndicator";
import { freshnessOf, stampFrom, type Freshness } from "@/api/envelope";
import { JsonLd, type Crumb } from "./JsonLd";
import { PublicChrome } from "./Chrome";
import { ShareCard } from "./ShareCard";
import type { PublicTraderPage } from "./wire";

export function TraderView({ page, now }: { page: PublicTraderPage; now: number }) {
  const stamp = stampFrom(page as unknown as Record<string, unknown>);
  const freshness: Freshness = freshnessOf(stamp, now);
  const ageMs = stamp ? Math.max(0, now - stamp.asOf) : null;
  const head = page.headline;
  const ranked = head.state === "ranked";
  const gated = head.winRateBps === null || head.winRateBps === undefined;
  // The trail is one value, rendered by the chrome and re-stated to the graph: the machine-readable breadcrumb is
  // the one a reader sees, including the copy the API cannot know (it sends its own names, in English).
  const trail: Crumb[] = [
    { label: "Openout", href: "/" },
    { label: t("public.chrome.backToBoard"), href: "/leaderboard/risk_adjusted" },
    { label: `@${page.handle}` },
  ];
  return (
    <PublicChrome
      crumbs={trail}
      footer={
        <p className="pgm-public__note" data-robots={page.robots}>
          {page.note}
        </p>
      }
    >
      <header className="pgm-public__head">
        <h1>{`@${page.handle}`}</h1>
        <p className="pgm-public__lede">
          {ranked
            ? t("public.trader.lede", { rank: String(head.rank), total: String(head.rankedTotal), board: head.board })
            : t("public.trader.unranked")}
        </p>
      </header>

      <section className="pgm-public__numbers" aria-label={t("public.trader.title", { handle: page.handle })}>
        <dl>
          <div>
            <dt>{t("public.trader.headlineRealised")}</dt>
            <dd>
              <Number kind="money" value={microToCents(head.realisedMicro)} />
            </dd>
          </div>
          <div>
            <dt>{t("public.trader.headlineDrawdown")}</dt>
            <dd className={head.maxDrawdownMicro ? "is-bad" : ""}>
              <Number kind="money" value={microToCents(head.maxDrawdownMicro)} />
            </dd>
          </div>
          <div>
            <dt>{t("public.trader.headlineSettled")}</dt>
            <dd>
              <Number kind="count" value={head.settledMarkets} />
            </dd>
          </div>
          <div>
            <dt>{t("public.trader.headlineWinRate")}</dt>
            <dd>
              {/* The gate, printed where the number would be: `—` alone reads as zero, and a zero win rate on a
                  trader who has not met the sample is the one reading of this cell that is definitely wrong. */}
              {gated ? (
                <span className="pgm-public__gated">{t("public.trader.winRateGated")}</span>
              ) : (
                <Number kind="percent" value={head.winRateBps ?? 0} />
              )}
            </dd>
          </div>
        </dl>
        <p className="pgm-public__stamp">
          <StaleIndicator freshness={freshness} ageMs={ageMs} />
          {t("public.trader.updated", { when: stamp ? new Date(stamp.asOf).toISOString().slice(0, 16).replace("T", " ") : "unknown" })}
        </p>
      </section>

      <section className="pgm-public__standing" aria-label={t("public.trader.standing")}>
        <h2>{t("public.trader.standing")}</h2>
        <table>
          <thead>
            <tr>
              <th scope="col">{t("public.board.rank")}</th>
              <th scope="col">{t("public.board.window")}</th>
              <th scope="col">{t("public.board.settled")}</th>
            </tr>
          </thead>
          <tbody>
            {page.standing.map((s) => (
              <tr key={`${s.board}-${s.category ?? ""}`}>
                <td>
                  {s.rank ? <span className="pgm-public__badge">{s.rankBadge && "text" in s.rankBadge
                    ? String((s.rankBadge as { text?: string }).text ?? `#${s.rank}`)
                    : `#${s.rank}`}</span> : <span className="pgm-public__muted">—</span>}
                </td>
                <td>
                  <a href={s.url}>
                    {[s.label, s.category].filter(Boolean).join(" · ") || s.board}
                  </a>
                </td>
                <td>{s.rankedTotal ? String(s.rankedTotal) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="pgm-public__notes" aria-label={t("public.card.footnoteLabel")}>
        <ul>
          {page.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      </section>

      <ShareCard card={page.card} url={page.url} />
      <JsonLd graph={page.structuredData} trail={trail} self={page.url} />
    </PublicChrome>
  );
}
