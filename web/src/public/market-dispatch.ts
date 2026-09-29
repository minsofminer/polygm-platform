/**
 * Which page a `/market/<segment>` address is.
 *
 * The segment serves two pages from one URL space (P09's detail page and D6's public odds page), and Next
 * forbids two dynamic names at the same level, so the dispatch is a property of the address itself. The rule
 * that shipped was "starts with `0x` and is hexadecimal", which is true of a venue condition id — and false of
 * *every id this database has*. The seed's markets are `0xM1`…`0xM159` (`M` for mock, so a fixture can never be
 * mistaken for a real condition id), all 159 fail the hex test, and every deep link the product makes —
 * `DossierView` links `/market/<marketId>`, the terminal links it, the buy-flow spec visits it — landed on the
 * public-slug branch and 404'd. A dispatch predicate that cannot be satisfied by the data it dispatches is a
 * 404 with a better excuse.
 *
 * So the property is the one that actually distinguishes the two pages: an id begins with `0x`, and a slug is
 * lowercase words. Alphanumeric rather than hexadecimal, because the discriminator is the prefix, and the
 * stricter test bought nothing except a route that never matched.
 *
 * It lives in its own module because the page is a server component and a client boundary rule applies to what
 * a *client* module exports — and, more to the point, because this is a rule with a right answer, so it is a
 * rule a test can state without rendering anything (`market-dispatch.test.ts`).
 */
export function isMarketId(segment: string): boolean {
  return /^0x[0-9a-zA-Z]{1,127}$/.test(segment);
}
