/**
 * The card's rules, as tests.
 *
 * The card is the artifact that gets screenshotted without the page around it, so the assertions here are about
 * what a reader of the IMAGE keeps: the qualifiers. `cardFootnoteFindings` is kind-aware — a market card owes
 * its reader the price's age, a board card owes the rules line, and a ranked trader card owes the provisional
 * window and the drawdown — and each of those three duties has a test that fails if it is dropped, because the
 * failure mode of all three is silent: the card just looks better.
 */
import { describe, expect, it } from "vitest";
import { cardAlt, cardFootnoteFindings, cardLines, cardIsDrawable, shareText } from "./card";
import type { PublicCard } from "./wire";

function card(over: Partial<PublicCard> = {}): PublicCard {
  return {
    kind: "trader",
    title: "@surat_whale",
    subtitle: "rank #4 of 91 on the risk-adjusted board",
    lines: [
      { label: "realised", value: "412000" },
      { label: "settled markets", value: "48" },
      { label: "win rate", value: "63.4%" },
    ],
    footnote: ["provisional: fewer than 7 days of history", "drawdown 12400"],
    brand: "Openout",
    footer: "openout.app — prediction markets, ranked on the trade and not the story",
    url: "https://openout.app/trader/surat_whale",
    ranked: true,
    provisional: true,
    ...over,
  } as PublicCard;
}

describe("the share card", () => {
  it("keeps the API's line order and its own strings", () => {
    expect(cardLines(card()).map((l) => l.label)).toEqual(["realised", "settled markets", "win rate"]);
  });

  it("finds a ranked trader card that lost its provisional line", () => {
    const found = cardFootnoteFindings(card({ footnote: ["drawdown 12400"] }));
    expect(found.length).toBe(1);
    expect(found[0]).toMatch(/provisional/);
  });

  it("finds a ranked trader card that lost its drawdown", () => {
    const found = cardFootnoteFindings(card({ footnote: ["provisional: fewer than 7 days of history"] }));
    expect(found.length).toBe(1);
    expect(found[0]).toMatch(/drawdown/);
  });

  it("does not hold an unranked trader card to a duty it does not have", () => {
    expect(cardFootnoteFindings(card({ ranked: false, footnote: [] }))).toEqual([]);
  });

  it("judges a market card by its age and a board card by its rules line", () => {
    const market = card({ kind: "market", ranked: false, footnote: ["412500 volume 24h"] });
    expect(cardFootnoteFindings(market)[0]).toMatch(/age/);
    expect(cardFootnoteFindings({ ...market, footnote: ["odds as of 12m ago"] })).toEqual([]);
    const board = card({ kind: "leaderboard", ranked: false, footnote: [] });
    expect(cardFootnoteFindings(board)[0]).toMatch(/rules/);
  });

  it("builds an accessible name out of the content and not out of the slogan", () => {
    const alt = cardAlt(card());
    expect(alt).toContain("@surat_whale");
    expect(alt).toContain("realised 412000");
    expect(alt).toContain("drawdown 12400");
    expect(alt).toContain("Openout");
    // The footer is the brand line; an image whose alt text is a slogan tells a screen-reader user nothing.
    expect(alt).not.toContain("ranked on the trade and not the story");
  });

  it("hands the sharer the canonical URL and the qualifiers, not a tracking address bar", () => {
    const text = shareText(card(), "https://openout.app/trader/surat_whale");
    expect(text.split("\n").at(-1)).toBe("https://openout.app/trader/surat_whale");
    expect(text).toContain("drawdown 12400");
    expect(text).not.toContain("?utm");
  });

  it("refuses to call a card drawable without a title and a subtitle", () => {
    expect(cardIsDrawable(card())).toBe(true);
    expect(cardIsDrawable(card({ title: "" }))).toBe(false);
  });
});
