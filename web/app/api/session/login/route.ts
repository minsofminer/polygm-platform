import { NextResponse } from "next/server";
import { establish } from "@/auth/server";

/** Sign-in with email-or-handle + password. The response carries no token: `establish` put both tokens in
 *  httpOnly cookies and there is nothing here for a script to steal. */
export async function POST(request: Request) {
  const body = (await request.json().catch(() => null)) as { identifier?: unknown; password?: unknown } | null;
  if (!body || typeof body.identifier !== "string" || typeof body.password !== "string") {
    return NextResponse.json({ error: { code: "VALIDATION", message: "identifier and password are both required", retryable: false, requestId: "" } }, { status: 422 });
  }
  const out = await establish({ path: "/v1/auth/login", body: { identifier: body.identifier, password: body.password } });
  if (!out.ok) {
    return NextResponse.json(
      { error: { code: out.code, message: out.message, retryable: out.code === "ACCOUNT_LOCKED", requestId: out.requestId } },
      { status: out.status, ...(out.code === "ACCOUNT_LOCKED" ? { headers: { "retry-after": "900" } } : {}) },
    );
  }
  return NextResponse.json({ state: "authenticated", user: { id: out.userId }, expiresInMs: out.expiresInMs });
}
