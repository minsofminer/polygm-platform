/**
 * The one place the surface is read from the environment. The `.server.` suffix is load-bearing: `assert-env.mjs`
 * allows a module of that shape to read a non-`NEXT_PUBLIC_` variable, because a value that decides *what this
 * deployment is* must never be inlined into a client bundle by a prefix.
 *
 * `PGM_SURFACE=miniapp` is set on the Mini App's Vercel project and nowhere else. It is read at render time by the
 * root layout and the root page, which is why it has to be present in the *build* environment too: a statically
 * generated `/` that was rendered without the flag would ship the landing page under the bot's URL.
 */
export function isMiniAppSurface(): boolean {
  return (process.env.PGM_SURFACE ?? "").trim().toLowerCase() === "miniapp";
}

/** The site to point a lost visitor at, from the Mini App deployment's 404. */
export function mainSiteUrl(): string {
  return (process.env.NEXT_PUBLIC_SITE_ORIGIN ?? "https://openout.app").replace(/\/+$/, "");
}
