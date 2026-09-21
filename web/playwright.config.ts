/**
 * P13 D4 — the browser suite: three flows a unit test cannot reach (a real navigation, real focus, a real
 * webview profile) plus the layout-shift measurement, which needs layout to exist at all.
 *
 * HONEST STATUS, and it belongs in the config rather than only in a doc: these specs do **not** run on the build
 * box. Playwright's chromium needs system libraries (`libxkbcommon0`, `libasound2t64`, `libnss3`, …) and this
 * sandbox runs as a non-root user with no way to install them, so the browser downloads and the specs execute on
 * the nightly runner instead (`npm run test:e2e:install` in `.github/workflows/nightly.yml`). What the phase gate
 * checks locally is that these files exist, that they cover the three flows the kit names, and that a workflow
 * runs them — a claim about the suite, not a claim that it passed here.
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
      name: "telegram-webview",
      use: {
        ...devices["Pixel 7"],
        hasTouch: true,
        viewport: { width: 390, height: 844 },
        userAgent:
          "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Mobile Safari/537.36 Telegram-Android/11.2.0",
        reducedMotion: "reduce",
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
