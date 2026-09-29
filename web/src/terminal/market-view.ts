import { microOf } from "@/lib/depth";

/**
 * The terminal's market-view helpers — and the reason they are NOT in `TerminalScreen.tsx`.
 *
 * This module exists because of a defect the browser found on 2026-09-29: `marketTitle` and `volumeCents` are
 * pure functions, and they lived in `TerminalScreen.tsx`, which is a `"use client"` module. The server component
 * `app/(app)/terminal/page.tsx` imported `marketTitle` and called it while rendering — which is not possible in
 * the App Router ("Attempted to call marketTitle() from the server but marketTitle is on the client"), so
 * `/terminal` returned **500**. The page's own doc comment promised the opposite: *"A failed read is not a failed
 * page … the difference between 'the API is down' and 'this route is blank'"* — and the route was blank, because
 * the failure happened before the resilience it described could matter.
 *
 * Nothing here needs a browser: one string fallback and one unit conversion. They belong in a module both sides
 * can call, and the type belongs with them so a server page never has to reach into a client component to
 * describe the shape it is holding. The rule this encodes, for whoever adds the next helper: **a pure function
 * lives in a plain module; only things that need the browser live behind `"use client"`.**
 */

/** What the picker, the trending rail and the tape label need to know about a market. */
export type TerminalMarketRef = {
  marketId: string;
  question: string;
  slug?: string;
  volume24h?: string;
};

/** The label a market is shown by: its question, or the id when the venue has not given us one yet. */
export function marketTitle(ref: TerminalMarketRef): string {
  return ref.question || ref.marketId;
}

/** The volume column the trending list sorts by, in the rail's own units (cents). */
export function volumeCents(ref: TerminalMarketRef): number {
  return Math.trunc(microOf(ref.volume24h ?? "0") / 10 ** 4);
}
