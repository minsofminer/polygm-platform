import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { proxy } from "@/auth/server";

/**
 * The authenticated proxy for /api/v1/** — one hop for every read and mutation the shell makes. It exists so
 * that (a) the bearer token never enters the browser, and (b) a read is stamped *and* freshness-checked
 * against the same clock the API used, because a client-side "this data is 3 seconds old" computed from
 * `Date.now()` on a phone with a wrong clock is a lie in either direction.
 *
 * The upstream is FastAPI on `PGM_API_ORIGIN`; these are its paths, not ours.
 */
type Ctx = { params: Promise<{ path: string[] }> };

async function forward(request: NextRequest, ctx: Ctx) {
  const { path } = await ctx.params;
  const target = "/" + (path ?? []).join("/");
  const search = new URL(request.url).search;
  const body = request.method === "GET" || request.method === "HEAD" ? undefined : await request.json().catch(() => undefined);
  const idem = request.headers.get("idempotency-key");
  const out = await proxy(target + search, {
    method: request.method,
    ...(body !== undefined ? { body } : {}),
    ...(idem ? { headers: { "idempotency-key": idem } } : {}),
  });
  // Everything the upstream said is echoed with its status: the envelope, the code, the Retry-After. A proxy
  // that rewrites refusals into 200-shaped "no data" is how a refusal becomes a mystery, and P07's refusal
  // reasons only reach a user if they survive this hop.
  const headers: Record<string, string> = { "content-type": out.contentType, "cache-control": "no-store" };
  const retryAfter = retryAfterFrom(out.body);
  if (retryAfter) headers["retry-after"] = String(retryAfter);
  return new NextResponse(out.body, { status: out.status, headers });
}

function retryAfterFrom(body: string): number | null {
  try {
    const parsed = JSON.parse(body) as { error?: { retryAfterS?: number } };
    return typeof parsed.error?.retryAfterS === "number" ? parsed.error.retryAfterS : null;
  } catch {
    return null;
  }
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
export const PATCH = forward;
export const DELETE = forward;
