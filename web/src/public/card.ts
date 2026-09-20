/**
 * The card, as a pure function of the payload.
 *
 * A share card is the most dangerous surface this phase ships, and for one reason: it is the artifact that gets
 * screenshotted *without the page around it*. Everything that qualifies a number — the sample gate, the
 * provisional window, the drawdown — exists on the page precisely so a reader can tell a lucky month from an
 * edge; a card that drops it converts an honest measurement into a boast. So the API builds the card (the
 * footnote's priority order lives in `packages/polygm_core/public_pages/cards.py`, where it is unit-tested) and
 * this module does not re-decide any of it: it reads the object and renders it.
 *
 * The two functions here that DO decide something are:
 *
 *  - `cardFootnoteFindings` — the re-check. The API already forces the provisional label and the drawdown into a
 *    ranked trader card; this asks again, per kind of card, so a card that arrives without them is a visible
 *    refusal on the page rather than a quietly nicer image. It is kind-aware on purpose: a market card's duty is
 *    to carry the price's AGE (the number that gets quoted out of context), and a board card's is to carry the
 *    rules line. One rule for all three would either miss the market's or invent a duty for the board's.
 *  - `cardAlt` — the image's alt text, which is the only version of the card a screen reader or a text-only
 *    unfurl gets. It is built from the same lines, so the two cannot diverge.
 */
import type { PublicCard } from "./wire";

/** The card's headline lines, in the API's order. Never re-sorted, never re-labelled. */
export function cardLines(card: PublicCard): { label: string; value: string }[] {
  return (card.lines ?? []).map((l) => ({ label: String(l.label), value: String(l.value) }));
}

/**
 * The footnote, or a refusal.
 *
 * The contract says a card carries at most two notes and that they are the two the product refuses to drop.
 * This does not repair a card that arrived without them: a repair would hide the bug at the reader's expense,
 * so the missing line is named instead — and the page crashes its own test rather than shipping the card.
 */
export function cardFootnote(card: PublicCard): string[] {
  return (card.footnote ?? []).map((f) => String(f));
}

export function cardFootnoteFindings(card: PublicCard): string[] {
  const notes = cardFootnote(card).join(" | ").toLowerCase();
  const out: string[] = [];
  if (card.kind === "market") {
    // The market card's footnote is the price's age and the volume. An odds card without an age is the one
    // screenshot that is always wrong out of context, so its absence is a finding.
    if (!/(ago|as of|second|minute|hour)/.test(notes)) {
      out.push("an odds card with no age: the number that is quoted out of context is the one that needs its clock");
    }
    return out;
  }
  if (card.kind === "leaderboard") {
    if (!notes) out.push("a board card with no rules line: the board it summarises ranks nobody without them");
    return out;
  }
  // A trader card. Only a RANKED one has the duty, and the PROVISIONAL line is owed by the cards whose record is
  // inside its first week — the card says which it is rather than leaving it to be inferred, because a card that
  // prints "provisional" for a forty-day record is as wrong as one that omits it for a three-day record.
  if (!card.ranked) return out;
  if (card.provisional && !notes.includes("provisional")) {
    out.push("a provisional card with no provisional line: the age of the record is what separates an edge from a streak");
  }
  if (!notes.includes("drawdown")) {
    out.push("a card that prints a profit with no drawdown is a card that hides the risk that produced it");
  }
  return out;
}

/**
 * The alt text, built from the card's own parts.
 *
 * Deliberately NOT `card.footer`: the footer is the brand line, which says nothing about the numbers, and an
 * image whose alt text is a slogan is an image a screen-reader user cannot use.
 */
export function cardAlt(card: PublicCard): string {
  const stats = cardLines(card)
    .map((l) => `${l.label} ${l.value}`)
    .join(", ");
  const foot = cardFootnote(card);
  return [`${card.title}. ${card.subtitle}`, stats, foot.join(". "), card.brand].filter(Boolean).join(" — ");
}

/**
 * The text a reader copies when they share the page, in the two places they actually share it.
 *
 * One function for both because the alternative — a different string per target — is how a page ends up with a
 * URL that works in a chat client and a bare domain that works in a post. `url` is the payload's own canonical
 * URL, not `location.href`: the address bar can carry a tracking parameter, and the canonical URL cannot.
 */
export function shareText(card: PublicCard, url: string): string {
  const stats = cardLines(card)
    .slice(0, 3)
    .map((l) => `${l.label} ${l.value}`)
    .join(" · ");
  return [card.title, card.subtitle, stats, cardFootnote(card).join(" · "), url].filter(Boolean).join("\n");
}

/**
 * Whether the card may be published as an image at all.
 *
 * A card is an image on somebody else's server, cached by an unfurler we do not control, which is why this is a
 * separate question from "may the page be crawled": a page can be `noindex, follow` (a provisional wallet) and
 * still deserve its card, because the card travels to the people the trader sent it to. The one case that stops
 * the image is a payload the API itself marks as unranked AND without a score — there is nothing to draw.
 */
export function cardIsDrawable(card: PublicCard): boolean {
  return Boolean(card.title && card.subtitle);
}
