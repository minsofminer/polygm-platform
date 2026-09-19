import { describe, expect, it } from "vitest";
import { parseStartapp, startappTarget } from "./startapp";

describe("startapp payloads are parsed, not trusted", () => {
  it("understands the four shapes we mint", () => {
    expect(parseStartapp("m:1234.5:6789")).toEqual({ kind: "market", marketId: "1234.5:6789" });
    expect(parseStartapp("w:0xabc123")).toEqual({ kind: "trader", address: "0xabc123" });
    expect(parseStartapp("r:ana")).toEqual({ kind: "referral", code: "ana" });
    expect(parseStartapp("mr:1234.5+ana")).toMatchObject({ kind: "market", marketId: "1234.5", referral: "ana" });
  });

  it("carries no HTML, no quote, no path traversal into a router", () => {
    for (const evil of ["m:../../etc/passwd", 'm:<img src=x onerror=alert(1)>', "m:1 2", "m:", ":x", "m:a+b+c", "x:y"]) {
      const out = parseStartapp(evil);
      expect(["rejected", "market", "trader", "referral", "none"]).toContain(out.kind);
      if (out.kind === "market") expect(out.marketId).toMatch(/^[A-Za-z0-9:_.-]+$/);
      if (out.kind === "trader") expect(out.address).toMatch(/^[A-Za-z0-9:_.-]+$/);
      const href = startappTarget(out).href;
      if (href !== null) {
        expect(href.startsWith("/")).toBe(true);
        expect(/["'<>\s]/.test(href)).toBe(false);
      }
    }
  });

  it("rejects an oversized payload rather than truncating it into a different id", () => {
    expect(parseStartapp("m:" + "1".repeat(400))).toEqual({ kind: "rejected", reason: "too-long" });
    expect(parseStartapp("m:1".repeat(200)).kind).toBe("rejected");
  });

  it("an empty or missing payload is 'none', which is not an error to show the user", () => {
    expect(parseStartapp(null)).toEqual({ kind: "none" });
    expect(parseStartapp("")).toEqual({ kind: "none" });
    expect(startappTarget({ kind: "none" })).toEqual({ href: null, notice: null });
  });

  it("never sends the user to a route we do not have because a link said so", () => {
    expect(startappTarget({ kind: "rejected", reason: "unknown-shape" })).toEqual({
      href: null,
      notice: "that link's payload was rejected",
    });
  });
});
