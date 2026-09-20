/**
 * Which paths this *deployment* serves, and the rule that decides it.
 *
 * The Mini App is deployed twice on purpose, and the reason is not tidiness:
 *
 *  - **The main site** serves everything: the terminal, the public pages, and `/tma` for anyone who opens a link
 *    outside Telegram. Its CSP frames nothing except the Mini App subtree.
 *  - **The Mini App's own domain** (`PGM_SURFACE=miniapp`) serves the trade screen at `/` — the URL registered with
 *    BotFather has to be the thing a customer lands on — and refuses everything else. Not "does not link to": refuses,
 *    with a 404. A Mini App domain that also answers `/sign-in`, `/admin` and `/terminal` is a second copy of the
 *    product with a second attack surface and a second indexable domain, and "which URL is the product" stops having
 *    an answer.
 *
 * The predicate lives here, away from the environment read (`surface.server.ts`) and away from the middleware, so it
 * is a pure function a test can walk: the list of what stays is the security decision, and a security decision that
 * cannot be unit-tested is a security decision that changes by accident.
 */

/** Paths the Mini App deployment answers. `/` is the registered Mini App URL and is always allowed. */
export const MINI_APP_PATHS: readonly string[] = [
  "/",
  "/tma",              // the canonical route, so a link that names it works on both deployments
  "/trade",            // the BotFather short-name form (`t.me/<bot>/trade`), kept as an alias
  "/api",              // the session proxy and every read/write the screen makes
  "/_next",            // the framework's own assets; without these the screen renders unstyled
  "/favicon.ico",
  "/icon.svg",
  "/apple-icon.png",
  "/manifest.webmanifest",
  "/robots.txt",
];

export const MAIN_SITE_URL = "https://openout.app";

/**
 * Is this path part of the Mini App surface?
 *
 * Prefix matching is on whole segments (`/api` matches `/api/...`, not `/apis`), because the failure it prevents is
 * the one where an unrelated route starts with an allowlisted string and quietly ships with the Mini App's CSP.
 */
export function surfaceAllows(pathname: string, paths: readonly string[] = MINI_APP_PATHS): boolean {
  const clean = (pathname.split("?")[0] ?? "/").replace(/\/+$/, "") || "/";
  return paths.some((allowed) => clean === allowed || (allowed !== "/" && clean.startsWith(allowed + "/")));
}
