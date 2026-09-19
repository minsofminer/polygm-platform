import { BoardPanel } from "@/terminal/BoardPanel";
import { t } from "@/i18n/terminal";

export const dynamic = "force-dynamic";

/**
 * D3 · the rating board, inside the app shell.
 *
 * A route of its own rather than a panel bolted onto `/terminal`: the board is read by people who are not trading
 * at that moment, and the terminal's three columns are a trading layout. The panel takes the wallet to focus as a
 * prop — D4 pins the account's own standing here, and a page that had already hard-coded "your wallet" would have
 * to be unpicked to add it.
 */
export default async function LeaderboardPage({ searchParams }: { searchParams: Promise<{ anon?: string }> }) {
  const { anon } = await searchParams;
  return (
    <main className="pgm-page">
      <h1>{t("terminal.board.title")}</h1>
      <BoardPanel focus={anon ?? ""} />
    </main>
  );
}
