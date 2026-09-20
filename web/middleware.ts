import { NextResponse, type NextRequest } from "next/server";
import { surfaceAllows } from "@/tma/surface";
import { isMiniAppSurface, mainSiteUrl } from "@/tma/surface.server";

/**
 * The surface gate for the Mini App's own deployment.
 *
 * On the main site this middleware does nothing at all — one `if` and a pass-through, so the deployment that serves
 * everything is unaffected by a rule that exists for the deployment that does not. On the Mini App's domain it is the
 * difference between "we do not link to /admin" and "/admin is not here": everything off the allowlist gets a 404
 * with a sentence saying where the rest of the product lives.
 *
 * A 404 rather than a redirect is deliberate. A redirect would make the Mini App domain a doorway to the main site,
 * which is exactly the second indexable copy this split exists to avoid, and it would hide a typo in a deep link
 * behind a page that looks fine.
 */
export function middleware(request: NextRequest) {
  if (!isMiniAppSurface()) return NextResponse.next();
  const { pathname } = request.nextUrl;
  if (surfaceAllows(pathname)) return NextResponse.next();
  const site = mainSiteUrl();
  return new NextResponse(
    `<!doctype html><html lang="en"><head><meta charset="utf-8">` +
      `<meta name="viewport" content="width=device-width,initial-scale=1">` +
      `<meta name="robots" content="noindex">` +
      `<title>Nothing here · Openout</title></head>` +
      `<body style="margin:0;padding:24px;font:16px/1.5 system-ui,sans-serif">` +
      `<h1 style="font-size:20px">Nothing here</h1>` +
      `<p>This address serves the Openout Mini App inside Telegram. The terminal lives at ` +
      `<a href="${site}">${site.replace(/^https?:\/\//, "")}</a>.</p>` +
      `</body></html>`,
    { status: 404, headers: { "content-type": "text/html; charset=utf-8", "x-robots-tag": "noindex" } },
  );
}

/** Everything except the framework's static assets, which never need a decision made about them. */
export const config = {
  matcher: ["/((?!_next/static|_next/image).*)"],
};
