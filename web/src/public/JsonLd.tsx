/**
 * `structuredData`, as a script tag — and the breadcrumb trail, brought back in line with the visible one.
 *
 * Three rules, all of them about the fact that this is the only place on the page where the payload becomes code:
 *
 *  1. **`<` is escaped as `\u003c`.** JSON inside a `<script>` element is parsed by the HTML parser first, so a
 *     `resolutionCriteria` containing `</script>` would end the block and everything after it would be markup.
 *     Escaping the character that closes the element is the whole fix, and it survives `JSON.parse` unchanged.
 *  2. **Only the API's own graph is emitted.** This component does not build JSON-LD, it prints what
 *     `packages/polygm_core/public_pages/structured.py` built — the layer where `unbacked()` refuses a claim
 *     that is not also a field in the payload. A component that assembled its own graph could say something the
 *     page never shows, and a machine-readable claim with no visible backing is the one kind of error nobody
 *     reading the page can catch.
 *  3. **The breadcrumb is the exception, and it is a rewrite rather than an addition.** The API names the trail
 *     from its own payload (the card's brand, the board's label, the question) because it has no idea what words
 *     the page will use — the web owns the copy and translates it. So the page hands this component the trail it
 *     actually rendered and this component replaces the `BreadcrumbList`'s items with it. A crawler then shows
 *     the same trail a reader sees, which is the only reason to emit a breadcrumb at all. (The gate's c23 caught
 *     the mismatch this replaces: the graph said "Leaderboard" while the page said "the boards".)
 */
export type Crumb = { label: string; href?: string };

const BREADCRUMB = "BreadcrumbList";

function breadcrumbItems(node: unknown): { name?: string; item?: string }[] | null {
  if (!node || typeof node !== "object") return null;
  const type = (node as { "@type"?: unknown })["@type"];
  const types = Array.isArray(type) ? type : [type];
  if (!types.some((t) => t === BREADCRUMB)) return null;
  const items = (node as { itemListElement?: unknown }).itemListElement;
  return Array.isArray(items) ? (items as { name?: string; item?: string }[]) : [];
}

/** The graph with every `BreadcrumbList`'s items replaced by `trail`.
 *
 *  Positions are numbered from the trail's own order and each `item` is the crumb's `href` — except the last,
 *  which is the page itself and takes `self` (the payload's canonical URL) because a link to the page you are
 *  already on is the one link a trail must still carry. A trail of fewer than two steps is not a trail, so the
 *  API's version is kept; a graph with no breadcrumb at all is returned untouched.
 */
export function withTrail(graph: unknown[], trail: Crumb[], self = ""): unknown[] {
  if (trail.length < 2) return graph;
  return graph.map((node) => {
    const items = breadcrumbItems(node);
    if (!items) return node;
    return {
      ...(node as object),
      itemListElement: trail.map((crumb, i) => ({
        "@type": "ListItem",
        position: i + 1,
        name: crumb.label,
        item: crumb.href ?? (i === trail.length - 1 ? self : items[i]?.item ?? ""),
      })),
    };
  });
}

export function JsonLd({ graph, trail, self = "" }: { graph: unknown; trail?: Crumb[]; self?: string }) {
  const items = Array.isArray(graph) ? graph : [graph];
  const usable = items.filter((g) => g && typeof g === "object" && Object.keys(g as object).length > 0);
  if (!usable.length) return null;
  const json = JSON.stringify(trail ? withTrail(usable, trail, self) : usable);
  return (
    <script
      type="application/ld+json"
      // eslint-disable-next-line react/no-danger -- the escape above is the sanction: this is JSON, and `<` is
      // the only character that can leave the element.
      dangerouslySetInnerHTML={{ __html: json.replace(/</g, "\\u003c") }}
    />
  );
}
