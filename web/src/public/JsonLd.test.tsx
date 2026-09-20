/**
 * The one component on a public page that turns data into code, and therefore the one that has to be right.
 *
 * JSON-LD lives inside a `<script>` element, so the HTML parser reads it first: a payload string containing
 * `</script>` would end the element and everything after it becomes markup. The first test is that escape; the
 * second is the contract with the API — this component prints the graph it was given, and it prints nothing at
 * all when the graph is empty, because an empty `<script type="application/ld+json">` is a validator warning on
 * every page that has no structured data yet.
 */
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { JsonLd, withTrail } from "./JsonLd";

function block(html: string): string {
  const el = document.createElement("div");
  el.innerHTML = html;
  return el.querySelector("script[type='application/ld+json']")?.textContent ?? "";
}

describe("structured data", () => {
  it("escapes the character that would close its own element", () => {
    const hostile = [{ "@type": "Question", name: "Will </script><img src=x> resolve?" }];
    const { container } = render(<JsonLd graph={hostile} />);
    const html = container.innerHTML;
    expect(html).not.toContain("</script><img");
    expect(html).toContain("\\u003c/script");
    // and it is still valid JSON once parsed, which is the point of escaping rather than dropping
    const parsed = JSON.parse(block(html)) as { name: string }[];
    expect(parsed[0]?.name).toContain("</script>");
  });

  it("rewrites the breadcrumb so the markup carries the trail the page renders", () => {
    const graph = [
      { "@context": "https://schema.org", "@type": "BreadcrumbList",
        itemListElement: [{ "@type": "ListItem", position: 1, name: "Leaderboard",
                            item: "https://openout.app/leaderboard" }] },
      { "@type": "ProfilePage", url: "https://openout.app/trader/deep_book" },
    ];
    const trail = [{ label: "Openout", href: "/" },
                   { label: "the boards", href: "/leaderboard/risk_adjusted" },
                   { label: "@deep_book" }];
    const out = withTrail(graph, trail, "https://openout.app/trader/deep_book") as Record<string, unknown>[];
    const crumbs = out[0]?.itemListElement as { name: string; item: string; position: number }[];
    expect(crumbs.map((c) => c.name)).toEqual(["Openout", "the boards", "@deep_book"]);
    expect(crumbs.map((c) => c.position)).toEqual([1, 2, 3]);
    // The last crumb is the page itself: it keeps the payload's canonical URL, because a trail that drops the
    // link to its own last step is a trail a crawler renders as unlinked text.
    expect(crumbs[2]?.item).toBe("https://openout.app/trader/deep_book");
    // ...and the other nodes are untouched.
    expect(out[1]).toEqual(graph[1]);
  });

  it("leaves a graph alone when there is no trail to state, or nothing to state it in", () => {
    const graph = [{ "@type": "BreadcrumbList", itemListElement: [{ name: "Leaderboard" }] }];
    expect(withTrail(graph, [{ label: "Home" }], "https://x.example/")).toEqual(graph);
    expect(withTrail([{ "@type": "ProfilePage" }],
                     [{ label: "a", href: "/" }, { label: "b" }], "https://x.example/"))
      .toEqual([{ "@type": "ProfilePage" }]);
  });

  it("prints the graph it was given, and nothing when there is none", () => {
    const { container } = render(<JsonLd graph={[{ "@type": "ItemList" }, {}]} />);
    const parsed = JSON.parse(block(container.innerHTML)) as { "@type"?: string }[];
    expect(parsed.length).toBe(1);
    expect(parsed[0]?.["@type"]).toBe("ItemList");
    const empty = render(<JsonLd graph={[]} />);
    expect(empty.container.querySelector("script")).toBeNull();
  });
});
