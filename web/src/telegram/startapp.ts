/**
 * `t.me/<bot>/<app>?startapp=<payload>` carries a market or a referral. The payload is attacker-shaped
 * data (anyone can mint a link), so it is parsed, length-capped, character-classed, and then *looked up*:
 * an unresolvable id renders the honest empty state, never a half-populated page, and a referral code is
 * recorded as an attribution string and not trusted as identity.
 */
export type StartPayload =
  | { kind: "market"; marketId: string; referral?: string }
  | { kind: "trader"; address: string; referral?: string }
  | { kind: "referral"; code: string }
  | { kind: "none" }
  | { kind: "rejected"; reason: "too-long" | "bad-characters" | "unknown-shape" };

const SAFE_ID = /^[A-Za-z0-9:_.-]{1,120}$/;
const MAX_LEN = 256;

export function parseStartapp(raw: string | null | undefined): StartPayload {
  if (!raw) return { kind: "none" };
  if (raw.length > MAX_LEN) return { kind: "rejected", reason: "too-long" };
  const parts = raw.split(":");
  const [prefix, value] = [parts[0], parts.slice(1).join(":")];
  if (!prefix || !value) return { kind: "rejected", reason: "unknown-shape" };
  if (prefix !== "mr" && !SAFE_ID.test(value)) return { kind: "rejected", reason: "bad-characters" };
  switch (prefix) {
    case "m":
      return { kind: "market", marketId: value };
    case "w":
      return { kind: "trader", address: value };
    case "r":
      return { kind: "referral", code: value };
    case "mr": {
      // The whole payload cannot be tested with SAFE_ID because `+` is the separator, so each half is
      // checked on its own. A separator that also happens to be a legal id character is how one id becomes
      // two attacker-chosen fields.
      const [market, referral] = raw.slice(prefix.length + 1).split("+");
      if (!market || !referral || !SAFE_ID.test(market) || !SAFE_ID.test(referral)) {
        return { kind: "rejected", reason: "unknown-shape" };
      }
      return { kind: "market", marketId: market, referral };
    }
    default:
      return { kind: "rejected", reason: "unknown-shape" };
  }
}

/** What the shell does with each verdict. Kept next to the parser so "rejected" cannot become an ignored
 *  return value somewhere else in the app. */
export function startappTarget(payload: StartPayload): { href: string | null; notice: string | null } {
  switch (payload.kind) {
    case "market":
      return { href: `/markets/${encodeURIComponent(payload.marketId)}`, notice: null };
    case "trader":
      return { href: `/traders/${encodeURIComponent(payload.address)}`, notice: null };
    case "referral":
      return { href: null, notice: `referral ${payload.code} recorded for this device` };
    case "none":
      return { href: null, notice: null };
    case "rejected":
      return { href: null, notice: "that link's payload was rejected" };
  }
}
