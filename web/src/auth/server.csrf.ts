/**
 * The CSRF / context decision, in its own module so it can be unit-tested without `next/headers` and without
 * a process environment. `src/auth/server.ts` supplies the allowlist; this file is arithmetic on headers the
 * browser sent, which is exactly the kind of rule worth testing.
 */
export type GuardInput = {
  method: string;
  origin: string | null;
  secFetchSite: string | null;
  secFetchDest: string | null;
  /** The deployment's own origins, from config. An empty list means "nobody has said yet". */
  allowedOrigins?: readonly string[];
};

export function csrfOk(init: GuardInput): { allowed: boolean; crossSite: boolean } {
  const method = init.method.toUpperCase();
  const crossSite = init.secFetchSite === "cross-site";
  const mutating = method !== "GET" && method !== "HEAD";
  if (!mutating) return { allowed: true, crossSite };
  if (!init.origin) return { allowed: false, crossSite };
  let url: URL;
  try {
    url = new URL(init.origin);
  } catch {
    return { allowed: false, crossSite };
  }
  const allowlist = init.allowedOrigins ?? [];
  if (allowlist.length && !allowlist.includes(url.origin)) return { allowed: false, crossSite };
  // The rule, in order of trustworthiness: the browser's own classification wins when it has one, because a
  // cross-site page cannot fake `Sec-Fetch-Site`. With no header at all — curl, a proxy that strips it — the
  // allowlist is the only thing standing between a foreign Origin and a mutation, so an *unconfigured*
  // allowlist refuses rather than assumes.
  if (init.secFetchSite === "same-origin") return { allowed: true, crossSite };
  // `none` (a top-level navigation or a new-tab form post) and `same-site` (a proxy on another host of the
  // same registrable domain) are the two cases where the browser could not or did not say "same-origin".
  // They are not evidence of an attack, so the decision falls to the one thing that is unforgeable here: the
  // Origin header on a mutating request, which line 28 already required to be on the *configured* list. An
  // unconfigured list still refuses — "nobody said" is never "everybody".
  if (init.secFetchSite === null || init.secFetchSite === "none" || init.secFetchSite === "same-site") {
    return { allowed: allowlist.length > 0, crossSite };
  }
  // `cross-site`, with or without an allowlist: a foreign page has no business mutating our session.
  return { allowed: false, crossSite };
}
