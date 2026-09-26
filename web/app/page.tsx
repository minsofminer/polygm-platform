import Link from "next/link";
import { t } from "@/i18n/t";
import { TmaSurface } from "@/tma/TmaSurface";
import { isMiniAppSurface } from "@/tma/surface.server";

/**
 * The public landing page is a server component with no client JavaScript beyond the shell: it is the SEO
 * surface (P08 D1's "SEO is a real acquisition channel") and a marketing page that ships 200 KB to say
 * "come sign up" is a page nobody on a phone reads.
 *
 * Copy rules (web/DESIGN.md §8): no promises, no "guaranteed", no financial-advice phrasing, and nothing
 * that reproduces a competitor's marketing. Derived figures are labelled as ours.
 */
function Landing() {
  return (
    <main style={{ padding: "var(--pgm-pad-screen-comfortable)", maxWidth: "var(--pgm-shell-max-width)", margin: "0 auto" }}>
      <h1>{t("shell.tagline.value")}</h1>
      <p>
        The tape, the books and the wallets behind them, on one keyboard. Openout reads Polymarket&apos;s public
        data and shows you what a whale is doing while it is doing it, with the freshness of every number on
        the screen next to the number.
      </p>
      <p>
        PnL, rank and win rate on this site are <strong>our</strong> calculations from the public tape, not
        Polymarket&apos;s: there is no upstream endpoint for them, and we do not pretend otherwise.
      </p>
      <p>
        <Link href="/sign-in" className="button">
          {t("auth.signin.submit")}
        </Link>{" "}
        <Link href="/markets" className="button">
          {t("shell.nav.markets")}
        </Link>
      </p>
      <h2>Pro</h2>
      <p>{t("billing.honest.pro")}</p>
      <p>{t("billing.page.paywallRule")}</p>
    </main>
  );
}

/**
 * Two deployments, one root path.
 *
 * The Mini App's own domain must answer at `/` — that is the URL registered with BotFather, and a customer who taps
 * "Trade" in a channel alert has to land on the market card, not on a marketing page with a sign-in button. The main
 * site keeps the landing page, because that is its job. Which one this is comes from `PGM_SURFACE` on the deployment,
 * read on the server: a client-side branch would ship both pages and pick one after hydration, which is a flash of
 * the wrong product on the slowest device the product is for.
 *
 * The branch is on the server, but until P15 the *import* was static, and that is a different thing: it put the
 * whole Mini App into the chunk graph of this route, so the landing page carried the trade sheet and the wallet to
 * every visitor. `TmaSurface` is the chunk boundary (`next/dynamic`), which is why this file imports a name rather
 * than the screen itself.
 */
export default function Root() {
  if (isMiniAppSurface()) return <TmaSurface />;
  return <Landing />;
}
