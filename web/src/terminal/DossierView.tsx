"use client";
/**
 * D3 · the trader dossier screen.
 *
 * The arrangement is the argument: the header says who this is (and what we do not know about them), the
 * switcher says which window every number below belongs to, the metric grid puts the drawdown next to the PnL it
 * shadows, the curve draws the overlay, and only then do the trading history and the behaviour labels appear —
 * each with the rule that produced it. A reader who stops after the first screen-width has still seen the win
 * rate's sample, the drawdown, and the fact that a label is a rule and not a verdict.
 *
 * The screen renders the SERVER's sentences (threshold rules, sample notes, warnings) rather than composing its
 * own from the same fields: two renderings of one rule is how a tooltip and a badge end up disagreeing.
 */
import { useCallback, useMemo, useState } from "react";
import { t } from "@/i18n/terminal";
import { freshnessOf } from "@/api/envelope";
import { microToCents } from "@/money/cents";
import { Number } from "@/num/Number";
import { StaleIndicator } from "@/num/StaleIndicator";
import { Button } from "@/ui/Button";
import { RefusalNotice } from "@/ui/RefusalNotice";
import { useDossier, useNow } from "./useTerminal";
import {
  DEFAULT_WINDOW,
  EMPTY_HISTORY_FILTER,
  accountAgeText,
  behaviourRows,
  confidenceText,
  curvePoints,
  drawdownSentence,
  fillVerdict,
  filterHistory,
  historyMarkets,
  holdText,
  identityNote,
  labelSentence,
  marketHref,
  metricRows,
  methodologyLines,
  priceCell,
  shareText,
  windowChoices,
} from "./dossier";

/**
 * The three literal tables this screen needs, instead of computed keys.
 *
 * `t()` with a template argument is a build failure in this repo (the check cannot verify what it cannot read),
 * and the key shape forbids a segment that starts with a digit — so `terminal.dossier.window.7d` was wrong twice.
 * These tables are the fix: every key is literal, every key is checkable, and the window names read as what they
 * select ("7 days") rather than as a wire value.
 */
const WINDOW_LABEL: Record<string, string> = {
  "7d": t("terminal.dossier.window7"),
  "30d": t("terminal.dossier.window30"),
  "90d": t("terminal.dossier.window90"),
  all: t("terminal.dossier.windowAll"),
};

const METRIC_LABEL: Record<string, string> = {
  volume: t("terminal.dossier.metric.volume"),
  realised: t("terminal.dossier.metric.realised"),
  unrealised: t("terminal.dossier.metric.unrealised"),
  winRate: t("terminal.dossier.metric.winRate"),
  maxDrawdown: t("terminal.dossier.metric.maxDrawdown"),
  avgHold: t("terminal.dossier.metric.avgHold"),
  best: t("terminal.dossier.metric.best"),
  worst: t("terminal.dossier.metric.worst"),
  fills: t("terminal.dossier.metric.fills"),
  unavailable: t("terminal.dossier.metric.unavailable"),
};

const SIDE_LABEL: Record<string, string> = {
  BUY: t("terminal.dossier.buy"),
  SELL: t("terminal.dossier.sell"),
};

export function windowLabel(key: string): string {
  return WINDOW_LABEL[key] ?? key;
}

export type DossierInitial = {
  anonWallet: string;
  stamp?: { asOf?: number; staleAfter?: number } | null;
};

export function TraderDossierView({ anon, initial }: { anon: string; initial?: DossierInitial | null }) {
  const [windowKey, setWindowKey] = useState<string>(DEFAULT_WINDOW);
  const [filter, setFilter] = useState(EMPTY_HISTORY_FILTER);
  const [copied, setCopied] = useState(false);
  const { data, loading, err, stamp, reload } = useDossier(anon, windowKey);
  const now = useNow(1_000);

  // The switcher recomputes the whole set: `rows` is derived from the selected window alone, so no number on
  // the screen can come from a window the user is not looking at.
  const rows = useMemo(() => metricRows(data?.metrics?.[windowKey], data?.sampleGate ?? 20), [data, windowKey]);
  const choices = useMemo(() => windowChoices({ windows: data?.windows ?? [] }), [data]);
  const history = useMemo(() => filterHistory(data?.fills ?? [], filter), [data, filter]);
  const markets = useMemo(() => historyMarkets(data?.fills ?? []), [data]);
  const boxes = useMemo(
    () => behaviourRows(data?.behaviour),
    [data],
  );
  const geometry = useMemo(() => curvePoints(data?.curve ?? [], 640, 160), [data]);
  const fresh = freshnessOf(stamp, now);
  const identity = useMemo(() => (data ? accountAgeText(firstFill(data), now) : null), [data, now]);

  const copyPseudonym = useCallback(async () => {
    const text = data?.anonWallet ?? anon;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // A clipboard that refuses (insecure context, denied permission) must not look like a success: the button
      // says what happened, and the pseudonym is selectable in the meantime.
      setCopied(false);
    }
  }, [anon, data]);

  if (err && !data) {
    return (
      <section className="pgm-dossier" aria-label={t("terminal.dossier.label")}>
        <p role="status" className="pgm-dossier__error">{err}</p>
        <Button onClick={reload}>{t("terminal.dossier.retry")}</Button>
      </section>
    );
  }
  if (!data) {
    return (
      <section className="pgm-dossier" aria-label={t("terminal.dossier.label")}>
        <p role="status">{loading ? t("terminal.dossier.loading") : t("terminal.dossier.empty")}</p>
      </section>
    );
  }

  return (
    <section className="pgm-dossier" aria-label={t("terminal.dossier.label")}>
      <header className="pgm-dossier__head">
        <div className="pgm-dossier__id">
          <h1>{data.anonWallet}</h1>
          <p className="pgm-dossier__note" title={identityNote(data.anonWallet)}>
            {identityNote(data.anonWallet)}
          </p>
          <p className="pgm-dossier__meta">
            <span>{t("terminal.dossier.accountAge")}: <strong>{identity?.value}</strong></span>
            <span title={identity?.note}>{identity?.note}</span>
          </p>
        </div>
        <div className="pgm-dossier__actions">
          <Button onClick={copyPseudonym}>{copied ? t("terminal.dossier.copied") : t("terminal.dossier.copy")}</Button>
          {/* Follow and copy live on the left rail and the copy screen; here they are entry points, and the copy
              one lands on the dry-run config with the source pre-filled. */}
          <a className="pgm-link" href={`/copy?source=${encodeURIComponent(data.anonWallet)}`}>{t("terminal.dossier.copyTrade")}</a>
        </div>
        <StaleIndicator freshness={fresh} ageMs={stamp ? Math.max(0, now - stamp.asOf) : null} />
      </header>

      {boxes.length ? (
        <ul className="pgm-dossier__badges" aria-label={t("terminal.dossier.behaviour")}>
          {boxes.map((fact) => (
            <li key={fact.label} className="pgm-badge">
              <strong>{fact.label}</strong>
              <span className="pgm-badge__conf">{confidenceText(fact.confidence)}</span>
              {/* The rule and the disclaimer, on the badge: a label that cannot be explained in a tooltip is a
                  label the user is expected to take on faith, which is the thing this phase forbids. */}
              <span className="pgm-badge__why" title={labelSentence(fact)}>{labelSentence(fact)}</span>
            </li>
          ))}
        </ul>
      ) : null}

      <nav className="pgm-dossier__windows" aria-label={t("terminal.dossier.window")}>
        {choices.map((choice) => (
          <button
            key={choice}
            type="button"
            aria-pressed={choice === windowKey}
            onClick={() => setWindowKey(choice)}
            className={choice === windowKey ? "is-active" : ""}
          >
            {windowLabel(choice)}
          </button>
        ))}
      </nav>

      <dl className="pgm-dossier__metrics">
        {rows.map((row) => (
          <div key={row.id} className={`pgm-dossier__metric is-${row.tone}`}>
            <dt>{METRIC_LABEL[row.id] ?? row.id}</dt>
            <dd>{row.value}{row.note ? <small>{row.note}</small> : null}</dd>
          </div>
        ))}
      </dl>

      <figure className="pgm-dossier__curve">
        <figcaption>
          <span>{t("terminal.dossier.curve", { window: windowLabel(data.curveWindow) })}</span>
          <span className="pgm-dossier__drawdown">{drawdownSentence(data.curve, data.maxDrawdownMicro)}</span>
        </figcaption>
        <svg viewBox="0 0 640 160" role="img" aria-label={t("terminal.dossier.curveLabel")} preserveAspectRatio="none">
          {/* The overlay first, so the PnL line reads on top of its own drawdown rather than under it. */}
          <polygon className="pgm-chart-drawdown" points={geometry.drawdown} />
          <polyline className="pgm-chart-line" points={geometry.pnl} />
          <line className="pgm-chart-zero" x1="0" y1={zeroY(geometry, 160)} x2="640" y2={zeroY(geometry, 160)} />
        </svg>
      </figure>

      <div className="pgm-dossier__grid">
        <section aria-label={t("terminal.dossier.breakdown")}>
          <h2>{t("terminal.dossier.breakdown")}</h2>
          <table className="pgm-table">
            <thead>
              <tr>
                <th scope="col">{t("terminal.dossier.category")}</th>
                <th scope="col">{t("terminal.dossier.share")}</th>
                <th scope="col">{t("terminal.dossier.realised")}</th>
              </tr>
            </thead>
            <tbody>
              {(data.breakdown ?? []).map((row) => (
                <tr key={row.category}>
                  <th scope="row">{row.category || t("terminal.dossier.uncategorised")}</th>
                  <td>{shareText(row.shareBps)}</td>
                  <td><Number kind="pnl" value={microToCents(row.realisedMicro)} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section aria-label={t("terminal.dossier.positions")}>
          <h2>{t("terminal.dossier.positions")}</h2>
          {data.positions.length === 0 ? (
            <p>{t("terminal.dossier.noPositions")}</p>
          ) : (
            <table className="pgm-table">
              <thead>
                <tr>
                  <th scope="col">{t("terminal.dossier.market")}</th>
                  <th scope="col">{t("terminal.dossier.outcome")}</th>
                  <th scope="col">{t("terminal.dossier.size")}</th>
                  <th scope="col">{t("terminal.dossier.mark")}</th>
                  <th scope="col">{t("terminal.dossier.unrealised")}</th>
                </tr>
              </thead>
              <tbody>
                {data.positions.map((p) => (
                  <tr key={`${p.marketId}-${p.outcome}`}>
                    <th scope="row"><a href={`/market/${encodeURIComponent(p.marketId)}`}>{p.question || p.marketId}</a></th>
                    <td>{p.outcome}</td>
                    <td>{p.size}</td>
                    {/* An empty mark is "no mark", and it is rendered as words rather than as 0: a zero price is a
                        claim about the world, an absent one is a claim about our data. */}
                    <td>{p.mark ? <Number kind="price" value={priceCell(p.mark, "0.01")} tick="0.01" freshness={fresh} /> : t("terminal.dossier.noMark")}</td>
                    <td><Number kind="pnl" value={microToCents(p.unrealisedMicro)} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>

      <section aria-label={t("terminal.dossier.history")}>
        <h2>{t("terminal.dossier.history")}</h2>
        <div className="pgm-dossier__filters" role="group" aria-label={t("terminal.dossier.historyFilters")}>
          <label>
            {t("terminal.dossier.side")}
            <select value={filter.side} onChange={(e) => setFilter({ ...filter, side: e.target.value as "" | "BUY" | "SELL" })}>
              <option value="">{t("terminal.dossier.any")}</option>
              <option value="BUY">{t("terminal.dossier.buy")}</option>
              <option value="SELL">{t("terminal.dossier.sell")}</option>
            </select>
          </label>
          <label>
            {t("terminal.dossier.market")}
            <select value={filter.market} onChange={(e) => setFilter({ ...filter, market: e.target.value })}>
              <option value="">{t("terminal.dossier.any")}</option>
              {markets.map((m) => (
                <option key={m.marketId} value={m.marketId}>{m.question || m.marketId}</option>
              ))}
            </select>
          </label>
          <label>
            <input type="checkbox" checked={filter.resolvedOnly} onChange={(e) => setFilter({ ...filter, resolvedOnly: e.target.checked })} />
            {t("terminal.dossier.resolvedOnly")}
          </label>
          <span className="pgm-dossier__count">{t("terminal.dossier.shown", { n: history.length })}</span>
        </div>
        <table className="pgm-table">
          <thead>
            <tr>
              <th scope="col">{t("terminal.dossier.time")}</th>
              <th scope="col">{t("terminal.dossier.market")}</th>
              <th scope="col">{t("terminal.dossier.side")}</th>
              <th scope="col">{t("terminal.dossier.outcome")}</th>
              <th scope="col">{t("terminal.dossier.price")}</th>
              <th scope="col">{t("terminal.dossier.notional")}</th>
              <th scope="col">{t("terminal.dossier.result")}</th>
            </tr>
          </thead>
          <tbody>
            {history.slice(0, 40).map((f) => {
              const verdict = fillVerdict(f);
              return (
                <tr key={`${f.tokenId}-${f.tsMs}-${f.side}`}>
                  <td title={new Date(f.tsMs).toISOString()}>{holdText(now - f.tsMs)}</td>
                  <th scope="row"><a href={marketHref(f)}>{f.question || f.marketId}</a></th>
                  <td className={`is-${f.side.toLowerCase()}`}>{SIDE_LABEL[f.side] ?? f.side}</td>
                  <td>{f.outcome}</td>
                  <td><Number kind="price" value={priceCell(f.price, f.tick)} tick={f.tick} freshness={fresh} /></td>
                  <td><Number kind="money" value={microToCents(f.notionalMicro)} /></td>
                  <td className={`is-${verdict.tone}`}>
                    {verdict.text}
                    {verdict.micro !== null ? <Number kind="pnl" value={microToCents(verdict.micro)} /> : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <section className="pgm-dossier__methodology" aria-label={t("terminal.dossier.methodology")}>
        <h2>{t("terminal.dossier.methodology")}</h2>
        <ul>
          {methodologyLines(data.methodology).map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        {boxes.length ? (
          <ul className="pgm-dossier__disclaimers">
            {boxes.map((fact) => (
              <li key={`d-${fact.label}`}><strong>{fact.label}</strong>: {fact.disclaimer}</li>
            ))}
          </ul>
        ) : null}
      </section>

      {err ? <RefusalNotice route="trader" extra={err} /> : null}
      {/* D3's integration: the dossier is where somebody lands from a leaderboard row, and the way back to the
          board is a link rather than a browser back button. It points at the panel with this wallet focused, so
          the standing opens on the trader the user was already reading about. */}
      <p className="pgm-dossier__board-link">
        <a href={`/leaderboard?anon=${encodeURIComponent(anon)}`}>{t("terminal.dossier.boardLink")}</a>
      </p>
    </section>
  );
}

/** The first fill we hold, used for the header's account age. Derived from the history the screen already has. */
function firstFill(data: { fills: { tsMs: number }[] }): number | null {
  if (!data.fills?.length) return null;
  const first = data.fills[0];
  if (!first) return null;
  return data.fills.reduce((min, f) => (f.tsMs < min ? f.tsMs : min), first.tsMs);
}

/** Where y=0 sits, so a curve that crosses zero says so. Outside the range it is clamped to the edge. */
function zeroY(geometry: { min: number; max: number }, height: number): number {
  const span = geometry.max - geometry.min || 1;
  return Math.max(0, Math.min(height, height - ((0 - geometry.min) / span) * height));
}
