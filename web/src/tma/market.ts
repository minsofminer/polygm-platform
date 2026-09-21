/**
 * From the API's payloads to the sheet's `MarketView` — the only place a payload shape is read.
 *
 * Kept out of the component for the reason every other data module in this app exists: the mapping is where a
 * *product* rule lives (which price is the ask, what "no recent quote" looks like, when a market counts as ending
 * soon), and a rule inside JSX is a rule no test can hold still.
 *
 * Three of the rules, because they are the ones that get broken by a mapper written in a hurry:
 *
 *  - **A missing price is "—", never 0.** A market with no offers that renders `0.0¢` looks like a free lottery
 *    ticket, and the sheet's `blockers()` is written to refuse exactly that.
 *  - **The NO side is derived, not invented.** For a binary market the YES ask and the NO ask are complements about
 *    100¢, which is arithmetic the venue itself uses; the derivation is stated here so nobody "fixes" it later.
 *  - **The age is the book's age, not the page's.** `staleAfter`/`asOf` on the book response is when the snapshot
 *    was taken; rendering the client's clock would claim a freshness we did not receive.
 */
import type { SuccessBody } from "@/api/types";
import type { MarketView } from "./trade";

/** The gap between a book snapshot and now, in the words the rest of the product uses. */
export function ageText(asOfMs: number, nowMs: number): string {
  const ms = Math.max(0, Math.trunc(nowMs - asOfMs));
  if (ms < 2_000) return "as of just now";
  if (ms < 60_000) return `as of ${Math.round(ms / 1_000)} seconds ago`;
  if (ms < 3_600_000) return `as of ${Math.round(ms / 60_000)} minutes ago`;
  return `as of ${Math.round(ms / 3_600_000)} hours ago`;
}

/** `"0.62"` → `620000` micro-cents, by digits — never `Number()`: the API sends decimal strings precisely so that no
 * float ever touches a price, and this side of the wire keeps that promise. `null` when it is not a price at all. */
export function microFromPrice(price: string | null | undefined): number | null {
  if (price === null || price === undefined) return null;
  const m = /^(\d+)(?:\.(\d{1,6}))?$/.exec(String(price).trim());
  if (!m) return null;
  const micro = Number(m[1]) * 1_000_000 + Number((m[2] ?? "").padEnd(6, "0"));
  return micro > 0 && micro < 1_000_000 ? micro : null;
}

export function centsFromMicros(micro: number | null): string {
  if (micro === null) return "—";
  return `${Math.trunc(micro / 10_000)}.${Math.trunc((micro % 10_000) / 1_000)}¢`;
}

export function centsFromMicro(price: string | null | undefined): string {
  return centsFromMicros(microFromPrice(price));
}

/**
 * The two payloads this module reads, derived from the contract rather than retyped.
 *
 * The P08 gate fails a module that hand-types an API body, and it is right to: a body shape written out here by hand
 * is a second copy of the contract that the contract cannot update. `SuccessBody` reads the 2xx
 * schema out of `src/api/schema.gen.ts`, so when a field is renamed in `contracts/openapi.yaml` these break here
 * instead of lying quietly.
 *
 * They are `Partial<>` on purpose, and the reason is a product one: a ladder's sides are genuinely optional (a market
 * with no offers has no bids) and a mapper is called from tests with payloads built by hand. What is *not* optional is
 * where the field names come from.
 */
type PublicMarket = SuccessBody<"/v1/public/market/{slug}", "get">;
type BookPayload = SuccessBody<"/v1/markets/{market_id}/book", "get">;
export type BookLevel = Partial<BookPayload["asks"][number]>;
type BookRead = Omit<Partial<BookPayload>, "asks" | "bids" | "bestAsk" | "bestBid"> & {
  asks?: BookLevel[];
  bids?: BookLevel[];
  // Nullable, unlike the contract's `Price`, because the snapshot carries the null case: a one-sided book IS the
  // market's state, and the sheet's copy for it is a sentence rather than an empty ladder.
  bestAsk?: string | null;
  bestBid?: string | null;
};

export function yesAskFromBook(book: BookRead | null | undefined): { yes: string; no: string; spread: string } {
  // `shares`, not `size`: the ladder's field names are the API's, read off the route that builds them rather than
  // guessed from a schema sketch. A level with no shares behind it is not an offer, so it is filtered out.
  const asks = (book?.asks ?? []).filter((l) => Number(l.shares) > 0);
  const bids = (book?.bids ?? []).filter((l) => Number(l.shares) > 0);
  // The ladder arrives best-first from the API; sorting defensively here costs nothing and makes the function total.
  const bestAsk = asks.slice().sort((a, b) => Number(a.price) - Number(b.price))[0];
  const bestBid = bids.slice().sort((a, b) => Number(b.price) - Number(a.price))[0];
  const askMicros = microFromPrice(bestAsk?.price ?? null);
  const bidMicros = microFromPrice(bestBid?.price ?? null);
  // Integer arithmetic end to end. The first version of this computed the spread as `Number(a) - Number(b)`, which
  // gives `0.010000000000000009` for 0.62 − 0.61 — the exact class of bug the API's decimal-string contract exists to
  // prevent, reintroduced one module later by a subtraction that looked harmless.
  return {
    yes: centsFromMicros(askMicros),
    no: centsFromMicros(askMicros === null ? null : 1_000_000 - askMicros),
    spread: askMicros === null || bidMicros === null ? "—" : centsFromMicros(askMicros - bidMicros),
  };
}

export function closingText(endMs: number | null | undefined, nowMs: number): string {
  if (!endMs) return "";
  const hours = (endMs - nowMs) / 3_600_000;
  if (hours <= 0) return "closed";
  if (hours < 1) return `closes in ${Math.round(hours * 60)} min`;
  if (hours < 48) return `closes in ${Math.round(hours)} h`;
  return `closes ${new Date(endMs).toISOString().slice(0, 10)}`;
}

export function endsSoon(endMs: number | null | undefined, nowMs: number): boolean {
  // Six hours, the same window the alert channel treats as "resolving soon" — one threshold in the product, not two.
  return !!endMs && endMs - nowMs <= 6 * 3_600_000 && endMs > nowMs;
}

/** The whole mapping, from the two reads the page makes. */
export function toMarketView(
  market: PublicMarket & { id: string },
  book: BookRead | null,
  asOfMs: number,
  nowMs: number,
): MarketView {
  const { yes, no, spread } = yesAskFromBook(book);
  return {
    slug: market.slug ?? market.id,
    marketId: market.id,
    question: market.question,
    yesAsk: yes,
    noAsk: no,
    spread,
    ageText: ageText(asOfMs, nowMs),
    closesText: closingText(market.endDate, nowMs),
    minSizeMicro: "0",
    endsSoon: endsSoon(market.endDate, nowMs),
  };
}

/**
 * The two reads the sheet is built from, and the rule about which stamp wins.
 *
 *  1. `/v1/public/market/{slug}` resolves the deep link's slug. It is the *public* read on purpose: a link out of a
 *     channel alert opens in a webview whose user may not have a session yet, and the card must still render.
 *  2. `/v1/markets/{id}/book` prices the side. Its `asOf` is the age of the ladder itself.
 *
 * The freshness shown is the **older** of the two stamps. A snapshot from a minute ago beside a row from now is
 * still a minute-old price, and a sheet is allowed to understate freshness and never to overstate it.
 */
export type TmaRead<T> = { ok: true; data: T & { asOf: number } } | { ok: false; code: string; message: string };

/**
 * The page injects this: a function that reads one of the two routes the sheet needs and hands back the payload plus
 * its stamp. Route *keys*, not paths — the page owns the URL vocabulary (`ROUTES`), this module owns the mapping.
 */
export type TmaGet = (key: "publicMarketPage" | "book", params: Record<string, string>) => Promise<TmaRead<unknown>>;

export async function loadMarketForSheet(get: TmaGet, slug: string): Promise<{ view: MarketView } | { error: string }> {
  const page = (await get("publicMarketPage", { slug })) as TmaRead<PublicMarket>;
  if (!page.ok) {
    return { error: page.code === "NOT_FOUND"
      ? "That market is not listed here any more. Nothing was traded."
      : "We could not read that market just now. Try again from the bot." };
  }
  const market = page.data;
  const id = String(market.marketId ?? "");
  const book = id ? ((await get("book", { market_id: id })) as TmaRead<BookPayload>) : null;
  const stamped = book && book.ok ? book.data : null;
  const asOf = Math.min(Number(page.data.asOf) || 0, stamped ? Number(stamped.asOf) || 0 : Infinity);
  const now = Date.now();
  const prices = yesAskFromBook(stamped);
  return {
    view: {
      slug: String(market.slug ?? slug),
      marketId: id || slug,
      question: String(market.question ?? "Untitled market"),
      yesAsk: prices.yes,
      noAsk: prices.no,
      spread: prices.spread,
      ageText: ageText(asOf, now),
      closesText: closingText(market.endDate ?? null, now),
      minSizeMicro: "0",
      endsSoon: endsSoon(market.endDate ?? null, now),
    },
  };
}
