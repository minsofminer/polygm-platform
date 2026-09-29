/**
 * Tests for the two documents a crawler reads. They are short on purpose: each one asserts the property that
 * would silently cost the site its index if it broke — every `<loc>` naming the site that served it, and a
 * robots.txt that does not disallow its own sitemap.
 */
import { describe, expect, it } from "vitest";

import { robotsTxt, sitemapXml, xmlEscape } from "./crawl";

const payload = {
  robots: "index, follow",
  generatedAtMs: 1790662823515,
  urls: [
    { url: "https://polygm-api.vercel.app/leaderboard/risk_adjusted", changefreq: "hourly", priority: "0.7" },
    { url: "https://polygm-api.vercel.app/market/mayor-2027", changefreq: "hourly", priority: "0.6" },
    { url: "https://polygm-api.vercel.app/market/mayor-2027?ref=board", changefreq: "hourly", priority: "0.6" },
    { url: "https://polygm-api.vercel.app/market/mayor-2027", changefreq: "hourly", priority: "0.6" },
  ],
};

describe("sitemapXml", () => {
  const xml = sitemapXml(payload, "https://openout.app");

  it("is a sitemap document a crawler will parse", () => {
    expect(xml.startsWith(`<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">`)).toBe(true);
    expect(xml.trimEnd().endsWith("</urlset>")).toBe(true);
  });

  it("sends every entry to the site that served the sitemap, never to the API that built it", () => {
    expect(xml).toContain("<loc>https://openout.app/market/mayor-2027</loc>");
    expect(xml).toContain("<loc>https://openout.app/leaderboard/risk_adjusted</loc>");
    expect(xml).not.toContain("polygm-api.vercel.app");
  });

  it("keeps a query string — a windowed board is a different page from the board", () => {
    expect(xml).toContain("<loc>https://openout.app/market/mayor-2027?ref=board</loc>");
  });

  it("prints an address once, whatever the payload says", () => {
    expect(xml.match(/<loc>https:\/\/openout\.app\/market\/mayor-2027<\/loc>/g)).toHaveLength(1);
  });

  it("writes `changefreq` and `priority` only when they are valid values", () => {
    const odd = sitemapXml({ urls: [{ url: "/a", changefreq: "whenever", priority: "high" }] }, "https://o.example");
    expect(odd).toContain("<loc>/a</loc>");
    expect(odd).not.toContain("changefreq");
    expect(odd).not.toContain("priority");
  });

  it("survives a payload with nothing in it, and a payload that is not a payload", () => {
    expect(sitemapXml({}, "https://o.example")).toContain("</urlset>");
    expect(sitemapXml({ urls: [{ url: "" }] } as never, "https://o.example")).not.toContain("<url>");
  });

  it("escapes a URL that would otherwise close the element", () => {
    expect(xmlEscape("/a?b=1&c=<2>")).toBe("/a?b=1&amp;c=&lt;2&gt;");
    expect(sitemapXml({ urls: [{ url: "/a?x=1&y=2" }] }, "https://o.example")).toContain("<loc>/a?x=1&amp;y=2</loc>");
  });
});

describe("robotsTxt", () => {
  const robots = robotsTxt("https://openout.app");

  it("points at its own sitemap, so the two artefacts agree by construction", () => {
    expect(robots).toContain("Sitemap: https://openout.app/sitemap.xml");
  });

  it("keeps the gated surfaces out of the index without blocking the pages that should be in it", () => {
    expect(robots).toContain("Allow: /");
    for (const path of ["/terminal", "/wallet", "/admin"]) expect(robots).toContain(`Disallow: ${path}`);
    // `/markets` must not be swallowed by a `Disallow: /m…` prefix: the rules above are exact paths.
    expect(robots).not.toMatch(/Disallow: \/m/);
  });

  it("disallows everything on the Mini App's surface, which is indexable by nobody", () => {
    expect(robotsTxt("https://polygm-mini-app.vercel.app", { disallowAll: true })).toBe("User-agent: *\nDisallow: /\n");
  });
});
