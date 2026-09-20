/**
 * The frame the three public pages share.
 *
 * It is deliberately not the app shell. A public page is loaded by somebody who has no account, by an unfurler
 * that runs no JavaScript, and by a crawler — none of which should pay for the terminal's client providers, and
 * all of which need the two things this frame prints instead: **what this page is a page of** (the breadcrumb,
 * as text as well as JSON-LD) and **how the number was computed** (a link to the methodology, and the API's own
 * sentence about why the page is public at all).
 *
 * The last one is a product position, not decoration: publishing a ranking means publishing the rules that
 * produced it, and a reader who cannot reach them has been asked to take our word for it.
 */
import { t } from "@/i18n/public";

export function PublicChrome({
  crumbs,
  children,
  footer,
  methodologyHref = "/leaderboard/risk_adjusted",
}: {
  crumbs: { label: string; href?: string }[];
  children: React.ReactNode;
  footer?: React.ReactNode;
  /** Where "how this is computed" goes. The board page points at its own rules block rather than at a generic
   *  page, because the rules that matter are the ones for the board the reader is looking at. */
  methodologyHref?: string;
}) {
  return (
    <main className="pgm-public">
      <nav className="pgm-public__crumbs" aria-label={t("public.chrome.backToBoard")}>
        <ol>
          {crumbs.map((c, i) => (
            <li key={`${c.label}-${i}`}>
              {c.href ? <a href={c.href}>{c.label}</a> : <span aria-current="page">{c.label}</span>}
            </li>
          ))}
        </ol>
      </nav>
      {children}
      <footer className="pgm-public__foot">
        {footer}
        <p className="pgm-public__note">{t("public.chrome.shareNote")}</p>
        <p className="pgm-public__note">
          <a href={methodologyHref}>{t("public.chrome.methodology")}</a>
        </p>
      </footer>
    </main>
  );
}

/** A refusal or an error, in the same frame, so a broken page looks like a page. */
export function PublicState({ title, body }: { title: string; body: string }) {
  return (
    <main className="pgm-public">
      <p className="pgm-public__state" role="status">
        <strong>{title}</strong>
        <span>{body}</span>
      </p>
    </main>
  );
}
