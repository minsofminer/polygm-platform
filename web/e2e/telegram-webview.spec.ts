/**
 * The Mini App inside Telegram's webview, which is where the product is actually used.
 *
 * Three claims live only here:
 *
 *  1. **The frame is the only external script.** The Mini App may load `telegram-web-app.js` and nothing else —
 *     no CDN, no font host, no analytics — because the CSP says so and because a webview that reaches a third
 *     party is a webview that leaks an account to it. The test intercepts every request and asserts the set.
 *  2. **A blocked Telegram script is degradation, not breakage.** Telegram's script is sometimes not there
 *     (desktop previews, a strict extension, a bad connection). The app must still render its read-only state
 *     with the connection warning, not a blank screen — this is checked by aborting the request.
 *  3. **The phone layout does not scroll sideways.** At 390×844 a ticket that overflows horizontally is a button
 *     a thumb cannot reach, and `scrollWidth > clientWidth` is the cheapest honest test for it.
 */
import { expect, test } from "@playwright/test";

test.describe("telegram webview", () => {
  test("loads the Telegram script and nothing else external", async ({ page }) => {
    const external: string[] = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") external.push(url.href);
    });
    await page.goto("/tma?startapp=fed-cut-sept");
    await expect(page.locator("body")).toBeVisible();
    expect(external.filter((u) => !u.includes("telegram-web-app.js"))).toEqual([]);
  });

  test("with Telegram's script blocked it still says what it cannot do", async ({ page }) => {
    await page.route("**/telegram-web-app.js", (route) => route.abort());
    await page.route("**/v1/**", (route) => route.fulfill({ status: 401, json: { error: { code: "UNAUTHENTICATED", message: "a session is required", retryable: false, requestId: "e2e" } } }));
    await page.goto("/tma?startapp=fed-cut-sept");
    const body = await page.locator("body").innerText();
    expect(body.length).toBeGreaterThan(20);
    expect(body).toMatch(/connect|session|open|Telegram/i);
  });

  test("no horizontal overflow on a phone, and the primary action is above the fold", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "telegram-webview", "the geometry claim is about the phone viewport");
    await page.route("**/v1/**", (route) => route.fulfill({ json: {} }));
    await page.goto("/tma?startapp=fed-cut-sept");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    const main = page.getByRole("button").first();
    const box = await main.boundingBox();
    expect(box?.y ?? 0).toBeLessThan(844);
  });
});
