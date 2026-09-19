import { describe, expect, it } from "vitest";
import { csrfOk } from "./server.csrf";

describe("the mutating-request guard", () => {
  it("lets a same-origin page through", () => {
    expect(csrfOk({ method: "POST", origin: "https://openout.app", secFetchSite: "same-origin", secFetchDest: "empty" }).allowed).toBe(true);
  });
  it("honours a configured allowlist, so a proxied deployment is not locked out of itself", () => {
    expect(csrfOk({ method: "POST", origin: "https://openout.app", secFetchSite: "none", secFetchDest: "empty", allowedOrigins: ["https://openout.app"] }).allowed).toBe(true);
    expect(csrfOk({ method: "POST", origin: "https://evil.example", secFetchSite: "none", secFetchDest: "empty", allowedOrigins: ["https://openout.app"] }).allowed).toBe(false);
  });
  it("refuses a cross-site POST, which is what SameSite=None inside a webview would otherwise invite", () => {
    expect(csrfOk({ method: "POST", origin: "https://evil.example", secFetchSite: "cross-site", secFetchDest: "empty" }).allowed).toBe(false);
  });
  it("refuses when no origins are configured, because 'unconfigured' is not the same as 'everybody'", () => {
    expect(csrfOk({ method: "POST", origin: "https://evil.example", secFetchSite: null, secFetchDest: "empty" }).allowed).toBe(false);
    expect(csrfOk({ method: "POST", origin: "https://openout.app", secFetchSite: null, secFetchDest: "empty" }).allowed).toBe(false);
  });
  it("refuses a POST with no Origin at all — curl, and any fetch with no-cors oddity, is not our page", () => {
    expect(csrfOk({ method: "POST", origin: null, secFetchSite: "same-origin", secFetchDest: "empty" }).allowed).toBe(false);
  });
  it("never blocks a read, because a refused GET is a broken page, not a security win", () => {
    expect(csrfOk({ method: "GET", origin: null, secFetchSite: "cross-site", secFetchDest: "document" }).allowed).toBe(true);
  });
  it("reports cross-site so the cookie writer can pick SameSite=None for the webview", () => {
    expect(csrfOk({ method: "GET", origin: null, secFetchSite: "cross-site", secFetchDest: "iframe" }).crossSite).toBe(true);
  });
});
