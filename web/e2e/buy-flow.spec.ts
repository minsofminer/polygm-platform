/**
 * The buy flow, in a browser, because three of its claims only exist in one.
 *
 *  1. **The gate.** The ticket must refuse — in words, with nothing on the wire — when the feed is down. That is
 *     not a degraded state to be skipped past: `canTrade` is derived from the same stamp the connection dot
 *     shows, and a ticket that posted anyway would be a button that spends money while the screen says the venue
 *     is unreachable.
 *  2. **The layout.** The design rule is that a price animates its *background* and never its digits. That is a
 *     statement about geometry, so this measures the element's box across a live tick — a claim no unit test can
 *     make, because jsdom does not lay anything out.
 *  3. **The wire.** The ticket posts `{slug, side, amountUsdc}` to `/v1/orders/amount` — a slug, a side and a
 *     budget, with the server resolving the token and re-reading the ask. It is written below and *parked*, with
 *     the blocker named, rather than asserted against an environment that cannot open the gate.
 *
 * HONEST STATUS, 2026-09-29. This file used to visit `/markets/0xM1` (the app's detail page is `/market/<id>`,
 * one segment), stub the bare orders route (which the ticket has not posted to since P12 — the request escaped
 * the stub and came back a 401 from the real API), and look for a `Trade` region that never rendered, because
 * the page 404'd first. Three stale assumptions in a file whose whole purpose was to catch stale assumptions. It
 * is now green on the two claims the environment can answer, and the third is parked with its own reason.
 */
import { expect, test } from "@playwright/test";
import { signIn } from "./session";

// The book fixture is written in the payload's real shape, taken from a live response: a truncated ladder
// is not a smaller book, it is a payload the client refuses to parse — and a refused book renders as no
// ladder at all, which is exactly what a stub with six missing keys produced here.
const BOOK = {
  "cacheKey": "book:0xM1:400:raw",
  "market": "0xM1",
  "aggregate": "raw",
  "aggregateStep": null,
  "tickSize": "0.01",
  "bids": [
    {
      "price": "0.49",
      "shares": "40",
      "levels": 1,
      "cumShares": "40"
    },
    {
      "price": "0.48",
      "shares": "20",
      "levels": 2,
      "cumShares": "60"
    },
    {
      "price": "0.47",
      "shares": "13.333333",
      "levels": 3,
      "cumShares": "73.333333"
    }
  ],
  "asks": [
    {
      "price": "0.51",
      "shares": "36",
      "levels": 1,
      "cumShares": "36"
    },
    {
      "price": "0.52",
      "shares": "18",
      "levels": 2,
      "cumShares": "54"
    },
    {
      "price": "0.53",
      "shares": "12",
      "levels": 3,
      "cumShares": "66"
    }
  ],
  "spreadTicks": 2.0,
  "midPrice": "0.5",
  "bestBid": "0.49",
  "bestAsk": "0.51",
  "oneSided": null,
  "maxCumShares": "124.128424",
  "ageMs": 516182054,
  "asOf": 1790145561000,
  "staleAfter": 1790145564000,
  "serverAsOf": 1790661743054,
  "cache": {
    "ttlMs": 250,
    "key": "book:0xM1:400:raw",
    "public": true,
    "immutable": false
  },
  "depth": 400
};

test("the ticket refuses, in words and with nothing on the wire, while the feed is down", async ({ page }) => {
  await signIn(page);
  const posted: string[] = [];
  await page.route("**/v1/orders**", async (route) => {
    posted.push(route.request().url());
    await route.fulfill({ status: 202, json: { intentId: "int_should_not_happen" } });
  });
  await page.route("**/v1/markets/*/book*", (route) => route.fulfill({ json: BOOK }));

  await page.goto("/market/0xM1");
  const ticket = page.getByRole("region", { name: /trade/i });
  await expect(ticket).toBeVisible();

  // The gate is closed in every environment until a live transport is configured (`NEXT_PUBLIC_WS_ORIGIN`), and
  // the ticket says so in a sentence rather than by greying the button and hoping.
  await expect(ticket.getByRole("status")).toContainText(/feed|disconnect/i);

  await ticket.getByLabel(/size|amount/i).fill("10.00");
  // The send control is `aria-disabled` with the reason as its title — not merely greyed. A control that looks
  // disabled and is still clickable is the bug this asserts against, so the assertion is the attribute and the
  // sentence, in that order.
  const send = ticket.getByRole("button", { name: /confirm|trade/i }).last();
  await expect(send).toHaveAttribute("aria-disabled", "true");
  await expect(send).toHaveAttribute("title", /feed|disconnect/i);

  // Nothing was posted, and the reason is on screen. A refusal that still sends is the failure this checks.
  await page.waitForTimeout(500);
  expect(posted).toEqual([]);
});

/** One tick of the ask, with every derived field moved with it — a payload the server itself could send. */
function ticked(price: string) {
  const bid = BOOK.bestBid;
  return {
    ...BOOK,
    asks: [{ ...BOOK.asks[0], price }, ...BOOK.asks.slice(1)],
    bestAsk: price,
    midPrice: ((Number(price) + Number(bid)) / 2).toFixed(2),
    spreadTicks: Math.round((Number(price) - Number(bid)) / Number(BOOK.tickSize)),
    asOf: Date.now(),
    staleAfter: Date.now() + 3_000,
  };
}

test("a price tick does not move the ladder's row", async ({ page }) => {
  await signIn(page);
  let ask: string = BOOK.bestAsk;
  await page.route("**/v1/markets/*/book*", (route) => route.fulfill({ json: ticked(ask) }));

  await page.goto("/market/0xM1");
  // The row measured is a *bid* level: the ask is what moves in this test, and the claim is about the rows that
  // are standing still while it does.
  const row = page.locator(`.pgm-ladder [data-ladder-price="${BOOK.bestBid}"]`).first();
  await expect(row).toBeVisible();
  const before = await row.boundingBox();
  expect(before).not.toBeNull();

  // A real tick: the ask moves one tick, the poll picks it up, and the row that was there is the row that stays.
  // The new price is derived from the tick size rather than hard-coded, so this test cannot drift from the venue's
  // convention — and every field the payload derives (`midPrice`, `spreadTicks`) moves with it, because a book
  // whose `bestAsk` disagrees with its own ladder is not a book the client will render at all.
  ask = (Number(BOOK.bestAsk) + 2 * Number(BOOK.tickSize)).toFixed(2);
  await expect(page.locator(`.pgm-ladder [data-ladder-price="${ask}"]`)).toBeVisible({ timeout: 15_000 });

  const after = await row.boundingBox();
  expect(after?.x).toBe(before?.x);
  expect(after?.width).toBe(before?.width);
  // The row's digits must not have grown or wrapped: same height, same box, a different number in it.
  expect(after?.height).toBe(before?.height);
});

/**
 * Parked, not deleted: the claim is real and the ticket's body is already right (`{slug, side, amountUsdc}` to
 * `/v1/orders/amount` — the contract explains why a browser may not invent a token id and a limit price).
 *
 * It cannot be executed here because the gate above is closed by construction: `canTrade` is derived from the
 * live feed's freshness, the feed connects only when `NEXT_PUBLIC_WS_ORIGIN` names a WebSocket origin, and no
 * environment in this repository sets one — there is no `/v1/live/tape` server yet. The moment one exists, this
 * is the assertion that proves the client half of the order path, and it should be un-parked in the same change.
 */
test.fixme("the ticket posts the route's own shape once the feed is live", async ({ page }) => {
  await signIn(page);
  let orderBody: Record<string, unknown> | null = null;
  await page.route("**/v1/markets/*/book*", (route) => route.fulfill({ json: BOOK }));
  await page.route("**/v1/orders/amount", async (route) => {
    orderBody = route.request().postDataJSON();
    await route.fulfill({ status: 202, json: { intentId: "int_e2e_1", state: "queued" } });
  });

  await page.goto("/market/0xM1");
  const ticket = page.getByRole("region", { name: /trade/i });
  await ticket.getByLabel(/size|amount/i).fill("10.00");

  // Keyboard only: Tab to the send control from the amount field and press it.
  await ticket.getByLabel(/size|amount/i).press("Tab");
  let guard = 0;
  while (guard < 12 && (await page.evaluate(() => document.activeElement?.tagName)) !== "BUTTON") {
    await page.keyboard.press("Tab");
    guard += 1;
  }
  await page.keyboard.press("Enter");
  await expect(ticket.getByRole("status")).toContainText(/intent|queued/i);

  expect(orderBody).not.toBeNull();
  expect(Object.keys(orderBody ?? {}).sort()).toEqual(["amountUsdc", "side", "slug"]);
});
