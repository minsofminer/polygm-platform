/**
 * Which upstream reads the proxy serves without a session.
 *
 * A deep link into a plain browser has no Telegram `initData` and never will, so every read the Mini App's card
 * needs must be one the API itself serves to nobody in particular. Those reads are declared with `anonymous: true`
 * on the route ledger (`src/api/routes.ts`) and matched here, so the rule has one home: a route cannot be public in
 * the declaration and session-gated in the proxy, and a route nobody declared cannot become anonymous by accident.
 *
 * The path is matched against the ledger's *template*, never a prefix. The tempting shortcut — "anything under
 * `/v1/public/` is public" — is wrong in the one place it would matter most: `/v1/public/blocks` is a read that is
 * also a write, and `x-auth: admin`.
 *
 * This module is deliberately free of `next/headers` and of `server-only`: it is arithmetic on a path and the
 * ledger, and it is worth a test that needs no request. `src/auth/server.ts` is the only caller that matters.
 */
import { ROUTES, type RouteDecl } from "@/api/routes";

/** Is this upstream path one the contract serves to nobody in particular? */
export function isAnonymousRead(method: string, path: string): boolean {
  // Stripped of its query string (`the template has none`) and of a trailing slash, because both spellings reach
  // the same upstream route and a read that works at one URL and 401s at another is the bug this fixes.
  const target = (path.split("?")[0] ?? "").replace(/\/+$/, "") || "/";
  // `satisfies` on the ledger keeps each row's literal type, which drops the optional flag from the inferred
  // union; the cast is the type the declaration promised.
  for (const decl of Object.values(ROUTES) as readonly RouteDecl[]) {
    if (decl.anonymous !== true) continue;
    if (decl.method.toUpperCase() !== method.toUpperCase()) continue;
    const template = decl.path.replace(/\{([a-z_]+)\}/g, "[^/]+").replace(/\/+$/, "") || "/";
    // `{slug}` is one segment: a deeper path is a different route and inherits nothing from its ancestor.
    if (new RegExp(`^${template}$`).test(target)) return true;
  }
  return false;
}
