/**
 * `GET /sitemap.xml` — the site's own, not the API's.
 *
 * The read goes through `publicRead`, so this is one of the transports that honours the ledger's `anonymous`
 * declaration (the sitemap is `x-auth: public` in the contract; a crawler has no session and never will). The
 * payload's URLs name the API — see `src/public/site-origin.ts` for why, and for what that cost — so they are
 * re-based onto the origin this request arrived on before they are printed. That is the one origin guaranteed to
 * be right: the crawler fetched this file from it.
 *
 * A failure here is still a sitemap: an empty `urlset` is a valid document that a crawler will re-read next
 * cycle, where a 500 is an artifact it may cache against us and a redirect into `/sign-in` is a reason to drop
 * the whole host. `s-maxage` matches the payload's own horizon (`generatedAtMs` moves hourly in practice).
 */
import { publicRead } from "@/api/public-read";
import { sitemapXml, type SitemapPayload } from "@/public/crawl";
import { requestOrigin } from "@/public/request-origin";
import { SITE_ORIGIN } from "@/public/site-origin";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  // Not `new URL(request.url).origin`: this server binds 0.0.0.0 and Next reports that back, which is how
  // `http://0.0.0.0:3200` nearly became the origin in 143 published URLs. See src/public/request-origin.ts.
  const origin = requestOrigin(request.headers, SITE_ORIGIN, request.url);
  const read = await publicRead<SitemapPayload>("GET", "/v1/public/sitemap");
  const payload: SitemapPayload = read.ok === false ? {} : read.data;
  return new Response(sitemapXml(payload, origin), {
    status: 200,
    headers: {
      "content-type": "application/xml; charset=utf-8",
      "cache-control": "public, s-maxage=3600, stale-while-revalidate=86400",
    },
  });
}
