/**
 * The origin a request actually arrived on — for the two artefacts that must name it (`/sitemap.xml`, `/robots.txt`).
 *
 * `new URL(request.url).origin` is the obvious answer and the wrong one on a server that is not the edge: this
 * app is started with `-H 0.0.0.0`, and Next fills in `request.url` from the address it bound, so the first
 * sitemap this repo ever served advertised `http://0.0.0.0:3200` to every crawler. Vercel and the preview proxy
 * in front of it do the same thing in the opposite direction: the socket is the proxy's, the public host is in
 * the headers.
 *
 * So the headers are read in the order a proxy chain writes them — `x-forwarded-host` (the host the client asked
 * for) first, because it is the only one that survives a hop, then `host`, then the bind address, and only then
 * the configured origin. The protocol follows the same rule: `x-forwarded-proto`, else `http` for an address that
 * cannot be public, else `https`.
 *
 * Pure and header-shaped, so the precedence is a test (`request-origin.test.ts`) rather than something a running
 * deployment has to be asked about — which is exactly how `0.0.0.0` reached production output the first time.
 */

/** The first value of a proxy header: a chain (`"a, b"`) names the client-facing hop first. */
function first(value: string | null): string | null {
  const head = value?.split(",")[0]?.trim();
  return head ? head : null;
}

/** The protocol to assume when nothing forwarded one: plain HTTP on an address that cannot be public, TLS anywhere else. */
function defaultProto(host: string): string {
  const bare = host.replace(/:\d+$/, "").replace(/^\[|\]$/g, "").toLowerCase();
  const local =
    bare === "localhost" ||
    bare.endsWith(".local") ||
    bare.startsWith("127.") ||
    bare === "0.0.0.0" ||
    bare === "::" ||
    /^10\./.test(bare) ||
    /^192\.168\./.test(bare) ||
    /^172\.(1[6-9]|2\d|3[01])\./.test(bare);
  return local ? "http" : "https";
}

/**
 * A wildcard bind address, which is never an origin anybody can fetch: `0.0.0.0` means "every interface", not
 * "this host". Nothing else is rejected. A loopback or private host is a poor origin for a public site and a
 * perfectly truthful one for a local preview — and refusing it would trade a wrong-but-obvious `127.0.0.1:3200`
 * for a confidently wrong `https://openout.app`, which is the worse of the two.
 */
function isWildcard(host: string): boolean {
  const bare = host.replace(/:\d+$/, "").replace(/^\[|\]$/g, "").toLowerCase();
  return bare === "" || bare === "0.0.0.0" || bare === "::" || bare === "*";
}

export function requestOrigin(headers: Headers, fallback: string, url?: string): string {
  const proto = first(headers.get("x-forwarded-proto"));
  const forwardedHost = first(headers.get("x-forwarded-host"));
  const hostHeader = first(headers.get("host"));

  const chosen = forwardedHost ?? hostHeader;
  if (chosen !== null && !isWildcard(chosen)) return `${proto ?? defaultProto(chosen)}://${chosen}`;

  // The bind address: keep it only when the request really did arrive on it (a local preview), never as a guess.
  if (url) {
    const origin = (() => {
      try {
        return new URL(url).origin;
      } catch {
        return null;
      }
    })();
    if (origin !== null) {
      const asHost = origin.replace(/^https?:\/\//, "");
      if (!isWildcard(asHost)) return origin;
    }
  }
  return fallback.replace(/\/+$/, "");
}
