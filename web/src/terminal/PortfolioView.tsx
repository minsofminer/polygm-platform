"use client";
/**
 * D6 · the portfolio screen.
 *
 * The order of the page is the order in which a loss becomes visible: what I hold (with the marks we do not
 * have stated as missing), the events where my legs are one bet, the curve with its drawdown and its benchmark,
 * the order history with the rows we could not resolve kept in place, and the export built from the same
 * payload. There is no summary tile anywhere above that can be read on its own: every headline number is a row
 * in the totals block, which is where the drawdown is too.
 *
 * The screen renders the wire's own sentences (the benchmark's note, the unresolved row's reason) instead of
 * composing its own from the same fields: two renderings of one rule is how a screen and its own footnote
 * end up disagreeing.
 */
import { useMemo, useState } from "react";
import { t } from "@/i18n/t";
import { freshnessOf } from "@/api/envelope";
import { microToCents } from "@/money/cents";
import { Number } from "@/num/Number";
import { StaleIndicator } from "@/num/StaleIndicator";
import { RefusalNotice } from "@/ui/RefusalNotice";
import { priceCell } from "./dossier";
import { useNow, usePortfolio } from "./useTerminal";
import {
  benchmarkLabel,
  csvFilename,
  csvText,
  curveGeometry,
  curveLabel,
  emptyTarget,
  exitHref,
  endsInText,
  exposureNote,
  hasPositions,
  markText,
  moneyText,
  orderRows,
  shareText,
  totalRows,
  unresolvedText,
  unrealisedKnown,
} from "./portfolio";
import type { Portfolio, PortfolioPosition } from "./wire";

/** Every literal key this screen asks for, in one place: a `t()` with a computed key fails the i18n check. */
const TOTAL_LABEL: Record<string, string> = {
  value: t("terminal.portfolio.total.value"),
  costBasis: t("terminal.portfolio.total.costBasis"),
  unrealised: t("terminal.portfolio.total.unrealised"),
  cash: t("terminal.portfolio.total.cash"),
  equity: t("terminal.portfolio.total.equity"),
  drawdown: t("terminal.portfolio.total.drawdown"),
};

const TOTAL_RULE: Record<string, string> = {
  value: t("terminal.portfolio.total.valueRule"),
  costBasis: t("terminal.portfolio.total.costBasisRule"),
  unrealised: t("terminal.portfolio.total.unrealisedRule"),
  cash: t("terminal.portfolio.total.cashRule"),
  equity: t("terminal.portfolio.total.equityRule"),
  drawdown: t("terminal.portfolio.total.drawdownRule"),
};

export function PortfolioView({ initial = null }: { initial?: Portfolio | null }) {
  const { data, err, refresh } = usePortfolio();
  const now = useNow(1_000);
  // The server read is the first paint: a portfolio that renders blank until a fetch lands is a portfolio a
  // user sees for a second every time they open it.
  const book = data ?? initial;
  const [copied, setCopied] = useState(false);

  // The stamp travels with the payload (asOf/staleAfter), so this surface reports the data's clock and not the
  // answer's — the same rule as every other read in the terminal.
  const freshness = useMemo(
    // The portfolio read carries no ttlMs (the API stamps it ttl 0: a portfolio is never cached), and `Stamp`
    // asks for one, so it is stated here rather than invented by the envelope.
    () => (book ? freshnessOf({ asOf: book.asOf, staleAfter: book.staleAfter, ttlMs: 0 }, now) : "unknown"),
    [book, now],
  );
  const age = book ? now - book.asOf : null;

  if (err && !book) {
    return <RefusalNotice route="portfolio" extra={err} />;
  }
  if (!book) {
    return <p className="pgm-whales__counts" role="status">{t("terminal.portfolio.loading")}</p>;
  }

  const download = () => {
    // The file is built from the payload on screen, and the label says so; a CSV fetched separately is a CSV
    // that can disagree with the table it claims to export.
    const blob = new Blob([csvText(book)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = csvFilename(now);
    a.click();
    URL.revokeObjectURL(url);
    setCopied(true);
  };

  return (
    <div className="pgm-dossier">
      <header className="pgm-dossier__head">
        <h2>{t("terminal.portfolio.title")}</h2>
        <StaleIndicator freshness={freshness} ageMs={age} />
        <button className="pgm-terminal__collapse" onClick={() => void refresh()} type="button">
          {t("terminal.portfolio.refresh")}
        </button>
      </header>

      {!hasPositions(book) ? (
        <p className="pgm-whales__counts" role="status">
          {t("terminal.portfolio.empty")} <a href={emptyTarget(book)}>{t("terminal.portfolio.emptyLink")}</a>
        </p>
      ) : null}

      <dl className="pgm-dossier__metrics">
        {totalRows(book.totals, book.maxDrawdownMicro).map((row) => (
          <div className={`pgm-dossier__metric ${row.id === "drawdown" ? "is-bad" : ""}`} key={row.id}>
            <dt>
              {TOTAL_LABEL[row.id]}
              <small>{TOTAL_RULE[row.id]}</small>
            </dt>
            <dd>
              <Number kind={row.kind} value={microToCents(row.micro)} freshness={freshness} staleMs={(now - book.asOf) / 1_000} />
            </dd>
          </div>
        ))}
      </dl>

      <section className="pgm-dossier__curve">
        <h3>{t("terminal.portfolio.curveTitle")}</h3>
        <p className="pgm-whales__counts">{curveLabel()}</p>
        <Curve book={book} />
        <p className="pgm-whales__counts">{benchmarkLabel(book)}</p>
        <p className="pgm-dossier__disclaimers">{t("terminal.portfolio.unrealisedNotInCurve")}</p>
      </section>

      <section>
        <h3>{t("terminal.portfolio.positionsTitle")}</h3>
        <table className="pgm-table">
          <thead>
            <tr>
              <th>{t("terminal.portfolio.col.market")}</th>
              <th>{t("terminal.portfolio.col.outcome")}</th>
              <th>{t("terminal.portfolio.col.size")}</th>
              <th>{t("terminal.portfolio.col.avgEntry")}</th>
              <th>{t("terminal.portfolio.col.mark")}</th>
              <th>{t("terminal.portfolio.col.unrealised")}</th>
              <th>{t("terminal.portfolio.col.share")}</th>
              <th>{t("terminal.portfolio.col.endsIn")}</th>
              <th>{t("terminal.portfolio.col.exit")}</th>
            </tr>
          </thead>
          <tbody>
            {book.positions.map((p) => (
              <PositionRow key={p.tokenId} position={p} nowMs={now} freshness={freshness} />
            ))}
          </tbody>
        </table>
        {book.positions.length === 0 ? <p role="status">{t("terminal.portfolio.noPositions")}</p> : null}
        {/* The mark rule as text, under the table it governs. A rule that lives in a `title` is not a rule a
            reader has: the mark is the number both of the PnL columns are derived from. */}
        <p className="pgm-whales__counts">{t("terminal.portfolio.markRule")}</p>
      </section>

      {book.negRiskGroups.length > 0 ? (
        <section>
          <h3>{t("terminal.portfolio.eventsTitle")}</h3>
          <p className="pgm-whales__counts">{t("terminal.portfolio.eventsRule")}</p>
          <ul className="pgm-dossier__badges">
            {book.negRiskGroups.map((g) => (
              <li className="pgm-badge" key={g.eventId}>
                <a href={`/event/${encodeURIComponent(g.eventId)}`}>{g.eventTitle}</a>
                {/* The exposure is the sentence, not a second chart: this is the number that changes the risk. */}
                <small>{exposureNote(g)}</small>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section>
        <h3>{t("terminal.portfolio.ordersTitle")}</h3>
        <table className="pgm-table">
          <thead>
            <tr>
              <th>{t("terminal.portfolio.col.at")}</th>
              <th>{t("terminal.portfolio.col.state")}</th>
              <th>{t("terminal.portfolio.col.size")}</th>
              <th>{t("terminal.portfolio.col.price")}</th>
              <th>{t("terminal.portfolio.col.notional")}</th>
            </tr>
          </thead>
          <tbody>
            {orderRows(book).map((row) => (
              <tr className={row.unresolved ? "is-unknown" : ""} key={`${row.unresolved ? "u" : "o"}-${row.id}`}>
                <td>{new Date(row.atMs).toISOString().slice(0, 16).replace("T", " ")}</td>
                <td>
                  {row.state}
                  {row.unresolved ? (
                    // Visible text, not a tooltip: an unresolved row is the one a user must not misread.
                    <small className="pgm-whales__why"> {unresolvedText(row)}</small>
                  ) : null}
                </td>
                <td>{row.shares || "—"}</td>
                <td>{row.price || "—"}</td>
                <td>{row.notionalMicro ? <Number kind="money" value={microToCents(row.notionalMicro)} /> : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section>
        <h3>{t("terminal.portfolio.exportTitle")}</h3>
        <p className="pgm-whales__counts">{book.csv.note}</p>
        <p className="pgm-dossier__disclaimers">{t("terminal.portfolio.exportTax")}</p>
        <button onClick={download} type="button">
          {t("terminal.portfolio.exportButton")}
        </button>
        {copied ? <span role="status"> {t("terminal.portfolio.exportStarted")}</span> : null}
      </section>
    </div>
  );
}

function PositionRow({ position, nowMs, freshness }: { position: PortfolioPosition; nowMs: number; freshness: ReturnType<typeof freshnessOf> }) {
  const mark = markText(position);
  const known = unrealisedKnown(position);
  return (
    <tr>
      <td>
        <a href={`/market/${encodeURIComponent(position.marketId)}`}>{position.question || position.marketId}</a>
      </td>
      <td>{position.outcome}</td>
      <td>{position.size}</td>
      <td>
        <Number kind="price" value={priceCell(position.avgEntry, "0.01")} tick="0.01" />
      </td>
      <td>
        {known ? <Number kind="price" value={priceCell(position.mark, "0.01")} tick="0.01" freshness={freshness} /> : null}
        {/* The rule travels with the number, on every row: a missing mark is a fact about the market, not a
            bug in the screen, and "no mark" without the reason reads as an unfinished page. The `title` is
            the same sentence for a pointer, and the text is the disclosure. */}
        <small className="pgm-whales__why" title={mark.rule}> {known ? mark.text : `${mark.text} — ${mark.rule}`}</small>
      </td>
      <td className={known ? (position.unrealisedMicro < 0 ? "is-bad" : "is-good") : ""}>
        {known ? (
          <>
            <Number kind="pnl" value={microToCents(position.unrealisedMicro)} />{" "}
            <small>
              ({position.unrealisedBps > 0 ? "+" : ""}
              {shareText(position.unrealisedBps)})
            </small>
          </>
        ) : (
          // Not 0%, and not "—" either: the sentence says why there is no percentage.
          <small className="pgm-whales__why">{t("terminal.portfolio.unrealisedUnknown")}</small>
        )}
      </td>
      <td>{shareText(position.shareOfPortfolioBps)}</td>
      <td>{endsInText(position.endsInMs, nowMs)}</td>
      <td>
        <a href={exitHref(position)} title={t("terminal.portfolio.exitRule")}>
          {t("terminal.portfolio.exitLink", { size: position.size, price: position.mark })}
        </a>
      </td>
    </tr>
  );
}

/** The curve: cash, its drawdown region, and the benchmark line with its own label. */
function Curve({ book }: { book: Portfolio }) {
  const geo = curveGeometry(book.pnlCurve, book.benchmark.valueMicro > 0 ? book.benchmark.valueMicro : null);
  if (book.pnlCurve.length === 0) {
    return <p role="status">{t("terminal.portfolio.curveEmpty")}</p>;
  }
  return (
    <svg aria-label={t("terminal.portfolio.curveAria")} role="img" viewBox="0 0 640 170">
      <path className="pgm-chart-drawdown" d={geo.drawdown} />
      <path className="pgm-chart-line" d={geo.line} />
      {geo.zeroY !== null ? <line className="pgm-chart-zero" x1="0" x2="640" y1={geo.zeroY} y2={geo.zeroY} /> : null}
      {geo.benchmarkY !== null ? (
        <line className="pgm-chart-zero" x1="0" x2="640" y1={geo.benchmarkY} y2={geo.benchmarkY} strokeDasharray="6 3" />
      ) : null}
    </svg>
  );
}

/** Kept for the route's server-side first paint and for tests. */
export function portfolioSummaryText(book: Portfolio): string {
  return moneyText(book.totals.equityMicro);
}
