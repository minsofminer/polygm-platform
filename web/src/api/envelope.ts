/**
 * The P4/P6/P7 error envelope, honoured exactly: `{error:{code,message,retryable,requestId}}`, and the
 * stamp fields `_stamped()` merges into every read (`asOf`, `staleAfter`, `cache.ttlMs`).
 *
 * Two facts drive the shape. (1) The API has no `detail` field in the envelope by design — that is where a
 * Python message would leak — so the client never renders `body.detail` and a UI that shows
 * `error.message` is showing a string we wrote, which is why refusals can be sentences.
 * (2) `asOf` is the age of the data, not the time we answered; freshness arithmetic must come from it or a
 * ten-minute-old book reports "fresh for two more seconds" (the exact P05 finding, recorded in
 * docs/P05-data-ingestion.md and enforced here by `stampAge`).
 */

export const ERROR_CODES = [
  "RISK_HALT",
  "MARKET_NOT_ACCEPTING",
  "NO_ORDER_BOOK",
  "STALE_QUOTE",
  "BAD_SIDE",
  "UNKNOWN_TICK",
  "OFF_TICK",
  "BAD_MARKET_META",
  "BELOW_MIN_SIZE",
  "ZERO_SIZE",
  "BAD_AMOUNT",
  "OVER_ORDER_CAP",
  "PRICE_FAR_FROM_MID",
  "TOO_MANY_OPEN",
  "DAILY_CAP",
  "IDEM_CONFLICT",
  "IDEM_IN_PROGRESS",
  "IDEM_KEY_REQUIRED",
  "RISK_UNAVAILABLE",
  "AUTHZ_UNDECLARED",
  "AUTHZ_DENIED",
  "ADMIN_REQUIRED",
  "TOTP_REQUIRED",
  "TOTP_INVALID",
  "TOTP_LOCKED",
  "LOGIN_FAILED",
  "ACCOUNT_LOCKED",
  "UNAUTHENTICATED",
  "SESSION_REVOKED",
  "REFRESH_REUSED",
  "TELEGRAM_INVALID",
  "TELEGRAM_REPLAY",
  "SECURITY_ENV_MISSING",
  "SIGNER_UNAVAILABLE",
  "VALIDATION",
  "NOT_FOUND",
  "CONFLICT",
  "INTERNAL",
] as const;

export type ErrorCode = (typeof ERROR_CODES)[number] | string;

export type ApiError = {
  code: ErrorCode;
  message: string;
  retryable: boolean;
  requestId: string;
  status: number;
  /** Seconds, when the server sent `Retry-After` (the login lock and the TOTP lock both do). */
  retryAfterS?: number;
};

export type Stamp = {
  asOf: number;
  staleAfter: number;
  ttlMs: number;
  serverAsOf?: number;
};

/** `stamp` is null for a mutation ack, which has no freshness to report. A *read* without a stamp is a
 *  failure (`UNSTAMPED_READ`), produced by the client, not a null here. */
export type Ok<T> = { ok: true; data: T; stamp: Stamp | null };
export type Err = { ok: false; error: ApiError };
export type Result<T> = Ok<T> | Err;

export function isApiError(value: unknown): value is ApiError {
  return typeof value === "object" && value !== null && "code" in value && "requestId" in value;
}

export function parseError(body: unknown, status: number, headers: Headers): ApiError {
  const e = (body as { error?: Record<string, unknown> } | null)?.error ?? {};
  const retryAfter = headers.get("retry-after");
  return {
    code: typeof e.code === "string" ? e.code : "INTERNAL",
    message: typeof e.message === "string" ? e.message : "the request could not be completed",
    retryable: e.retryable === true,
    requestId: typeof e.requestId === "string" ? e.requestId : "",
    status,
    ...(retryAfter ? { retryAfterS: Number(retryAfter) || undefined } : {}),
  };
}

/** Missing stamp fields are a bug in the caller or the route, not something to paper over: an unstamped
 *  read is rendered as `unknown`, which blocks trading, which is the safe direction. */
export function stampFrom(data: Record<string, unknown>): Stamp | null {
  const cache = data.cache as { ttlMs?: number } | undefined;
  if (typeof data.asOf !== "number" || typeof data.staleAfter !== "number") return null;
  return {
    asOf: data.asOf,
    staleAfter: data.staleAfter,
    ttlMs: typeof cache?.ttlMs === "number" ? cache.ttlMs : 0,
    ...(typeof data.serverAsOf === "number" ? { serverAsOf: data.serverAsOf } : {}),
  };
}

export function stampAge(stamp: Stamp, nowMs: number): number {
  return Math.max(0, nowMs - stamp.asOf);
}

export type Freshness = "live" | "stale" | "blocking" | "unknown";

/**
 * The client's own threshold, because the API says when data goes stale for *display* and the shell needs a
 * second, harsher line where a trade is refused (web/DESIGN.md §6: past `blocking`, order submission is
 * disabled). Blocking is stale + the same window again, so a surface never flips from fresh to blocking
 * without passing through stale on screen.
 */
export const BLOCKING_MULTIPLIER = 2;

export function freshnessOf(stamp: Stamp | null, nowMs: number): Freshness {
  if (!stamp) return "unknown";
  const age = stampAge(stamp, nowMs);
  const window = Math.max(0, stamp.staleAfter - stamp.asOf);
  if (age <= window) return "live";
  if (age <= window * BLOCKING_MULTIPLIER) return "stale";
  return "blocking";
}

export function canTradeGivenFreshness(f: Freshness): boolean {
  return f === "live";
}
