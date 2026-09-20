/**
 * `/leaderboard/<board>` — the board as a page, with its rules above the rows.
 *
 * The kit's acceptance criterion for this phase is a sentence: *explain why rank 47, with fewer resolved
 * markets, sits above rank 12*. That sentence is answerable here without reading our source, because the page
 * prints, before the first row: the formula (`formula`), who is eligible (`gate`), how ties break
 * (`tieBreaks`), and the per-row sample that decides how much a win rate is worth (`settledMarkets`,
 * `sampleNote`). A ranking page that prints only the ranking asks the reader to trust it.
 *
 * The integrity counts are shown for the same reason they are counted: excluded wallets, blown-up wallets and
 * provisional wallets are part of what the board IS, and a board that reports its own exclusions is a board
 * whose numbers a sceptic can check against its own summary.
 */
import { t } from "@/i18n/public";
import { JsonLd, type Crumb } from "./JsonLd";
import { PublicChrome } from "./Chrome";
import { ShareCard } from "./ShareCard";
import { boardFindings, rowCells, rowNotes } from "./rows";
import type { PublicLeaderboardPage } from "./wire";

export function BoardView({ page }: { page: PublicLeaderboardPage }) {
  const findings = boardFindings(page);
  const trail: Crumb[] = [
    { label: "Openout", href: "/" },
    { label: t("public.chrome.backToBoard"), href: "/leaderboard/risk_adjusted" },
    { label: [page.label || page.board, page.category].filter(Boolean).join(" · ") },
  ];
  return (
    <PublicChrome
      crumbs={trail}
      methodologyHref="#rules"
      footer={
        <p className="pgm-public__note">
          {page.note} · {page.rowCount} of {page.rankedTotal}
        </p>
      }
    >
      <header className="pgm-public__head">
        <h1>{[page.label || page.board, page.category].filter(Boolean).join(" · ")}</h1>
        <p className="pgm-public__lede">{t("public.board.window", { window: page.window || "all time" })}</p>
      </header>

      <section id="rules" className="pgm-public__rules" aria-label={t("public.chrome.methodology")}>
        <dl>
          <div>
            <dt>{t("public.board.formula")}</dt>
            <dd>{page.formula}</dd>
          </div>
          <div>
            <dt>{t("public.board.gate")}</dt>
            <dd>{page.gate}</dd>
          </div>
          <div>
            <dt>{t("public.board.tieBreaks")}</dt>
            <dd>{page.tieBreaks}</dd>
          </div>
        </dl>
        {findings.length ? (
          <ul className="pgm-public__findings">
            {findings.map((f) => (
              <li key={f}>{f}</li>
            ))}
          </ul>
        ) : null}
      </section>

      {page.rows.length === 0 ? (
        <p className="pgm-public__state" role="status">
          {t("public.board.empty")}
        </p>
      ) : (
        <table className="pgm-public__board">
          <thead>
            <tr>
              {rowCells(page.rows[0]!, page.board).map((cell) => (
                <th key={cell.key} scope="col">
                  {cell.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {page.rows.map((row) => {
              const notes = rowNotes(row);
              return (
                <tr key={String(row.anon)} data-rank={String(row.rank)}>
                  {rowCells(row, page.board).map((cell) => (
                    <td key={cell.key} className={cell.bad ? "is-bad" : cell.wide ? "is-wide" : ""}>
                      {cell.key === "rank" ? <span className="pgm-public__badge">{`#${cell.value}`}</span> : cell.value}
                      {/* The row's own qualifiers, inside the row: a wash finding or a sample note belongs to the
                          number beside it, not to a legend at the bottom of the page. */}
                      {cell.key === "trader" && notes.length ? (
                        <ul className="pgm-public__rownotes">
                          {notes.map((n) => (
                            <li key={n}>{n}</li>
                          ))}
                        </ul>
                      ) : null}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <ShareCard card={page.card} url={page.url} />
      <JsonLd graph={page.structuredData} trail={trail} self={page.url} />
    </PublicChrome>
  );
}
