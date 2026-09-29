/**
 * The serialisers behind `/sitemap.xml` and `/robots.txt` — pure, so the shape of what a crawler reads is a
 * testable value rather than something only a running server can be asked about.
 *
 * `app/sitemap.xml/route.ts` and `app/robots.txt/route.ts` are three lines each around these: read, hand over,
 * return. Everything that could be wrong in a way nobody notices (a missing XML declaration, an entry with no
 * `<loc>`, a `<changefreq>` the spec does not define, a `Disallow:` that swallows the sitemap) is decided here.
 *
 * The URL rule is the reason this file exists at all: the API's payload publishes URLs under *its own* origin
 * (see `site-origin.ts`), and a sitemap is the one artefact where that mistake is not merely cosmetic. It is a
 * list of addresses handed to a crawler; if they name the API, Google spent its budget on JSON endpoints and the
 * site never entered the index. So every `<loc>` is re-based onto the origin the *request* arrived on, which is
 * correct by construction — a crawler that fetched `https://<site>/sitemap.xml` is told to crawl `https://<site>`.
 */

/** One entry of the API's sitemap payload, as `contracts/openapi.yaml` describes it. */
export type SitemapEntry = { url: string; changefreq?: string; priority?: string };
export type SitemapPayload = {
  robots?: string;
  generatedAtMs?: number;
  urls?: SitemapEntry[];
  caps?: { handles?: number; markets?: number };
  truncated?: Record<string, boolean>;
};

/** The `changefreq` values the sitemap protocol defines. An unknown one is dropped, not printed. */
const CHANGEFREQ = new Set(["always", "hourly", "daily", "weekly", "monthly", "yearly", "never"]);

/** XML text escaping for the five characters that can end an element or an attribute early. */
export function xmlEscape(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

/**
 * The sitemap document.
 *
 * `base` is the origin this response is being served from — every entry in the payload is re-based onto it by
 * path, so the crawler is sent to the site it is already talking to and never to the API. A relative entry (one
 * the payload did not make absolute) is kept as a path for the same reason.
 *
 * `lastmod` is deliberately absent. The payload knows when *it* was generated (`generatedAtMs`), which is not
 * when any of these pages changed, and a sitemap that claims every page changed at once, hourly, teaches a
 * crawler to ignore the field.
 */
export function sitemapXml(payload: SitemapPayload, base: string): string {
  const origin = base.replace(/\/+$/, "");
  const entries = payload.urls ?? [];
  const seen = new Set<string>();
  const rows: string[] = [];
  for (const entry of entries) {
    const raw = entry?.url ?? "";
    if (!raw) continue;
    // One `<loc>` per address: the payload can legitimately name a board twice (once per window), and a sitemap
    // that repeats itself is a crawl budget spent on the same page.
    const loc = /^https?:\/\//i.test(raw) ? `${origin}${new URL(raw).pathname}${new URL(raw).search}` : raw;
    if (seen.has(loc)) continue;
    seen.add(loc);
    const parts = [`    <loc>${xmlEscape(loc)}</loc>`];
    if (entry.changefreq && CHANGEFREQ.has(entry.changefreq)) parts.push(`    <changefreq>${entry.changefreq}</changefreq>`);
    if (entry.priority && /^[01](\.\d+)?$/.test(entry.priority)) parts.push(`    <priority>${entry.priority}</priority>`);
    rows.push(`  <url>\n${parts.join("\n")}\n  </url>`);
  }
  return [
    `<?xml version="1.0" encoding="UTF-8"?>`,
    `<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">`,
    ...rows,
    `</urlset>`,
    ``,
  ].join("\n");
}

/**
 * The robots document.
 *
 * The disallow list is the app's own gated surfaces: they are not secret (a signed-out request is answered with a
 * redirect to `/sign-in`), but a crawler that follows them indexes the sign-in page under a dozen URLs, which is
 * a duplicate-content problem with the product's name on it. `/api/` is disallowed because those responses are
 * JSON; `/tma` because it is the Mini App's shell, which declares itself `noindex` and is served on its own
 * domain anyway.
 *
 * The `Sitemap:` line is absolute against the *request's* origin, for the reason in this file's header — and it
 * is the reason a robots.txt without a sitemap line is half an artefact.
 */
export function robotsTxt(base: string, options: { disallowAll?: boolean } = {}): string {
  const origin = base.replace(/\/+$/, "");
  if (options.disallowAll === true) {
    // The Mini App's deployment: one surface, served inside Telegram, and indexable by nobody.
    return ["User-agent: *", "Disallow: /", ""].join("\n");
  }
  return [
    "User-agent: *",
    "Allow: /",
    "Disallow: /api/",
    "Disallow: /terminal",
    "Disallow: /wallet",
    "Disallow: /portfolio",
    "Disallow: /alerts",
    "Disallow: /automation",
    "Disallow: /copy",
    "Disallow: /radar",
    "Disallow: /profile",
    "Disallow: /billing",
    "Disallow: /referrals",
    "Disallow: /admin",
    "Disallow: /tma",
    "",
    `Sitemap: ${origin}/sitemap.xml`,
    "",
  ].join("\n");
}
