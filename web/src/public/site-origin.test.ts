/**
 * Tests for the origin rewrite, including the two cases that make it safe to apply to a whole payload: a link to
 * somebody else's site, and a market question that opens with the letters "http".
 */
import { describe, expect, it } from "vitest";

import { originOf, rebaseDeep, SITE_ORIGIN, siteUrl } from "./site-origin";

describe("originOf", () => {
  it("reads the origin of an absolute URL and nothing else", () => {
    expect(originOf("https://polygm-api.vercel.app/market/mayor-2027?x=1#a")).toBe("https://polygm-api.vercel.app");
    expect(originOf("http://127.0.0.1:3200/leaderboard/pnl")).toBe("http://127.0.0.1:3200");
    expect(originOf("/market/mayor-2027")).toBeNull();
    expect(originOf("")).toBeNull();
  });
});

describe("siteUrl", () => {
  it("moves an absolute URL onto this site, keeping its path, query and fragment", () => {
    expect(siteUrl("https://polygm-api.vercel.app/market/mayor-2027")).toBe(`${SITE_ORIGIN}/market/mayor-2027`);
    expect(siteUrl("https://polygm-api.vercel.app/leaderboard/pnl?window=7d")).toBe(`${SITE_ORIGIN}/leaderboard/pnl?window=7d`);
    expect(siteUrl("https://polygm-api.vercel.app")).toBe(SITE_ORIGIN);
  });

  it("treats a path as already ours and never doubles the slash", () => {
    expect(siteUrl("/markets")).toBe(`${SITE_ORIGIN}/markets`);
    expect(siteUrl("market/x")).toBe(`${SITE_ORIGIN}/market/x`);
  });

  it("leaves an empty URL empty — a canonical we do not have is better than a wrong one", () => {
    expect(siteUrl("")).toBe("");
  });

  it("does not touch a URL that is already ours", () => {
    const mine = `${SITE_ORIGIN}/market/x`;
    expect(siteUrl(mine)).toBe(mine);
  });
});

describe("rebaseDeep", () => {
  const from = "https://polygm-api.vercel.app";
  it("rewrites URLs that belonged to the payload's own origin, however deep they sit", () => {
    const graph = [
      { "@type": "ListItem", item: `${from}/markets` },
      { "@type": "Event", url: `${from}/market/mayor-2027`, sameAs: [`${from}`] },
      { untouched: 7, flag: true, nil: null },
    ];
    const out = rebaseDeep(graph, from) as typeof graph;
    expect(out[0]?.item).toBe(`${SITE_ORIGIN}/markets`);
    expect(out[1]?.url).toBe(`${SITE_ORIGIN}/market/mayor-2027`);
    expect(out[1]?.sameAs?.[0]).toBe(SITE_ORIGIN);
    expect(out[2]).toEqual({ untouched: 7, flag: true, nil: null });
  });

  it("copies through a link to anybody else's site — including one that merely starts with the same host name", () => {
    const graph = { partner: "https://polygm-api.vercel.app.evil.example/x", schema: "https://schema.org" };
    expect(rebaseDeep(graph, from)).toEqual(graph);
  });

  it("does not rewrite a market question that happens to begin with http", () => {
    const graph = { name: "http://will-this-be-a-url.example" };
    expect(rebaseDeep(graph, from)).toEqual(graph);
  });

  it("is a no-op when the payload gave no origin", () => {
    const graph = { url: `${from}/x` };
    expect(rebaseDeep(graph, null)).toEqual(graph);
    expect(rebaseDeep(graph, "")).toEqual(graph);
  });

  it("does not mutate its input — a payload is shared with the page that already rendered it", () => {
    const graph = { url: `${from}/x`, nested: [{ item: `${from}/y` }] };
    const before = JSON.stringify(graph);
    rebaseDeep(graph, from);
    expect(JSON.stringify(graph)).toBe(before);
  });
});
