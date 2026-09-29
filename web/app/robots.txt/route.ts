/**
 * `GET /robots.txt` — five rules and the line that matters, the one naming our own sitemap.
 *
 * Without it a crawler has no address for the sitemap and will not find one: nothing links to `/sitemap.xml`,
 * which is the whole point of a sitemap. On the Mini App's deployment the document is `Disallow: /` — that
 * surface declares itself `noindex` at the layout and 404s everything off its allowlist, and this is the same
 * decision stated in the one file a crawler reads before it reads anything else.
 */
import { isMiniAppSurface } from "@/tma/surface.server";
import { robotsTxt } from "@/public/crawl";
import { requestOrigin } from "@/public/request-origin";
import { SITE_ORIGIN } from "@/public/site-origin";

export const dynamic = "force-dynamic";

export function GET(request: Request): Response {
  const origin = requestOrigin(request.headers, SITE_ORIGIN, request.url);
  return new Response(robotsTxt(origin, { disallowAll: isMiniAppSurface() }), {
    status: 200,
    headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "public, s-maxage=3600" },
  });
}
