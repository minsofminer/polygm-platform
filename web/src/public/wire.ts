/**
 * The public pages' wire shapes, taken from the generated contract rather than re-typed by hand.
 *
 * `Schema<"PublicTraderPage">` is the schema `npm run gen:api` produced, so a field the API stops sending is a
 * type error here rather than a page that renders `undefined`. The three pages share the card type: the OG
 * image, the on-page card and the copy-to-clipboard text all render the SAME object the API returned, which is
 * the only arrangement in which a share card cannot disagree with the page it came from.
 */
import type { components } from "@/api/schema.gen";

type Schema<K extends keyof components["schemas"]> = components["schemas"][K];

export type PublicCard = Schema<"PublicCard">;
export type PublicStanding = Schema<"PublicStanding">;
export type PublicTraderPage = Schema<"PublicTraderPage">;
export type PublicMarketPage = Schema<"PublicMarketPage">;
export type PublicLeaderboardPage = Schema<"PublicLeaderboardPage">;
export type PublicBoardRow = PublicLeaderboardPage["rows"][number];

/** A page payload the view needs the stamp off, without importing the whole `Stamped` shape at each call site. */
export type Stamped = { asOf?: number; staleAfter?: number };

/** What the API says a page kind is eligible for. Rendered verbatim — the server decides indexability, not us. */
export type Robots = string;
