/**
 * The precedence rule, asserted: a public host header wins, an unroutable one never does, and there is always an
 * answer. The case that prompted the module is the first test — the sitemap this repo served before it named
 * `http://0.0.0.0:3200`, the address the server bound rather than the one the crawler asked for.
 */
import { describe, expect, it } from "vitest";

import { requestOrigin } from "./request-origin";

const FALLBACK = "https://openout.app";
const headers = (init: Record<string, string>) => new Headers(init);

describe("requestOrigin", () => {
  it("uses the host the client asked for, through a proxy", () => {
    const h = headers({ "x-forwarded-host": "openout.app", "x-forwarded-proto": "https", host: "10.0.0.4:3000" });
    expect(requestOrigin(h, FALLBACK, "http://0.0.0.0:3200/sitemap.xml")).toBe("https://openout.app");
  });

  it("takes the client-facing hop of a chain and follows the forwarded protocol", () => {
    const h = headers({ "x-forwarded-host": "openout.app, edge.internal", "x-forwarded-proto": "http, https" });
    expect(requestOrigin(h, FALLBACK)).toBe("http://openout.app");
  });

  it("falls back to the Host header when nothing forwarded it", () => {
    expect(requestOrigin(headers({ host: "openout.app" }), FALLBACK)).toBe("https://openout.app");
    expect(requestOrigin(headers({ host: "127.0.0.1:3100" }), FALLBACK, "http://127.0.0.1:3100/x")).toBe("http://127.0.0.1:3100");
  });

  it("refuses a wildcard bind address — `0.0.0.0` is every interface, not an origin", () => {
    expect(requestOrigin(headers({ host: "0.0.0.0:3200" }), FALLBACK, "http://0.0.0.0:3200/robots.txt")).toBe(FALLBACK);
    expect(requestOrigin(headers({ "x-forwarded-host": "[::]:3000" }), FALLBACK)).toBe(FALLBACK);
    expect(requestOrigin(headers({}), FALLBACK, "not a url")).toBe(FALLBACK);
  });

  it("keeps a private host rather than inventing a public one: a truth about the network beats a guess", () => {
    expect(requestOrigin(headers({ "x-forwarded-host": "10.1.2.3" }), FALLBACK)).toBe("http://10.1.2.3");
  });

  it("keeps a local preview honest — the URL it was really reached on is better than a guess", () => {
    expect(requestOrigin(headers({ host: "127.0.0.1:3200" }), FALLBACK, "http://127.0.0.1:3200/sitemap.xml"))
      .toBe("http://127.0.0.1:3200");
  });

  it("answers with the configured origin when there is nothing at all to go on", () => {
    expect(requestOrigin(headers({}), FALLBACK)).toBe(FALLBACK);
    expect(requestOrigin(headers({}), "https://openout.app/")).toBe("https://openout.app");
  });
});
