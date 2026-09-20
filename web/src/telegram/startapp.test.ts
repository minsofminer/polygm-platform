import { describe, expect, it } from "vitest";
import { parseStartapp, startappTarget } from "./startapp";
import { STARTAAPP_CONTRACT as contract } from "./startapp.gen";

/**
 * The payload grammar is shared with the Python bot (`contracts/startapp.json`), and this file is the half of that
 * agreement that runs in the browser's language. The other half is `test_telegrambot.py`, which mints links from the
 * same contract and asserts they parse; the two are joined by `tools/p12-gate-check.py`, which runs one against the
 * other. These tests are therefore about *this* side's behaviour, not about the grammar being whatever this side says.
 */
describe("startapp payloads are parsed, not trusted", () => {
  it("reads the grammar from the shared contract rather than carrying its own copy", () => {
    expect(contract.separator).toBe("-");
    expect(Object.keys(contract.tags).sort()).toEqual(["m", "r", "w"]);
    expect(contract.valueCharsetLiteral).toContain("z");
  });

  it("understands the shapes we mint", () => {
    expect(parseStartapp("m-fed-cut-sept")).toEqual({ kind: "market", marketId: "fed-cut-sept" });
    expect(parseStartapp("w-0xabc123")).toEqual({ kind: "trader", address: "0xabc123" });
    expect(parseStartapp("r-ana")).toEqual({ kind: "referral", code: "ana" });
    // The value's hyphens are the value's: only the first separator splits. Splitting on all of them is how one
    // slug becomes three attacker-chosen fields.
    expect(parseStartapp("m-a-b-c")).toEqual({ kind: "market", marketId: "a-b-c" });
  });

  it("carries no HTML, no quote, no path traversal into a router", () => {
    for (const evil of [
      "m-../../etc/passwd",
      "m-<img src=x onerror=alert(1)>",
      "m-1 2",
      "m-",
      "-x",
      "x-y",
      "m-a+b",
      "m-1234.5:6789", // the old colon grammar: legal in our charset only for the dot and digits it also contains
      "m-fed-cut-sept@evil.example",
    ]) {
      const out = parseStartapp(evil);
      expect(["rejected", "market", "trader", "referral", "none"]).toContain(out.kind);
      if (out.kind === "market") expect(out.marketId).toMatch(/^[A-Za-z0-9_.-]+$/);
      if (out.kind === "trader") expect(out.address).toMatch(/^[A-Za-z0-9_.-]+$/);
      const href = startappTarget(out).href;
      if (href !== null) {
        expect(href.startsWith("/")).toBe(true);
        expect(/["'<>\s]/.test(href)).toBe(false);
      }
    }
  });

  it("keeps every character Telegram's startapp parameter is documented to carry", () => {
    // A `:` in a payload is the shape that started this: percent-encoded by a client at best, dropped at worst, and
    // the parser at the other end then refuses a link the bot considers valid.
    const legal = /^[A-Za-z0-9_-]+$/;
    for (const good of ["m-fed-cut-sept", "w-0x4bbeEB066eD09B7AEd07bF39EEe0466f9EB3", "r-AB12"]) {
      expect(good).toMatch(legal);
      expect(parseStartapp(good).kind).not.toBe("rejected");
    }
    expect(parseStartapp("m-1234.5:6789").kind).toBe("rejected");
  });

  it("rejects an oversized payload rather than truncating it into a different id", () => {
    expect(parseStartapp("m-" + "1".repeat(400))).toEqual({ kind: "rejected", reason: "too-long" });
    expect(parseStartapp("m-1".repeat(200)).kind).toBe("rejected");
  });

  it("an empty or missing payload is 'none', which is not an error to show the user", () => {
    expect(parseStartapp(null)).toEqual({ kind: "none" });
    expect(parseStartapp("")).toEqual({ kind: "none" });
    expect(startappTarget({ kind: "none" })).toEqual({ href: null, notice: null });
  });

  it("sends a market link to the route that exists", () => {
    // `/market/<slug>` is the page; the first version of this helper appended the id to `/markets` — the *list* route
    // with a stray segment — which is a 404 that no test had walked to.
    const target = startappTarget({ kind: "market", marketId: "fed-cut-sept" });
    expect(target).toEqual({ href: "/market/fed-cut-sept", notice: null });
  });

  it("says the same refusal in words on both surfaces, and never prints the machine reason", () => {
    const web = startappTarget({ kind: "rejected", reason: "bad-characters" }, "web");
    const app = startappTarget({ kind: "rejected", reason: "bad-characters" }, "miniapp");
    for (const notice of [web.notice, app.notice]) {
      expect(notice).toBeTruthy();
      expect(notice).not.toMatch(/bad-characters|unknown-shape|too-long/);
      expect(notice).toMatch(/ignored\./);
    }
    expect(web.href).toBeNull();
    expect(app.href).toBeNull();
  });

  it("does not offer the Mini App a route its own surface 404s", () => {
    // The Mini App serves one page: sending a market or trader link to `/market/...` from inside it would land on
    // "Nothing here". The screen consumes `marketId` itself; a trader link is honest about living on the main site.
    expect(startappTarget({ kind: "market", marketId: "fed-cut-sept" }, "miniapp")).toEqual({
      href: null,
      notice: null,
    });
    const trader = startappTarget({ kind: "trader", address: "0xabc" }, "miniapp");
    expect(trader.href).toBeNull();
    expect(trader.notice).toContain("main site");
  });
});
