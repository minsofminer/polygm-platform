import Link from "next/link";
import { t } from "@/i18n/t";

/**
 * The public landing page is a server component with no client JavaScript beyond the shell: it is the SEO
 * surface (P08 D1's "SEO is a real acquisition channel") and a marketing page that ships 200 KB to say
 * "come sign up" is a page nobody on a phone reads.
 *
 * Copy rules (web/DESIGN.md §8): no promises, no "guaranteed", no financial-advice phrasing, and nothing
 * that reproduces a competitor's marketing. Derived figures are labelled as ours.
 */
export default function Landing() {
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
