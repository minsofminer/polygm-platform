/**
 * `t.me/<bot>/<app>?startapp=<payload>` carries a market, a trader or a referral. The payload is attacker-shaped data
 * — anyone can mint a link — so it is parsed, charset-checked, length-capped, and then *looked up*: an unresolvable
 * value renders the honest empty state rather than a half-populated page, and a referral code is recorded as an
 * attribution string, never trusted as identity.
 *
 * **The grammar changed in P12, and the reason is a bug this file had from P08 to P12.** It used `:` as its
 * separator (`m:<id>`), and everything else followed: the Python side that mints the channel's links emitted a bare
 * slug, so every alert's trade button produced a payload this parser rejected — "that link's payload did not look
 * right" was, in the product, the message behind every trade button we shipped. Two more things were wrong underneath
 * it: a colon is not a character Telegram's `startapp` value is documented to carry (the value is a short URL-safe
 * token, so anything outside `A-Za-z0-9_-` is percent-encoded by a client at best), and the target for a market was
 * `/markets/<id>` — the *list* route with an extra segment, which is a 404 no test had walked to.
 *
 * The grammar now lives in `contracts/startapp.json`, which both this file and the Python emitter are tested against.
 * A change to one side that is not a change to both fails a test rather than reaching a customer.
 */
import { STARTAAPP_CONTRACT as contract } from "./startapp.gen";

export type StartPayload =
  | { kind: "market"; marketId: string; referral?: string }
  | { kind: "trader"; address: string; referral?: string }
  | { kind: "referral"; code: string }
  | { kind: "none" }
  | { kind: "rejected"; reason: "too-long" | "bad-characters" | "unknown-shape" };

/** One segment of the contract's charset, as a character class. Read from the contract so the two sides agree. */
const VALUE = new RegExp(`^[${contract.valueCharsetLiteral.replace(/[-\]\\^]/g, (c) => `\\${c}`)}]+$`);
const MAX_VALUE = contract.valueMaxLen;
const MAX_LEN = contract.payloadMaxLen;
const TAGS = contract.tags as Record<string, string>;

export function parseStartapp(raw: string | null | undefined): StartPayload {
  if (!raw) return { kind: "none" };
  if (raw.length > MAX_LEN) return { kind: "rejected", reason: "too-long" };
  // The separator is the FIRST one only: a slug may contain hyphens (`fed-cut-sept`), and splitting on all of them is
  // how one value becomes three attacker-chosen fields.
  const cut = raw.indexOf(contract.separator);
  if (cut <= 0) return { kind: "rejected", reason: "unknown-shape" };
  const tag = raw.slice(0, cut);
  const value = raw.slice(cut + contract.separator.length);
  if (!(tag in TAGS) || !value) return { kind: "rejected", reason: "unknown-shape" };
  if (value.length > MAX_VALUE || !VALUE.test(value)) return { kind: "rejected", reason: "bad-characters" };
  switch (tag) {
    case "m":
      return { kind: "market", marketId: value };
    case "w":
      return { kind: "trader", address: value };
    case "r":
      return { kind: "referral", code: value };
    default:
      return { kind: "rejected", reason: "unknown-shape" };
  }
}

/**
 * What each surface does with each verdict, and the only place a rejection becomes words.
 *
 * `surface` exists because the two surfaces are genuinely different places: the main site has a market list and a
 * `/market/<slug>` route to send a deep link to, and the Mini App has neither (its middleware 404s everything except
 * the trade screen). A single notice string therefore has to lie on one of them.
 *
 * **Reasons are never rendered.** The screen used to print `payload.reason` — `"bad-characters"` is a machine token,
 * and P12's own rule is that refusals arrive as sentences. The mapping below is where a reason becomes one, and the
 * `never` branch is the tripwire: add a reason and TypeScript makes you write its sentence.
 */
export type StartappSurface = "web" | "miniapp";

const REJECTION_TEXT: Record<"too-long" | "bad-characters" | "unknown-shape", string> = {
  "too-long": "That link was too long to be one of ours, so it was ignored.",
  "bad-characters": "That link carried characters we never put in a link, so it was ignored.",
  "unknown-shape": "That link was not the shape we mint, so it was ignored.",
};

export function startappTarget(
  payload: StartPayload,
  surface: StartappSurface = "web",
): { href: string | null; notice: string | null } {
  switch (payload.kind) {
    case "market":
      // On the Mini App the market key is the sheet's input and there is no other route to send it to; the screen
      // consumes `payload.marketId` itself.
      return surface === "web"
        ? { href: `/market/${encodeURIComponent(payload.marketId)}`, notice: null }
        : { href: null, notice: null };
    case "trader":
      return surface === "web"
        ? { href: `/trader/${encodeURIComponent(payload.address)}`, notice: null }
        : { href: null, notice: "Trader links open on the main site — this app trades one market at a time." };
    case "referral":
      return { href: null, notice: "Referral recorded — here is what is moving today." };
    case "none":
      return { href: null, notice: null };
    case "rejected": {
      const why = REJECTION_TEXT[payload.reason];
      return {
        href: null,
        notice:
          surface === "web"
            ? `${why} Here is the market list.`
            : `${why} Open the bot and tap a market to trade it.`,
      };
    }
    default: {
      const exhaustive: never = payload;
      return exhaustive;
    }
  }
}
