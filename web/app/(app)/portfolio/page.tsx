import { t } from "@/i18n/t";
import { serverRead } from "@/api/server-read";
import { PortfolioView } from "@/terminal/PortfolioView";
import type { Portfolio } from "@/terminal/wire";

export const dynamic = "force-dynamic";

/**
 * The portfolio — D6.
 *
 * The first paint is a server read of the same endpoint the client polls, so a user who opens this page sees
 * their positions, the mark rule and the drawdown before any JavaScript runs — which matters most for the one
 * screen in the product where a missing number is expensive. The client takes over a second later and keeps
 * the same payload shape; the two cannot disagree because it is one endpoint.
 */
export default async function PortfolioPage() {
  const read = await serverRead<Portfolio>("GET", "/v1/me/portfolio");
  return (
    <main className="pgm-page">
      <h1>{t("shell.nav.portfolio")}</h1>
      {read.ok === false ? (
        <p className="unavailable" role="status">
          <span>{t("terminal.portfolio.readFailed")}</span> <span>{read.message}</span>
        </p>
      ) : null}
      <PortfolioView initial={read.ok === false ? null : read.data} />
    </main>
  );
}
