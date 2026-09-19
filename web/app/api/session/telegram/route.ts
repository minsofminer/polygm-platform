import { NextResponse } from "next/server";
import { establish } from "@/auth/server";

/**
 * The Mini App sign-in. The browser hands over the raw `initData` string and nothing is read from it here:
 * the HMAC is recomputed server-side by the P07 plane (secret key = HMAC_SHA256("WebAppData", bot token),
 * data-check string = the alphabetised `key=value` lines without `hash`/`signature`, `auth_date` freshness
 * mandatory). A page that forged `auth_date` gets a refusal, not a session.
 */
export async function POST(request: Request) {
  const body = (await request.json().catch(() => null)) as { initData?: unknown } | null;
  if (!body || typeof body.initData !== "string" || body.initData.length === 0) {
    return NextResponse.json({ error: { code: "VALIDATION", message: "initData is required", retryable: false, requestId: "" } }, { status: 422 });
  }
  const out = await establish({ path: "/v1/auth/telegram", body: { initData: body.initData } });
  if (!out.ok) {
    return NextResponse.json(
      { error: { code: out.code, message: out.message, retryable: false, requestId: out.requestId } },
      { status: out.status },
    );
  }
  return NextResponse.json({ state: "authenticated", user: { id: out.userId }, expiresInMs: out.expiresInMs });
}
