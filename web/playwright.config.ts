/**
 * P13 D4 — the browser suite: three flows a unit test cannot reach (a real navigation, real focus, a real
 * webview profile) plus the layout-shift measurement, which needs layout to exist at all.
 *
 * HONEST STATUS, corrected on 2026-09-27 by running it: the paragraph that stood here said the browser could not
 * run on this box at all — no system libraries and no way to install them. That was wrong for the current
 * environment, and `plans/browser-verification.md` records the run that falsified it: `npx playwright install
 * chromium` plus `sudo npx playwright install-deps chromium`, then the whole suite against `next start` on a
 * local port. What it found on the first run was worth the correction — four real defects, three of them in
 * product code, none reachable from a unit test (`plans/design-review.md`, `plans/browser-verification.md`).
 *
 * The nightly runner still runs this suite (`npm run test:e2e:install` in `.github/workflows/nightly.yml`), and
 * the phase gates still only check that the files exist, that they cover the flows the kit names, and that a
 * workflow runs them. The difference now is that "it passes" is a claim this repository can check locally.
 *
 * `PGM_E2E_BASE_URL` points the suite at a running deployment (the Mini App's Vercel URL, or `next start` in CI).
 * When it is unset the suite starts `next dev` itself, which is what a developer wants locally.
 */
import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.PGM_E2E_BASE_URL ?? "http://127.0.0.1:3100";
const external = Boolean(process.env.PGM_E2E_BASE_URL);

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : 1,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL,
    trace: "retain-on-failure",
    video: "off",
    screenshot: "only-on-failure",
    locale: "en-GB",
    timezoneId: "UTC",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    {
      // The Mini App's real frame: Telegram's webview on a phone. The viewport and the touch flag matter (a
      // 390x844 layout is where the ticket's buttons collide), and so does the reduced-motion preference — the
      // design system honours it and a screen that only animates is a screen that is blank here.
      //
      // The device descriptor is projected rather than spread-and-patched. Spreading `devices["Pixel 7"]` into
      // `use` and then adding `reducedMotion` type-checks as a mismatch in the test-options overload (the
      // descriptor's `defaultBrowserType` and friends make the object no longer a plain `use`), and the fix
      // that keeps `npm run typecheck` honest is to name the fields this project wants.
      name: "telegram-webview",
      use: {
        ...devices["Pixel 7"],
        viewport: { width: 390, height: 844 },
        hasTouch: true,
        isMobile: true,
        userAgent:
          "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Mobile Safari/537.36 Telegram-Android/11.2.0",
        contextOptions: { reducedMotion: "reduce" },
      },
    },
  ],
  ...(external
    ? {}
    : {
        webServer: {
          command: "npx next dev -p 3100",
          url: baseURL,
          reuseExistingServer: !process.env.CI,
          timeout: 180_000,
        },
      }),
});
