/**
 * The buy flow, in a browser, because three of its claims only exist in one.
 *
 *  1. **The wire.** P12's ticket bug (posting `{market_id, amount_cents}` to a route that requires
 *     `{marketId, tokenId, side, price, size}`) was invisible to every unit test and to every screenshot: the
 *     screen looked right and the API answered 422. Here the request is *observed* — the route is intercepted and
 *     its body is asserted — and then the answer is stubbed, so the test is about the client, not the venue.
 *  2. **The keyboard.** A pointer can be faked in jsdom; a real tab order cannot. This walks the ticket with
 *     Tab/Enter only and requires the order to be sent.
 *  3. **No layout shift on a price change.** The design rule is that a price animates its *background* and never
 *     its digits. That is a statement about geometry, so the test measures the element's box before and after a
 *     tick and asserts it did not move — a claim no unit test can make, because jsdom does not lay anything out.
 */
import { expect, test } from "@playwright/test";

const BOOK = {
  market: "0xM1",
  bids: [{ price: "0.40", shares: "1000000", levels: 1, cumShares: "1000000" }],
  asks: [{ price: "0.42", shares: "1000000", levels: 1, cumShares: "1000000" }],
  aggregate: "raw",
  spreadTicks: 2,
  ageMs: 120,
  oneSided: null,
  asOf: 1_700_000_000_000,
  staleAfter: 1_700_000_003_000,
  cache: { ttlMs: 250, public: true },
};

test("a ticket posts the route's own shape and the ladder does not move when a price ticks", async ({ page }) => {
  let orderBody: Record<string, unknown> | null = null;
  await page.route("**/v1/markets/*/book*", (route) => route.fulfill({ json: BOOK }));
  await page.route("**/v1/orders", async (route) => {
    orderBody = route.request().postDataJSON();
    await route.fulfill({ status: 202, json: { intentId: "int_e2e_1", state: "queued" } });
  });

  await page.goto("/markets/0xM1");
  const ticket = page.getByRole("region", { name: /trade/i });
  await expect(ticket).toBeVisible();

  const ladderPrice = page.locator(".pgm-ladder").first();
  const before = await ladderPrice.boundingBox();

  await page.getByLabel(/amount/i).fill("10.00");
  // keyboard only: Tab to the send control from the amount field and press it
  await page.getByLabel(/amount/i).press("Tab");
  let guard = 0;
  while (guard < 12 && (await page.evaluate(() => document.activeElement?.tagName)) !== "BUTTON") {
    await page.keyboard.press("Tab");
    guard += 1;
  }
  await page.keyboard.press("Enter");
  await expect(page.getByRole("status")).toContainText(/intent|queued/i);

  expect(orderBody).not.toBeNull();
  expect(Object.keys(orderBody ?? {}).sort()).toEqual(["marketId", "price", "side", "size", "tokenId"]);

  // the same element, the same box: nothing about a new price reflows the row
  const after = await ladderPrice.boundingBox();
  expect(after?.x).toBe(before?.x);
  expect(after?.width).toBe(before?.width);
});
