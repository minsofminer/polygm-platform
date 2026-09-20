/**
 * The share card, rendered — the same object the OG image route draws, on the page, in HTML.
 *
 * Both renderings read `PublicCard` from the API and nothing else. That is the whole design: the image an
 * unfurler caches, the card a reader sees, and the text they copy all come from one object, so there is no
 * arrangement in which the screenshot says something the page does not. The footnote is printed at the same
 * weight as the numbers — not as fine print — because it is the part that makes the numbers mean anything.
 */
import { t } from "@/i18n/public";
import { cardAlt, cardFootnote, cardFootnoteFindings, cardLines } from "./card";
import type { PublicCard } from "./wire";

export function ShareCard({ card, url }: { card: PublicCard; url: string }) {
  const notes = cardFootnote(card);
  const findings = cardFootnoteFindings(card);
  // The accessible name of the card is its content, built by the same function the OG route uses for its alt
  // text: a share card is a picture of numbers, and a picture of numbers with no name is unreadable.
  return (
    <article className="pgm-share" data-ranked={String(Boolean(card.ranked))} aria-label={cardAlt(card)}>
      <header className="pgm-share__head">
        <span className="pgm-share__brand">{card.brand}</span>
        <h2 className="pgm-share__title">{card.title}</h2>
        <p className="pgm-share__subtitle">{card.subtitle}</p>
      </header>
      <dl className="pgm-share__lines">
        {cardLines(card).map((line) => (
          <div key={line.label}>
            <dt>{line.label}</dt>
            <dd>{line.value}</dd>
          </div>
        ))}
      </dl>
      {notes.length ? (
        <ul className="pgm-share__foot" aria-label={t("public.card.footnoteLabel")}>
          {notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}
      {/* A card whose own footnote is missing something the product refuses to drop says so on the page. The
          alternative is a nicer card and a quieter lie, and this component is not allowed to pick that. */}
      {findings.length ? (
        <ul className="pgm-share__findings" role="alert">
          {findings.map((f) => (
            <li key={f}>{f}</li>
          ))}
        </ul>
      ) : null}
      <footer className="pgm-share__foot-brand">
        <span>{card.footer}</span>
        <a href={url} rel="canonical">
          {url.replace(/^https?:\/\//, "")}
        </a>
      </footer>
    </article>
  );
}
