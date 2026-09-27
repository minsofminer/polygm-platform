/**
 * The webview session constraint, stated and mitigated (P08 D3).
 *
 * On the open web the session is an httpOnly cookie on our own origin and nothing else is needed. In the
 * Mini App the same cookie *should* work — we are first-party to the webview — but Android's WebView and
 * iOS' WKWebView both drop or partition cookies that a "third-party" context sets under some settings, and
 * a lost cookie in a webview has no UI: there is no address bar to click, and the user reads a login form
 * inside an app they never installed.
 *
 * The mitigation is that the webview always carries a fresh, forgeable-by-nobody credential the client
 * cannot read: `initData`. So a 401 inside a TMA triggers one silent re-auth from `initData`, once per
 * cold start, and if that fails the user sees a sentence rather than a form.
 */
import { isTma, tma } from "./bridge";

export type ReauthResult = { attempted: false; reason: "not-tma" | "already-attempted" } | { attempted: true; ok: boolean; code?: string };

const attempted = new WeakSet<object>();

export async function silentReauth(state: object): Promise<ReauthResult> {
  if (!isTma()) return { attempted: false, reason: "not-tma" };
  if (attempted.has(state)) return { attempted: false, reason: "already-attempted" };
  attempted.add(state);
  const initData = tma()?.initData ?? "";
  if (!initData) return { attempted: true, ok: false, code: "NO_INIT_DATA" };
  let response: Response;
  try {
    response = await fetch("/api/session/telegram", {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ initData }),
    });
  } catch {
    // The gesture was made and the answer is "the WebApp could not reach the website" — a reason the caller can
    // act on, where an escaping exception would have been an unhandled rejection with no result at all.
    return { attempted: true, ok: false, code: "NETWORK" };
  }
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { error?: { code?: string } } | null;
    return { attempted: true, ok: false, code: body?.error?.code ?? `HTTP_${response.status}` };
  }
  return { attempted: true, ok: true };
}
