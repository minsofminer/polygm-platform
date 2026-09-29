/**
 * The site's own origin, and the two rewrites that stop the API's host from leaking into pages we publish.
 *
 * Public payloads carry absolute URLs — `page.url` (the canonical), the OG url, the two share links and every
 * `item` in the structured-data graph. Those URLs are built by the API, and the API builds them from
 * `PGM_PUBLIC_BASE`, falling back to the host it was itself asked on. On a deployment where that variable is
 * unset (the API answers on `polygm-api.vercel.app`) every one of those URLs named the *API*, so a crawler was
 * told that the canonical home of `/market/mayor-2027` is a JSON endpoint: the site's own pages were declared
 * duplicates of the API. Two consequences, both silent — nothing 404s, nothing errors, and the site simply does
 * not index. The sitemap made it worse by publishing 143 URLs under that host, all of which answer JSON.
 *
 * The fix belongs here rather than in the API's config for one reason: the origin of a page is a property of the
 * site that serves it, and this is the site. `NEXT_PUBLIC_SITE_ORIGIN` (the same variable `app/layout.tsx` gives
 * `metadataBase`) is therefore the single source, and everything a page publishes is re-based onto it. No
 * deployment can be edited into disagreement with itself.
 *
 * `siteUrl` is the one-URL rewrite; `rebaseDeep` is for payloads that are mostly prose with URLs buried in them
 * (the JSON-LD graph), and it rewrites only strings that begin with the origin the payload itself used — never a
 * link to anybody else's site, which is a distinction a naive "replace the host" pass cannot make.
 *
 * This module is deliberately free of `next/headers`, of `server-only` and of any request: it is string arithmetic
 * on a configured origin, and it is worth tests that need no server (`site-origin.test.ts`).
 */

/** The origin this site publishes under. `NEXT_PUBLIC_*` is inlined at build time, so client bundles agree. */
export const SITE_ORIGIN: string = (process.env.NEXT_PUBLIC_SITE_ORIGIN ?? "https://openout.app").replace(/\/+$/, "");

/** The origin an absolute URL names, or `null` when it is relative or unparseable. */
export function originOf(url: string): string | null {
  const match = /^([a-z][a-z0-9+.-]*:\/\/[^/?#]+)/i.exec(url);
  return match ? (match[1] ?? null) : null;
}

/**
 * One URL, moved onto this site's origin.
 *
 * A path stays a path (it is already this site's), an absolute URL keeps its path, query and fragment and loses
 * only the host it was minted on. An empty string stays empty, because callers pass optional fields and a
 * canonical of `SITE_ORIGIN` would be a worse answer than no canonical at all.
 */
export function siteUrl(urlOrPath: string): string {
  if (!urlOrPath) return "";
  const origin = originOf(urlOrPath);
  if (origin === null) return `${SITE_ORIGIN}${urlOrPath.startsWith("/") ? "" : "/"}${urlOrPath}`;
  if (origin === SITE_ORIGIN) return urlOrPath;
  return `${SITE_ORIGIN}${urlOrPath.slice(origin.length)}`;
}

/**
 * Every URL in a JSON-shaped payload that belonged to the payload's own origin, moved onto this site's.
 *
 * `from` is the origin the payload used (in practice `originOf(page.url)`), and it is the whole safety property:
 * a string is rewritten only if it starts with exactly that origin followed by a path, a query or nothing. Links
 * the API pointed somewhere else — a partner, a source, `schema.org` — are copied through untouched, and so is
 * every non-URL string in the payload, including the question text of a market that happens to begin with "http".
 */
export function rebaseDeep<T>(value: T, from: string | null): T {
  if (from === null || from === "") return value;
  const walk = (node: unknown): unknown => {
    if (typeof node === "string") {
      if (!node.startsWith(from)) return node;
      const rest = node.slice(from.length);
      return rest === "" || rest.startsWith("/") || rest.startsWith("?") || rest.startsWith("#")
        ? `${SITE_ORIGIN}${rest}`
        : node;
    }
    if (Array.isArray(node)) return node.map(walk);
    if (node !== null && typeof node === "object") {
      const out: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(node as Record<string, unknown>)) out[k] = walk(v);
      return out;
    }
    return node;
  };
  return walk(value) as T;
}
