import { describe, expect, it } from "vitest";
import { ROUTE_NOTES } from "./route-notes";
import { ROUTES, type RouteKey } from "./routes";

/**
 * The prose half of the ledger is only worth having if it stays complete, and it is only worth moving out of
 * `routes.ts` if nothing moves it back.
 *
 * The rule the P08 gate also enforces (c1, same `ledger_findings`): no note outlives its route. Coverage is
 * deliberately *not* a rule here — the launch list in docs/P08-frontend-shell.md §4 already explains every
 * unbuilt route, and a second list that must agree with the first is a second list that can disagree. The byte
 * decision is the other two tests: `routes.ts` is imported by `client.ts`, so a `note` field on a `RouteDecl`
 * is prose on every phone's first load, out of a 200 KB budget the notes were taking 2.9 KB of.
 */
describe("the route notes are complete and stay off the wire", () => {
  const keys = Object.keys(ROUTES) as RouteKey[];

  it("does not carry a note for a route that no longer exists", () => {
    const stray = Object.keys(ROUTE_NOTES).filter((k) => !(k in ROUTES));
    expect(stray).toEqual([]);
  });

  it("keeps the notes out of the module the client fetches", () => {
    const onWire = keys.filter((k) => "note" in ROUTES[k]);
    expect(onWire).toEqual([]);
    // A note is a sentence, not a document: the long version belongs in docs/P08-frontend-shell.md.
    const tooLong = Object.entries(ROUTE_NOTES).filter(([, v]) => (v ?? "").length > 160);
    expect(tooLong).toEqual([]);
  });
});
