import { t } from "@/i18n/t";
import { serverRead } from "@/api/server-read";
import { serverAuth } from "@/auth/server";
import { marketTitle, TerminalScreen, type TerminalMarketRef } from "@/terminal/TerminalScreen";

export const dynamic = "force-dynamic";

/**
 * The terminal — D1.
 *
 * The market list is read on the server so the picker, the trending rail and the selected market's title are in
 * the first paint; the panels themselves are client surfaces (a live tape is not a document). The list is sorted
 * by 24h volume because that is what "trending" means here, and the read is the same endpoint `/markets` uses, so
 * the two screens cannot disagree about which markets exist or what they are called.
 *
 * A failed read is not a failed page: the terminal still renders its three columns with an empty picker and the
 * panels' own refusals, which is the difference between "the API is down" and "this route is blank".
 */
export default async function TerminalPage() {
  // The layout key is per user, so the panel widths and the watchlist follow the account and not the browser.
  const auth = await serverAuth();
  const read = await serverRead<{ items: TypicalMarket[] }>("GET", "/v1/markets?limit=50&sortBy=volume24h");
  const items = read.ok === false ? [] : (read.data.items ?? []);
  const markets: TerminalMarketRef[] = items.map((m) => ({
    marketId: m.id,
    question: marketTitle({ marketId: m.id, question: m.question }),
    slug: m.slug,
    volume24h: m.volume24h,
  }));

  return (
    <main className="pgm-page pgm-page--terminal">
      {read.ok === false ? (
        <p className="unavailable" role="status">
          <span>{t("terminal.screen.marketsUnavailable")}</span> <span>{read.message}</span>
        </p>
      ) : null}
      <TerminalScreen userId={auth.userId ?? "anon"} markets={markets} />
    </main>
  );
}

/** The three fields the terminal's picker needs from `/v1/markets`' items. */
type TypicalMarket = { id: string; question: string; slug?: string; volume24h?: string };
