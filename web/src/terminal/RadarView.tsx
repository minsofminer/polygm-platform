"use client";
/**
 * D5 · the Wallet Radar screen.
 *
 * Four rankings over the same scan, and the four things the phase asks for around them:
 *
 *  * **the input is capped and says so** — ten markets, counted on screen, with a refusal when the eleventh is
 *    chosen rather than a silent drop;
 *  * **the cost is visible** — the quota line is the API's own numbers plus its own note, and a cached scan says
 *    it was free, because a user who does not know a scan is free will not run a second one;
 *  * **the gate is visible** — the profit ranking's exclusions are listed with their reasons;
 *  * **a row is actionable in one click** — track, follow, copy (dry-run) and open, from the same destination
 *    table the whale tracker uses.
 *
 * The list and the compact-card views hold the same fields: a card view that dropped the win rate's sample note
 * would be a second, more flattering rendering of one payload.
 */
import { useState } from "react";
import { t } from "@/i18n/terminal";
import { Number } from "@/num/Number";
import { microToCents } from "@/money/cents";
import { RefusalNotice } from "@/ui/RefusalNotice";
import {
  MAX_SCAN_MARKETS,
  labelSentence,
  matchedChips,
  moneyText,
  publishableLabels,
  quotaText,
  rankingMeta,
  rowActions,
  rowsFor,
  scanProblems,
  toggleMarket,
  unrankedNote,
  unrankedRows,
  winRateText,
} from "./radar";
import { useRadar } from "./useTerminal";
import type { RadarRow } from "./wire";

const ACTION_LABEL: Record<string, string> = {
  watchlist: t("terminal.radar.action.watchlist"),
  follow: t("terminal.radar.action.follow"),
  copy: t("terminal.radar.action.copy"),
  open: t("terminal.radar.action.open"),
};

export function RadarView({ markets }: { markets: { marketId: string; question: string }[] }) {
  const { result, busy, err, run } = useRadar();
  const [selected, setSelected] = useState<string[]>([]);
  const [ranking, setRanking] = useState("active");
  const [view, setView] = useState<"list" | "cards">("list");
  const [refused, setRefused] = useState<string | null>(null);

  const tabs = result?.rankingsMeta ?? [];
  const rows = rowsFor(result, ranking);
  const meta = rankingMeta(result, ranking);
  const problems = scanProblems(selected);

  const add = (marketId: string) => {
    const out = toggleMarket(selected, marketId);
    setSelected(out.next);
    setRefused(out.refused);
  };

  return (
    <div className="pgm-dossier">
      <header className="pgm-dossier__head">
        <h2>{t("terminal.radar.title")}</h2>
        <p className="pgm-whales__counts">{t("terminal.radar.rule", { cap: MAX_SCAN_MARKETS })}</p>
        <p className="pgm-whales__counts">{quotaText(result)}</p>
      </header>

      {err ? <RefusalNotice route="walletRadar" extra={err} /> : null}

      <section>
        <h3>{t("terminal.radar.markets")}</h3>
        <p className="pgm-whales__counts">{t("terminal.radar.picked", { n: selected.length, cap: MAX_SCAN_MARKETS })}</p>
        <select aria-label={t("terminal.radar.addMarket")} onChange={(e) => add(e.target.value)} value="">
          <option value="">{t("terminal.radar.addMarket")}</option>
          {markets.map((m) => (
            <option key={m.marketId} value={m.marketId}>
              {m.question || m.marketId}
            </option>
          ))}
        </select>
        <ul className="pgm-dossier__badges">
          {selected.map((id) => (
            <li className="pgm-badge" key={id}>
              {markets.find((m) => m.marketId === id)?.question ?? id}
              <button type="button" aria-label={t("terminal.radar.remove", { what: id })} onClick={() => add(id)}>
                ×
              </button>
            </li>
          ))}
        </ul>
        {refused ? <p className="pgm-whales__counts" role="status">{refused}</p> : null}
        {problems.length > 0 ? <p className="pgm-whales__counts">{problems[0]}</p> : null}
        <button disabled={busy || problems.length > 0} onClick={() => void run(selected)} type="button">
          {busy ? t("terminal.radar.scanning") : t("terminal.radar.scan")}
        </button>
      </section>

      <section>
        <div className="pgm-whales__modes" role="tablist" aria-label={t("terminal.radar.rankings")}>
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={ranking === tab.id}
              className={ranking === tab.id ? "is-active" : ""}
              onClick={() => setRanking(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>
        {/* The ranking's own question, from the API: what this list is FOR, stated above it. */}
        {meta ? <p className="pgm-whales__counts">{meta.question}</p> : null}
        <div className="pgm-whales__modes">
          <button aria-pressed={view === "list"} onClick={() => setView("list")} type="button">
            {t("terminal.radar.viewList")}
          </button>
          <button aria-pressed={view === "cards"} onClick={() => setView("cards")} type="button">
            {t("terminal.radar.viewCards")}
          </button>
        </div>

        {result && rows.length === 0 ? <p role="status">{t("terminal.radar.empty")}</p> : null}
        {!result ? <p role="status">{t("terminal.radar.noScan")}</p> : null}

        {view === "list" && rows.length > 0 ? (
          <table className="pgm-table">
            <thead>
              <tr>
                <th>{t("terminal.radar.col.rank")}</th>
                <th>{t("terminal.radar.col.wallet")}</th>
                <th>{t("terminal.radar.col.matched")}</th>
                <th>{t("terminal.radar.col.bought")}</th>
                <th>{t("terminal.radar.col.sold")}</th>
                <th>{t("terminal.radar.col.realised")}</th>
                <th>{t("terminal.radar.col.winRate")}</th>
                <th>{t("terminal.radar.col.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.anonWallet}>
                  <td>{row.rank}</td>
                  <td>
                    <a href={`/trader/${encodeURIComponent(row.anonWallet)}`}>{row.anonWallet}</a>
                    <LabelList row={row} />
                  </td>
                  <td>
                    {matchedChips(row).map((c) => (
                      <a className="pgm-chip" href={`/market/${encodeURIComponent(c.marketId)}`} key={c.marketId}>
                        {c.label}
                      </a>
                    ))}
                  </td>
                  <td>
                    <Number kind="money" value={microToCents(row.boughtMicro)} />
                  </td>
                  <td>
                    <Number kind="money" value={microToCents(row.soldMicro)} />
                  </td>
                  <td className={row.realisedMicro < 0 ? "is-bad" : ""}>
                    <Number kind="pnl" value={microToCents(row.realisedMicro)} />
                  </td>
                  <td>
                    {winRateText(row)}
                    {row.insufficientSample ? <small className="pgm-whales__why"> {t("terminal.radar.gate")}</small> : null}
                  </td>
                  <td>
                    <Actions row={row} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}

        {view === "cards" && rows.length > 0 ? (
          <ul className="pgm-whales__feed">
            {rows.map((row) => (
              <li className="pgm-whales__row" key={row.anonWallet}>
                <span className="pgm-whales__ratio">{row.rank}</span>
                <span>
                  <a href={`/trader/${encodeURIComponent(row.anonWallet)}`}>{row.anonWallet}</a>
                  <LabelList row={row} />
                </span>
                <span className="pgm-whales__counts">{row.reason}</span>
                <span>
                  {moneyText(row.realisedMicro, true)} · {winRateText(row)}
                </span>
                <Actions row={row} />
              </li>
            ))}
          </ul>
        ) : null}

        {unrankedRows(result).length > 0 ? (
          <div>
            <p className="pgm-whales__counts">{unrankedNote(result)}</p>
            <ul className="pgm-dossier__methodology">
              {unrankedRows(result).map((u) => (
                <li key={u.anonWallet}>
                  <a href={`/trader/${encodeURIComponent(u.anonWallet)}`}>{u.anonWallet}</a>: {u.reason}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>

      {result ? <p className="pgm-whales__counts">{result.costNote}</p> : null}
    </div>
  );
}

/** A label is a rule and a disclaimer, as text. The `title` is convenience, and the text is the disclosure. */
function LabelList({ row }: { row: RadarRow }) {
  const labels = publishableLabels(row);
  if (labels.length === 0) return null;
  return (
    <ul className="pgm-dossier__badges">
      {labels.map((fact) => (
        <li className="pgm-badge" key={fact.label} title={labelSentence(fact)}>
          <strong>{fact.label}</strong>
          <small>{labelSentence(fact)}</small>
        </li>
      ))}
    </ul>
  );
}

function Actions({ row }: { row: RadarRow }) {
  return (
    <span className="pgm-whales__modes">
      {rowActions(row).map((action) => (
        <a href={action.href} key={action.id} title={action.note}>
          {ACTION_LABEL[action.id] ?? action.id}
        </a>
      ))}
    </span>
  );
}
