/**
 * The browser verification of `plans/design-review.md`, `plans/animation-audit.md` and the motion pass.
 *
 * Every claim here was previously asserted from source: the keyboard handler existed in `Shell.tsx`, the CSS
 * declared `touch-action: none`, the stylesheet carried `@keyframes pgm-panel-in`. Those are claims about *what
 * the code says*. This file makes them claims about **what a browser does**, which is a different and stronger
 * thing — and it exists because the P13 note that a browser cannot run on this box turned out to be false for the
 * current environment (see the correction in `playwright.config.ts`).
 *
 * How it reaches the shell without an API: `serverAuth()` reads the `pgm_at` **cookie** and decides, server-side,
 * with no network call, so a session cookie is enough to render the real `(app)` shell — see `./session`. Page
 * *data* then fails its reads and the content area shows its own unavailable state, which is what this spec wants:
 * the shell is the subject.
 */
import { expect, type Page } from "@playwright/test";
import { test } from "@playwright/test";
import { signIn } from "./session";


/**
 * The exact stored fraction, not the announced percentage. `aria-valuenow` is rounded to whole percents and the
 * CSS variable is rounded too — so a one-press step of 0.5% is invisible to both, and the first version of this
 * spec compared rounded numbers and called a working control broken. `pgm.rails.v1` is what the pointer path
 * persists, so asserting against it is also asserting that the keyboard writes the same store the drag does.
 */
const rails = (page: Page) =>
  page.evaluate(() => JSON.parse(localStorage.getItem("pgm.rails.v1") ?? "{}") as { left?: number; right?: number });

/**
 * The stored fraction, with the hook's own default when nothing has been changed yet.
 *
 * The store is written on the *first change*, not on mount — a preference nobody set is not a preference worth
 * persisting — so reading it before any key press returns nothing at all. The defaults are `DEFAULTS` in
 * `src/shell/rails.ts`; naming them here is the only way to assert "one press moved it *from* where it starts".
 */
const leftFraction = async (page: Page) => (await rails(page)).left ?? 0.18;
const rightFraction = async (page: Page) => (await rails(page)).right ?? 0.22;

test.describe("the shell the review fixed", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  /**
   * `/portfolio` rather than `/markets`: `/markets` is a **public** route and renders outside the shell, so every
   * assertion here was looking for rails on a page that has none. The first run of this spec failed 11 of 13
   * cases for that reason alone, which is the sort of thing only a browser tells you.
   */

  test("the rail handles resize from the keyboard, and announce their values", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "a resize handle is a desktop control");
    await page.goto("/portfolio");

    const left = page.getByRole("separator", { name: /rail/i }).first();
    await expect(left).toBeVisible();

    // The values a focusable separator owes — the half the first draft of the fix forgot.
    await expect(left).toHaveAttribute("aria-valuemin", "10");
    await expect(left).toHaveAttribute("aria-valuemax", "34");
    await expect(left).toHaveAttribute("aria-valuetext", /arrow keys resize/i);

    await left.focus();
    await expect(left).toBeFocused();

    const start = await leftFraction(page);
    expect(start).toBeCloseTo(0.18, 5);

    // One press is the terminal's own step: 0.005 of the viewport.
    await page.keyboard.press("ArrowRight");
    expect((await leftFraction(page)) - start).toBeCloseTo(0.005, 5);

    // Shift is the coarse step: five times as far, which is what "coarse" means in the terminal layout too.
    const beforeCoarse = await leftFraction(page);
    await page.keyboard.press("Shift+ArrowRight");
    expect((await leftFraction(page)) - beforeCoarse).toBeCloseTo(0.025, 5);

    // Home goes to the floor and End to the ceiling — the two keys the WAI-ARIA splitter pattern names — and the
    // announced value follows the same number the store holds.
    await page.keyboard.press("Home");
    expect(await leftFraction(page)).toBeCloseTo(0.1, 5);
    await expect(left).toHaveAttribute("aria-valuenow", "10");
    await page.keyboard.press("End");
    expect(await leftFraction(page)).toBeCloseTo(0.34, 5);
    await expect(left).toHaveAttribute("aria-valuenow", "34");

    // …and it is a real resize: the frame's own variable moved with it.
    const cssVar = await page.evaluate(() =>
      getComputedStyle(document.querySelector(".frame")!).getPropertyValue("--pgm-rail-left").trim(),
    );
    // The frame's variable is a grid *track* (`minmax(40px, 34fr)`), not a bare percentage — the rail is sized
    // by the grid it participates in, which is why the number is a share of the viewport rather than a width.
    expect(cssVar).toContain("34fr");

    // Left arrow walks it back.
    await page.keyboard.press("ArrowLeft");
    expect(await leftFraction(page)).toBeLessThan(0.34);
  });

  test("the right rail's keyboard delta is inverted", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "a resize handle is a desktop control");
    await page.goto("/portfolio");
    const right = page.getByRole("separator", { name: /rail/i }).nth(1);
    await right.focus();
    await page.keyboard.press("ArrowRight");
    // A separator that moves right takes width from the panel on its right: the RIGHT rail shrinks.
    expect(await rightFraction(page)).toBeLessThan(0.22);
    const shrunk = await rightFraction(page);
    await page.keyboard.press("ArrowLeft");
    expect(await rightFraction(page)).toBeCloseTo(shrunk + 0.005, 5);
  });

  test("Enter collapses the rail from the handle — the same action as the rail's own button", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "a resize handle is a desktop control");
    await page.goto("/portfolio");
    const left = page.getByRole("separator", { name: /rail/i }).first();
    await left.focus();
    const rail = page.locator("aside.rail").first();
    await expect(rail).not.toHaveClass(/rail--collapsed/);
    await page.keyboard.press("Enter");
    await expect(rail).toHaveClass(/rail--collapsed/);
  });

  test("a finger drag is not a scroll: the handle claims the gesture", async ({ page }) => {
    await page.goto("/portfolio");
    const touchAction = await page.locator(".handle").first().evaluate((el) => getComputedStyle(el).touchAction);
    // Without this the browser claims a touch drag, sends `pointercancel`, and the rail cannot be resized by
    // touch at all — the finding, asserted as a browser fact rather than as a declaration in a stylesheet.
    expect(touchAction).toBe("none");
  });

  test("the skip link is the first stop, is visible when focused, and lands inside the content", async ({ page }) => {
    await page.goto("/portfolio");
    await page.keyboard.press("Tab");
    const skip = page.locator("a.skip");
    await expect(skip).toBeFocused();
    await expect(skip).toHaveText(/skip to content/i);

    // Visible when focused: it arrives from off-screen at the top-left. A skip link nobody can see is a skip
    // link nobody uses.
    const box = await skip.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.y).toBeGreaterThanOrEqual(0);
    expect(box!.height).toBeGreaterThan(0);

    await page.keyboard.press("Enter");
    const landed = await page.evaluate(() => {
      const active = document.activeElement;
      const main = document.getElementById("content");
      return { activeId: active?.id ?? "", inMain: Boolean(main && active && (active === main || main.contains(active))) };
    });
    expect(landed.inMain || landed.activeId === "content").toBe(true);
  });

  test("color-scheme follows the theme, so the browser's own furniture matches the page", async ({ page }) => {
    await page.goto("/portfolio");
    const dark = await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme);
    expect(dark).toBe("dark");

    await page.context().addCookies([{ name: "pgm_theme", value: "light", domain: "127.0.0.1", path: "/" }]);
    await page.goto("/portfolio");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    const light = await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme);
    expect(light).toBe("light");
  });
});

test.describe("what the browser run found: no API, and the page still has to be a page", () => {
  /**
   * Both of these failed before this pass, and neither was reachable from a unit test.
   *
   * This suite runs the production build with **no backend behind it** (`PGM_API_ORIGIN` points at a closed
   * port), which is not a contrived state: it is what every user sees during an API deploy, a restart, or a
   * network blip. The first case is the server half, the second the browser half.
   */
  test("an authenticated page renders the shell, not a 500, when the API is unreachable", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "one project is enough for a server-rendering claim");
    await signIn(page);
    const response = await page.goto("/portfolio");
    // Before the fix: HTTP 500 and Next's own error document. `callUpstream` awaited a fetch that rejected and
    // let the rejection take the whole render with it.
    expect(response?.status()).toBe(200);
    await expect(page.locator(".frame")).toBeVisible();
    await expect(page.getByRole("separator", { name: /rail/i }).first()).toBeVisible();
    // …and the page says what it cannot do, in the product's own words, rather than showing an empty market list.
    const body = await page.locator("body").innerText();
    expect(body).toMatch(/unavailable|try again|could not|not available/i);
  });

  test("signing in while the network is down clears the busy state and says so", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "one project is enough for a client-fetch claim");
    await page.route("**/api/session/login", (route) => route.abort());
    await page.goto("/sign-in");
    await page.getByLabel(/email or handle/i).fill("someone@example.com");
    await page.locator('input[type="password"]').fill("hunter2hunter2");
    await page.getByRole("button", { name: /sign in/i }).first().click();

    // Before the fix the rejected fetch escaped the handler: no `setBusy(false)`, no message — a button that
    // spins for ever. The message is also not allowed to be the wrong-password sentence.
    await expect(page.getByText(/did not reach the server/i)).toBeVisible();
    await expect(page.getByRole("button", { name: /sign in/i }).first()).toBeEnabled();
  });
});

test.describe("the dialog's motion, in a browser with real animations", () => {
  test("the panel enters, and closing plays the exit before the element is removed", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "the reduced-motion project is a separate test below");
    await signIn(page);
    await page.goto("/portfolio");

    await page.getByRole("button", { name: /search markets|command|⌘|ctrl/i }).first().click();
    const panel = page.locator(".overlay__panel");
    await expect(panel).toBeVisible();

    // The entrance is the design system's own animation, not a hand-rolled transition.
    const entering = await panel.evaluate((el) => getComputedStyle(el).animationName);
    expect(entering).toContain("pgm-panel-in");

    // Closing is two steps: the exit plays…
    await page.keyboard.press("Escape");
    const leaving = await page.evaluate(() => {
      const el = document.querySelector(".overlay__panel");
      return el ? getComputedStyle(el).animationName : null;
    });
    expect(leaving).toContain("pgm-panel-out");

    // …and the element is removed only once the animation has ended, which is the bug the fix removed:
    // before it, the panel vanished between two frames.
    await expect(page.locator(".overlay__panel")).toHaveCount(0, { timeout: 3_000 });
  });

  test("under reduced motion the exit is skipped, so the dialog still closes", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "telegram-webview", "this project is configured with reducedMotion: reduce");
    await signIn(page);
    await page.goto("/portfolio");

    // The global reduced-motion block sets `animation: none !important`. An exit that waited for an
    // `animationend` that can never fire would be a dialog nobody could close — so this is the case that must
    // not regress.
    const animationsOff = await page.evaluate(() => {
      const probe = document.createElement("div");
      probe.style.animation = "pgm-panel-in 200ms";
      document.body.appendChild(probe);
      const name = getComputedStyle(probe).animationName;
      probe.remove();
      return name;
    });
    expect(animationsOff).toBe("none");

    await page.getByRole("button", { name: /search markets|command|⌘|ctrl/i }).first().click();
    await expect(page.locator(".overlay__panel")).toBeVisible();
    await page.keyboard.press("Escape");
    // Immediately, not after a 200ms animation.
    await expect(page.locator(".overlay__panel")).toHaveCount(0, { timeout: 1_000 });
  });
});
