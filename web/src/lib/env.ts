/**
 * The only module allowed to read `process.env` from code a client can import, and it reads nothing that
 * is not `NEXT_PUBLIC_`. `scripts/assert-env.mjs` enforces the rule at build time; this file enforces it
 * at runtime so a mis-set value is a loud startup failure rather than a request to `undefined`.
 */
const raw = {
  apiBase: process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api",
  wsOrigin: process.env.NEXT_PUBLIC_WS_ORIGIN ?? "",
};

export const env = {
  apiBase: raw.apiBase.replace(/\/+$/, ""),
  /** Empty means "no WebSocket exists yet": useLive then runs in REST mode and never flashes. */
  wsOrigin: raw.wsOrigin,
  hasWebSocket: raw.wsOrigin.length > 0,
} as const;

if (!raw.apiBase.startsWith("/") && !/^https?:\/\//.test(raw.apiBase)) {
  throw new Error(
    `NEXT_PUBLIC_API_BASE_URL must be same-origin (a path) or an https origin, got ${JSON.stringify(raw.apiBase)}`,
  );
}
