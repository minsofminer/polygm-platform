/**
 * P08 shell config. Three things here are security controls, not build preferences, and the P08 gate
 * (`tools/p08-gate-check.py`) checks each one by asking the running server for its headers:
 *
 * 1. CSP `frame-ancestors` is per-surface, never global. The Mini App must be frameable by Telegram's own
 *    origins; every other route must not be frameable at all. `X-Frame-Options` is deliberately absent: it
 *    has no per-origin list, so setting it would either break the webview or open the rest of the site.
 *    This matches the decision recorded in docs/P07-security.md §"the frame the shell sits in".
 * 2. No `*.js` chunk may be immutable for longer than the release that produced it: the P05 conclusion
 *    (cache-busting by content hash only, never by "latest" tags) is honoured by Next's hashed asset names;
 *    the `/_next/static` cache header below is `immutable` because those URLs contain the content hash.
 *    `index.html`/RSC documents are `no-store` so a stale document cannot point at pruned chunks.
 * 3. `poweredByHeader: false` and no `Server` value we control that names a framework version.
 */
// Which deployment this is. The Mini App's own Vercel project sets `PGM_SURFACE=miniapp`; the main site does not,
// and an unset value must mean "the main site" rather than "refuse everything" — the failure mode of a missing env
// var has to be the product working, not the product vanishing.
const MINIAPP = (process.env.PGM_SURFACE ?? "").trim().toLowerCase() === "miniapp";

const TELEGRAM_ORIGINS = [
  "https://web.telegram.org",
  "https://telegram.org",
  "https://t.me",
];

function cspFor({ framing, tma = false }) {
  const parts = [
    "default-src 'self'",
    // The Mini App bridge is the only third-party script in the app, and it is allowed only on the subtree
    // that is framed by Telegram. A `script-src` that lets telegram.org in everywhere is how an unrelated
    // page ends up able to call HapticFeedback and read initDataUnsafe.
    "script-src 'self'" + (tma ? " https://telegram.org" : "") + " 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "connect-src 'self' " + (process.env.NEXT_PUBLIC_WS_ORIGIN ?? ""),
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors " + framing,
    "require-trusted-types-for 'script'",
  ].filter((p) => !p.endsWith(" "));
  return parts.join("; ");
}

export default {
  reactStrictMode: true,
  poweredByHeader: false,
  typescript: { ignoreBuildErrors: false },
  // No `externalDir`: the stylesheet boundary is handled by a generated mirror (tools/build-web-tokens.mjs)
  // because Turbopack resolves CSS @import inside the project root only, and `make web-check` runs that
  // generator's --check before the build so the mirror cannot drift from brand/tokens.css.
  async headers() {
    const none = { key: "Content-Security-Policy", value: cspFor({ framing: "'none'", tma: false }) };
    const tg = {
      key: "Content-Security-Policy",
      value: cspFor({ framing: "'self' " + TELEGRAM_ORIGINS.join(" "), tma: true }),
    };
    // On the Mini App's own domain every path it serves is frameable by Telegram and nothing is reachable off the
    // allowlist (see middleware.ts), so the CSP is stated once for the whole origin. Ordering matters here: a path
    // matched by both a `frame-ancestors 'none'` rule and a Telegram rule would send BOTH headers, and a browser
    // enforces the intersection — which is to say the webview breaks and no test in this repo would say why.
    const framing = MINIAPP
      ? [{ source: "/:path*", headers: [tg] }]
      : [
          { source: "/:path*", headers: [none] },
          { source: "/tma/:path*", headers: [tg] },
          { source: "/tma", headers: [tg] },
        ];
    return [
      ...framing,
      {
        source: "/:all*(html|txt)",
        headers: [{ key: "Cache-Control", value: "no-store, max-age=0" }],
      },
      // The Mini App's domain is not a second SEO surface: it exists to be opened inside Telegram, and two indexable
      // copies of the same product on two domains is duplicate content with a support cost. The main site keeps its
      // own indexability (`app/layout.tsx` metadata), and `web/vercel.json` deliberately carries no header like this
      // — that file is read by both deployments.
      ...(MINIAPP
        ? [{ source: "/:path*", headers: [{ key: "X-Robots-Tag", value: "noindex, nofollow" }] }]
        : []),
      {
        source: "/.well-known/:path*",
        headers: [{ key: "Cache-Control", value: "public, max-age=3600" }],
      },
    ];
  },
  // No rewrite for /api: the session proxy is a route handler (app/api/[...path]/route.ts), because a
  // rewrite could not rotate the refresh cookie. A proxy that forwards the body but not the rotation is how
  // a single-use refresh token gets replayed by the next request and the family revoked.
};
