/**
 * A session, for the browser suite.
 *
 * This file exists because two specs in this directory had never actually reached the screens they describe.
 * `buy-flow` and `wallet-ceremony` stub the API at the browser's network boundary — which is the right way to
 * test a client — but the pages they visit are behind `app/(app)/layout.tsx`, whose `serverAuth()` decides
 * **on the server, from the `pgm_at` cookie** and redirects to `/sign-in` when there is none. A browser route
 * intercept cannot influence that decision, so both specs were asserting against the sign-in page and failing on
 * the first `toBeVisible()` that named a trade ticket.
 *
 * That is invisible in CI's gate: the phase gates check that these files exist and that a workflow runs them, and
 * the browser never ran on the build box. The first local run of the suite is what surfaced it.
 *
 * The cookie format is `"<expiryEpochMs>:<token>"` (`splitAccess` in `src/auth/refresh.ts`), and the expiry is
 * far in the future so nothing rotates mid-test. No token is real; nothing here reaches a real API, because every
 * spec in this directory stubs the wire.
 */
import type { Page } from "@playwright/test";

export const ACCESS_COOKIE = "9999999999999:e2e-session";

export async function signIn(page: Page, opts: { theme?: "dark" | "light" } = {}): Promise<void> {
  const cookies = [
    { name: "pgm_at", value: ACCESS_COOKIE, domain: "127.0.0.1", path: "/" },
    { name: "pgm_rt", value: "e2e-refresh", domain: "127.0.0.1", path: "/" },
    ...(opts.theme ? [{ name: "pgm_theme", value: opts.theme, domain: "127.0.0.1", path: "/" }] : []),
  ];
  await page.context().addCookies(cookies);
}
