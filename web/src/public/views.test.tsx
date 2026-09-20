/**
 * The three public pages, rendered.
 *
 * These are the assertions about what a stranger — and a crawler, which runs no JavaScript — actually gets. The
 * ones that matter most:
 *
 *  1. **No address, anywhere in the DOM.** Every payload is grepped for a `0x…` shape after rendering, the same
 *     check the API's own tests run on the wire, because the last place a pseudonymisation scheme leaks is a
 *     component that helpfully renders a field it should not have been given.
 *  2. **A gated win rate is not a number and not an empty cell.** It is the API's sentence.
 *  3. **The board's rules come before its rows**, in document order — that ordering is the whole argument for
 *     why rank 47 can sit above rank 12, and a rules block below the table is a rules block nobody reads.
 *  4. **The qualifiers survive into the card AND the alerts**: a card that lost its drawdown renders the finding.
 */
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { BoardView } from "./BoardView";
import { MarketPublicView } from "./MarketView";
import { TraderView } from "./TraderView";
import type { PublicCard, PublicLeaderboardPage, PublicMarketPage, PublicTraderPage } from "./wire";

const now = Date.UTC(2026, 8, 20, 12, 0, 0);
const stamp = { asOf: now - 4_000, staleAfter: now + 26_000, serverAsOf: now - 3_000 };

const card: PublicCard = {
  kind: "trader",
  title: "@deep_book",
  subtitle: "rank #47 of 64 on the risk-adjusted board",
  lines: [{ label: "realised", value: "41200" }, { label: "settled markets", value: "31" },
          { label: "win rate", value: "83.33%" }],
  footnote: ["provisional: fewer than 7 days of history", "drawdown 1200"],
  brand: "Openout",
  footer: "openout.app — prediction markets, ranked on the trade and not the story",
  url: "https://openout.app/trader/deep_book",
  ranked: true,
  provisional: true,          // 5 days of history: the page is provisional and the card owes its reader the line
} as PublicCard;

const trader = {
  ...stamp,
  handle: "deep_book",
  anon: "w_57c7dcc029",
  url: "https://openout.app/trader/deep_book",
  robots: "noindex, follow",
  standing: [
    { board: "risk_adjusted", label: "Risk-adjusted PnL", category: "", state: "ranked", rank: 47, rankedTotal: 64,
      rankBadge: { rank: 47, rankedTotal: 64, text: "#47" }, url: "https://openout.app/leaderboard/risk_adjusted" },
    { board: "category", label: "Category specialist", category: "Sports", state: "ranked", rank: 2, rankedTotal: 9,
      rankBadge: { rank: 2, rankedTotal: 9, text: "#2" }, url: "https://openout.app/leaderboard/category/c/sports" },
  ],
  headline: { board: "risk_adjusted", window: "30d", rank: 47, rankedTotal: 64, settledMarkets: 31,
              winRateBps: 8_333, realisedMicro: 41_200_000_000, realised: "41200",
              maxDrawdownMicro: 1_200_000_000, maxDrawdown: "1200", state: "ranked", ageDays: 5 },
  notes: ["provisional: 5 days of history, nothing rankable before 7", "worst drawdown 1200"],
  card,
  cardKey: "abc123",
  structuredData: [{ "@context": "https://schema.org", "@type": "BreadcrumbList" },
                   { "@context": "https://schema.org", "@type": "ProfilePage" }],
  sampleGate: 20,
  links: { methodology: "/v1/leaderboard/methodology" },
  note: "this page is public because the trader listed a handle",
} as unknown as PublicTraderPage;

const market = {
  ...stamp,
  marketId: "0xM1",
  slug: "fed-cut-sept",
  url: "https://openout.app/market/fed-cut-sept",
  robots: "index, follow",
  question: "Will the Fed cut rates at the September meeting?",
  eventTitle: "",
  eventSlug: "",
  category: "Economics",
  acceptingOrders: true,
  endDate: now + 1_036_799_994,
  minimumTickSize: "0.01",
  minimumOrderSize: "5",
  feeType: "None",
  outcomes: [{ outcome: "Yes", winner: null }, { outcome: "No", winner: null }],
  resolutionCriteria: "Resolves YES if the FOMC statement announces a reduction.</script>",
  odds: { lastPriceMicro: 500_000, lastPrice: "0.5", volume24hMicro: 412_500_000_000, volume24h: "412500",
          volume7d: "2887500", liquidity: "1820000", openInterest: "1284000", ageMs: 978,
          ageText: "seconds ago", quotedFrom: "market_activity.last_price_micro" },
  quoteNote: "the price is the last trade our ingest recorded",
  card: { ...card, kind: "market", ranked: false, title: "Will the Fed cut rates?",
          footnote: ["odds as of seconds ago", "412500 volume 24h"] } as PublicCard,
  cardKey: "mk1",
  structuredData: [{ "@type": "BreadcrumbList" }],
  links: { terminal: "/market/0xM1" },
} as unknown as PublicMarketPage;

const board = {
  ...stamp,
  board: "risk_adjusted",
  label: "Risk-adjusted PnL",
  url: "https://openout.app/leaderboard/risk_adjusted",
  robots: "index, follow",
  window: "30d",
  category: "",
  formula: "net realised after fees excluding the single best market, per unit of risk",
  gate: "20 settled markets in the window AND $500 verified lifetime turnover",
  tieBreaks: "score, then settled markets, then smaller drawdown",
  cadenceMs: 3_600_000,
  rowCount: 1,
  rankedTotal: 64,
  excludedTotal: 3,
  blewUpCount: 2,
  provisionalCount: 11,
  rows: [{ rank: 47, anon: "w_57c7dcc029", handle: "deep_book", state: "ranked", settledMarkets: 31,
           labels: [], sampleNote: "", insufficientSample: false, realised: "41200",
           drawdown: "1200", maxDrawdownMicro: 1_200_000_000, winRate: "83.33%", scoreText: "5625 bps",
           rankBadge: { rank: 47, rankedTotal: 64, text: "#47" } }],
  notes: ["the rows are the top 25 of 64"],
  note: "a board ranks wallets by one formula",
  card: { ...card, kind: "leaderboard", ranked: false, title: "Risk-adjusted PnL",
          footnote: ["the rows are the top 25 of 64"] } as PublicCard,
  cardKey: "bd1",
  structuredData: [{ "@type": "ItemList" }],
  links: { methodology: "/v1/leaderboard/methodology" },
} as unknown as PublicLeaderboardPage;

function assertNoAddress(html: string) {
  const hits = html.match(/0x[0-9a-fA-F]{6,}/g) ?? [];
  expect(hits).toEqual([]);
}

describe("the public trader page", () => {
  it("renders the rank, every standing, the qualifiers and no address", () => {
    const { container } = render(<TraderView page={trader} now={now} />);
    const html = container.innerHTML;
    expect(html).toContain("@deep_book");
    expect(html).toContain("#47");
    expect(html).toContain("Sports");
    expect(html).toContain("provisional: 5 days of history");
    expect(html).toContain("drawdown 1200");
    expect(html).toContain("noindex, follow");
    assertNoAddress(html);
  });

  it("says a win rate is behind the gate rather than printing a number or a blank", () => {
    const gated = { ...trader, headline: { ...trader.headline, winRateBps: null } } as PublicTraderPage;
    const { container } = render(<TraderView page={gated} now={now} />);
    expect(container.textContent).toContain("behind the sample gate");
  });

  it("emits a breadcrumb whose names are the trail it rendered, not the API's own words", () => {
    const { container } = render(<TraderView page={trader} now={now} />);
    const rendered = [...container.querySelectorAll("nav.pgm-public__crumbs a, nav.pgm-public__crumbs span")]
      .map((el) => el.textContent?.trim() ?? "");
    const script = container.querySelector("script[type='application/ld+json']");
    const graph = JSON.parse(script?.textContent ?? "[]") as { "@type"?: string;
      itemListElement?: { name: string }[] }[];
    const crumbs = graph.find((g) => g["@type"] === "BreadcrumbList")?.itemListElement ?? [];
    expect(crumbs.length).toBeGreaterThan(0);
    // The visible trail's last step is the page itself and is rendered as a `span`, the earlier ones as links;
    // either way the label a reader sees is the label the markup states.
    expect(crumbs.map((c) => c.name)).toEqual(rendered.filter((x) => x.length > 0));
  });

  it("shows the finding on the page when a card lost a qualifier the product refuses to drop", () => {
    const broken = { ...trader, card: { ...card, footnote: ["drawdown 1200"] } } as PublicTraderPage;
    const { getByRole } = render(<TraderView page={broken} now={now} />);
    expect(getByRole("alert").textContent).toMatch(/provisional/);
  });
});

describe("the public market page", () => {
  it("prints the odds with their age and the resolution text as text", () => {
    const { container } = render(<MarketPublicView page={market} now={now} />);
    const html = container.innerHTML;
    expect(container.textContent).toContain("0.50");
    expect(container.textContent).toContain("seconds ago");
    expect(container.textContent).toContain("the price is the last trade our ingest recorded");
    // The upstream sentence arrives with a `</script>` in it (the fixture is a real-world worst case) and has to
    // stay text: no injected element, no broken document.
    expect(container.querySelectorAll("script[type='text/x-injected']").length).toBe(0);
    expect(html).toContain("Resolves YES if the FOMC statement announces a reduction.");
    assertNoAddress(html);
  });
});

describe("the public board page", () => {
  it("puts the rules before the rows", () => {
    const { container } = render(<BoardView page={board} />);
    const rules = container.querySelector("#rules");
    const table = container.querySelector("table");
    expect(rules).not.toBeNull();
    expect(table).not.toBeNull();
    // `compareDocumentPosition` bit 4 = "follows": the table follows the rules in document order.
    expect(rules!.compareDocumentPosition(table!) & 4).toBe(4);
  });

  it("states the board's excluded, blown-up and provisional counts beside it", () => {
    const { container } = render(<BoardView page={board} />);
    const text = container.textContent ?? "";
    expect(text).toContain("3 wallets are excluded");
    expect(text).toContain("2 wallets on this board lost more than they deposited");
    expect(text).toContain("11 wallets are inside their first week");
  });

  it("renders a row's own cells and no address", () => {
    const { container } = render(<BoardView page={board} />);
    const row = container.querySelector("tr[data-rank='47']");
    expect(row?.textContent).toContain("#47");
    expect(row?.textContent).toContain("deep_book");
    expect(row?.textContent).toContain("83.33%");
    assertNoAddress(container.innerHTML);
  });
});
