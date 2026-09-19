"use client";
/**
 * D7 · copy trading: discovery, the config panel, and the monitor.
 *
 * The screen is ordered so that the two things the phase demands a user cannot miss come FIRST: how the list is
 * ranked (with the ranking stated in the API's own sentence, not in a tooltip), and what copying costs in
 * slippage before anything is confirmed. The config panel then refuses to be a surprise: its defaults are the
 * cautious ones, its validation is the API's own rules written where the user can still fix them, and going live
 * is blocked by evidence (dry-run history) rather than by a dialog.
 *
 * The monitor is honest about the two lists it merges: what the engine really did, and what it would have done.
 * A simulation is labelled `would`, never presented as a fill — the two live in separate API tables so this
 * screen cannot accidentally merge them into one row by forgetting a filter.
 */
import { useCallback, useMemo, useState } from "react";
import { t } from "@/i18n/terminal";
import { freshnessOf } from "@/api/envelope";
import { Number } from "@/num/Number";
import { StaleIndicator } from "@/num/StaleIndicator";
import { microToCents } from "@/money/cents";
import { RefusalNotice } from "@/ui/RefusalNotice";
import {
  DEFAULT_SORT,
  EMPTY_DRAFT,
  SORT_KEYS,
  type CopySort,
  type Draft,
  bpsText,
  discoveryRow,
  draftBody,
  draftProblems,
  dryRunHistory,
  goLiveBlockers,
  guardBody,
  monitorRows,
  pauseAllBodies,
  pauseBody,
  perSourceVerdict,
  slippageWarning,
  sourceRecord,
} from "./copy";
import { useCopyConfigs, useCopyMonitor, useCopySources, useNow, useSetCopyGuards } from "./useTerminal";
import type { CopyConfig } from "./wire";

/**
 * The sort options as LITERAL calls.
 *
 * `copy.ts` exports the key names, but the dictionary the check reads is built from literal arguments — the
 * table here is what makes each key checkable, and the order is the API's order.
 */
const SORT_LABEL: Record<CopySort, string> = {
  riskAdjusted: t("terminal.copy.sort.riskAdjusted"),
  netAfterFees: t("terminal.copy.sort.netAfterFees"),
  closedTrades: t("terminal.copy.sort.closedTrades"),
  drawdown: t("terminal.copy.sort.drawdown"),
};

const KIND_LABEL: Record<string, string> = {
  copied: t("terminal.copy.kind.copied"),
  skipped: t("terminal.copy.kind.skipped"),
  would: t("terminal.copy.kind.would"),
};

export function CopyView({ initialSource = "" }: { initialSource?: string }) {
  const [sort, setSort] = useState<CopySort>(DEFAULT_SORT);
  const [windowDays, setWindowDays] = useState(30);
  const sources = useCopySources(windowDays, sort);
  const { configs, refresh: refreshConfigs } = useCopyConfigs();
  const guards = useSetCopyGuards();
  const [draft, setDraft] = useState<Draft>({ ...EMPTY_DRAFT, sourceAnon: initialSource });
  const [selected, setSelected] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [created, setCreated] = useState<string | null>(null);
  const monitor = useCopyMonitor(selected);
  const now = useNow(1_000);

  const problems = useMemo(() => draftProblems(draft), [draft]);
  const previewSource = useMemo(
    () => sources.rows.find((r) => r.anonWallet === draft.sourceAnon) ?? null,
    [sources.rows, draft.sourceAnon],
  );
  const selectedConfig = configs.find((c) => c.configId === selected) ?? null;

  const set = useCallback(
    (field: keyof Draft) => (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
      setDraft((d) => ({ ...d, [field]: event.target.value })),
    [],
  );

  const submit = useCallback(async () => {
    if (problems.length > 0) return;
    const body = draftBody(draft);
    const out = await sources.create(body);
    if (!out.ok) return;
    const configId = String(out.id);
    // Creation makes a dry run; the guard call is what stores the knobs the create schema does not carry
    // (price bounds, TP/SL, the skip rules). Both are needed before the config is the one the user described.
    await guards.set(guardBody(draft, configId));
    setCreated(configId);
    setSelected(configId);
    await refreshConfigs();
  }, [draft, guards, problems.length, refreshConfigs, sources]);

  return (
    <div className="pgm-dossier">
      <header className="pgm-dossier__head">
        <h2>{t("terminal.copy.title")}</h2>
        <StaleIndicator freshness={sources.stamp ? freshnessOf(sources.stamp, now) : "unknown"} ageMs={sources.stamp ? now - sources.stamp.asOf : null} />
        {/* The ranking is the API's sentence. A screen that paraphrased it would be a second place for the
            list's own order to live, and the second place is the one that goes stale. */}
        <p className="pgm-whales__counts">{sources.ranking || t("terminal.copy.rankingFallback")}</p>
      </header>

      {sources.err ? <RefusalNotice route="copyConfigs" extra={sources.err} /> : null}

      <section>
        <h3>{t("terminal.copy.discoveryTitle")}</h3>
        <div className="pgm-whales__modes">
          <label>
            {t("terminal.copy.sortLabel")}
            <select aria-label={t("terminal.copy.sortLabel")} onChange={(e) => setSort(e.target.value as CopySort)} value={sort}>
              {SORT_KEYS.map((key) => (
                <option key={key} value={key}>
                  {SORT_LABEL[key]}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t("terminal.copy.window")}
            <select aria-label={t("terminal.copy.window")} // `Number` in this file is the number-layer COMPONENT, which shadows the global: parsing goes through
            // `window.Number` or through a helper, never through the shadowed name. This is the trap
            // `whales.ts` documented, met again.
            onChange={(e) => setWindowDays(window.Number.parseInt(e.target.value, 10))} value={windowDays}>
              <option value={7}>{t("terminal.copy.window7")}</option>
              <option value={30}>{t("terminal.copy.window30")}</option>
              <option value={90}>{t("terminal.copy.window90")}</option>
            </select>
          </label>
          <span className="pgm-whales__counts">{sources.sortNote}</span>
        </div>

        <table className="pgm-table">
          <thead>
            <tr>
              <th>{t("terminal.copy.col.rank")}</th>
              <th>{t("terminal.copy.col.source")}</th>
              <th>{t("terminal.copy.col.riskAdjusted")}</th>
              <th>{t("terminal.copy.col.net")}</th>
              <th>{t("terminal.copy.col.drawdown")}</th>
              <th>{t("terminal.copy.col.winRate")}</th>
              <th>{t("terminal.copy.col.closed")}</th>
              <th>{t("terminal.copy.col.copiers")}</th>
              <th>{t("terminal.copy.col.pick")}</th>
            </tr>
          </thead>
          <tbody>
            {sources.rows.map((raw) => {
              const row = discoveryRow(raw);
              return (
                <tr key={row.anonWallet}>
                  <td>{row.rank}</td>
                  <td>
                    <a href={`/trader/${encodeURIComponent(row.anonWallet)}`}>{row.anonWallet}</a>
                    {raw.currentlyCopying ? <small className="pgm-whales__why"> {t("terminal.copy.copyingTag")}</small> : null}
                  </td>
                  {/* The ratio comes first and its denominator sits beside it: a ratio whose divisor is not on
                      the row is a marketing number. */}
                  <td>
                    {row.riskText}
                    <small className="pgm-whales__why"> {t("terminal.copy.perDrawdown", { drawdown: row.drawdownText })}</small>
                  </td>
                  <td className={raw.netAfterFeesMicro < 0 ? "is-bad" : ""}>{row.netText}</td>
                  <td>{row.drawdownText}</td>
                  <td>
                    {row.winRateText}
                    {raw.insufficientSample ? <small className="pgm-whales__why"> {raw.sampleNote}</small> : null}
                  </td>
                  <td>{row.closedTrades}</td>
                  <td>{row.copierText}</td>
                  <td>
                    <button type="button" onClick={() => setDraft((d) => ({ ...d, sourceAnon: row.anonWallet }))}>
                      {t("terminal.copy.pick")}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {sources.rows.length === 0 ? <p role="status">{sources.emptyNote || t("terminal.copy.empty")}</p> : null}
      </section>

      <section>
        <h3>{t("terminal.copy.configTitle")}</h3>
        {previewSource ? (
          <p className="pgm-whales__counts">
            {t("terminal.copy.configFor", { wallet: previewSource.anonWallet })} — {perSourceVerdict(
              configs.find((c) => c.sourceAnon === previewSource.anonWallet)?.sourceStats,
            )}
          </p>
        ) : (
          <p className="pgm-whales__counts">{t("terminal.copy.pickFirst")}</p>
        )}

        <div className="pgm-whales__form">
          <label>
            {t("terminal.copy.field.mode")}
            <select aria-label={t("terminal.copy.field.mode")} onChange={set("mode")} value={draft.mode}>
              <option value="cap">{t("terminal.copy.mode.cap")}</option>
              <option value="ratio">{t("terminal.copy.mode.ratio")}</option>
            </select>
          </label>
          {draft.mode === "ratio" ? (
            <label>
              {t("terminal.copy.field.ratio")}
              <input aria-label={t("terminal.copy.field.ratio")} onChange={set("ratioBps")} value={draft.ratioBps} />
              <small>{t("terminal.copy.field.ratioHint")}</small>
            </label>
          ) : null}
          <label>
            {t("terminal.copy.field.maxOrder")}
            <input aria-label={t("terminal.copy.field.maxOrder")} onChange={set("maxOrder")} value={draft.maxOrder} />
          </label>
          <label>
            {t("terminal.copy.field.maxDaily")}
            <input aria-label={t("terminal.copy.field.maxDaily")} onChange={set("maxDaily")} value={draft.maxDaily} />
          </label>
          <label>
            {t("terminal.copy.field.category")}
            <input aria-label={t("terminal.copy.field.category")} onChange={set("categoryFilter")} value={draft.categoryFilter} />
          </label>
          <label>
            {t("terminal.copy.field.minPrice")}
            <input aria-label={t("terminal.copy.field.minPrice")} onChange={set("minPrice")} value={draft.minPrice} />
          </label>
          <label>
            {t("terminal.copy.field.maxPrice")}
            <input aria-label={t("terminal.copy.field.maxPrice")} onChange={set("maxPrice")} value={draft.maxPrice} />
          </label>
          <label>
            {t("terminal.copy.field.takeProfit")}
            <input aria-label={t("terminal.copy.field.takeProfit")} onChange={set("takeProfit")} value={draft.takeProfit} />
          </label>
          <label>
            {t("terminal.copy.field.stopLoss")}
            <input aria-label={t("terminal.copy.field.stopLoss")} onChange={set("stopLoss")} value={draft.stopLoss} />
          </label>
          <label>
            {t("terminal.copy.field.skipIfMoved")}
            <input aria-label={t("terminal.copy.field.skipIfMoved")} onChange={set("skipIfMovedCents")} value={draft.skipIfMovedCents} />
            <small>{t("terminal.copy.field.skipIfMovedHint")}</small>
          </label>
          <label>
            {t("terminal.copy.field.doNotEnter")}
            <input aria-label={t("terminal.copy.field.doNotEnter")} onChange={set("doNotEnterWithinHours")} value={draft.doNotEnterWithinHours} />
            <small>{t("terminal.copy.field.doNotEnterHint")}</small>
          </label>
        </div>

        <ul className="pgm-dossier__methodology">
          {problems.map((p) => (
            <li key={`${p.field}-${p.why}`}>
              <strong>{p.field}</strong>: {p.why}
            </li>
          ))}
          {problems.length === 0 ? <li>{t("terminal.copy.formOk")}</li> : null}
        </ul>

        <button disabled={problems.length > 0 || sources.busy} onClick={() => void submit()} type="button">
          {t("terminal.copy.createDryRun")}
        </button>
        <p className="pgm-whales__counts">{t("terminal.copy.createRule")}</p>

        {created ? (
          <p className="pgm-whales__counts" role="status">
            {t("terminal.copy.created", { configId: created })}
          </p>
        ) : null}
        {guards.err ? <p className="pgm-refusal" role="status">{guards.err}</p> : null}
      </section>

      <section>
        <h3>{t("terminal.copy.configsTitle")}</h3>
        {configs.length === 0 ? (
          <p role="status">{t("terminal.copy.noConfigs")}</p>
        ) : (
          <table className="pgm-table">
            <thead>
              <tr>
                <th>{t("terminal.copy.col.config")}</th>
                <th>{t("terminal.copy.col.source")}</th>
                <th>{t("terminal.copy.col.state")}</th>
                <th>{t("terminal.copy.col.caps")}</th>
                <th>{t("terminal.copy.col.open")}</th>
                <th>{t("terminal.copy.col.stop")}</th>
              </tr>
            </thead>
            <tbody>
              {configs.map((config) => (
                <tr key={config.configId}>
                  <td>{config.configId}</td>
                  <td>
                    <a href={`/trader/${encodeURIComponent(config.sourceAnon)}`}>{config.sourceAnon}</a>
                  </td>
                  <td>{config.dryRun ? t("terminal.copy.state.dryRun") : t("terminal.copy.state.live")}</td>
                  <td>
                    <Number kind="money" value={microToCents(config.maxOrderMicro)} /> {t("terminal.copy.perTrade")} ·{" "}
                    <Number kind="money" value={microToCents(config.maxDailyMicro)} /> {t("terminal.copy.perDay")}
                  </td>
                  <td>
                    <button type="button" onClick={() => setSelected(config.configId)}>
                      {t("terminal.copy.openMonitor")}
                    </button>
                  </td>
                  <td>
                    {/* Stop is a dry run: the API has no third state, and a button that claimed one would be a
                        switch that does nothing. */}
                    <button disabled={config.dryRun || guards.busy} onClick={() => void guards.set(pauseBody(config.configId))} type="button">
                      {t("terminal.copy.stop")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <button
          disabled={guards.busy || pauseAllBodies(configs).length === 0}
          onClick={() => void guards.setMany(pauseAllBodies(configs))}
          type="button"
        >
          {t("terminal.copy.stopAll")}
        </button>
        <p className="pgm-whales__counts">{t("terminal.copy.stopRule")}</p>
      </section>

      {selectedConfig ? (
        <section>
          <h3>{t("terminal.copy.monitorTitle", { configId: selectedConfig.configId })}</h3>
          <Warning config={selectedConfig} acknowledged={acknowledged} history={dryRunHistory(monitor)} onAcknowledge={setAcknowledged} />
          <p className="pgm-whales__counts">{perSourceVerdict(selectedConfig.sourceStats)}</p>
          <table className="pgm-table">
            <thead>
              <tr>
                <th>{t("terminal.copy.col.at")}</th>
                <th>{t("terminal.copy.col.kind")}</th>
                <th>{t("terminal.copy.col.sourcePrice")}</th>
                <th>{t("terminal.copy.col.ourPrice")}</th>
                <th>{t("terminal.copy.col.slippage")}</th>
                <th>{t("terminal.copy.col.reason")}</th>
              </tr>
            </thead>
            <tbody>
              {monitorRows(monitor).slice(0, 40).map((row) => (
                <tr key={row.id}>
                  <td>{new Date(row.atMs).toISOString().slice(0, 16).replace("T", " ")}</td>
                  <td>{KIND_LABEL[row.kind]}</td>
                  <td>{row.sourcePrice || "—"}</td>
                  <td>{row.ourPrice || "—"}</td>
                  <td>{row.slipBps ? bpsText(row.slipBps) : "—"}</td>
                  <td>{row.reason || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h4>{t("terminal.copy.recordTitle")}</h4>
          <table className="pgm-table">
            <thead>
              <tr>
                <th>{t("terminal.copy.col.window")}</th>
                <th>{t("terminal.copy.col.net")}</th>
                <th>{t("terminal.copy.col.drawdown")}</th>
                <th>{t("terminal.copy.col.riskAdjusted")}</th>
                <th>{t("terminal.copy.col.winRate")}</th>
              </tr>
            </thead>
            <tbody>
              {sourceRecord(selectedConfig.sourceStats).map((w) => (
                <tr key={w.windowDays}>
                  <td>{t("terminal.copy.days", { n: w.windowDays })}</td>
                  <td className={w.netAfterFeesMicro < 0 ? "is-bad" : ""}>{w.netText}</td>
                  <td>{w.maxDrawdownMicro}</td>
                  <td>{bpsText(w.riskAdjustedBps)}</td>
                  <td>{w.winRateText}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {monitor ? <p className="pgm-whales__counts">{monitor.skipReasons.join(" · ")}</p> : null}
        </section>
      ) : null}
    </div>
  );
}

/**
 * The warning, before the confirm, with the button it guards.
 *
 * `acknowledged` is a checkbox rather than a dialog: the API's condition is an explicit acknowledgement, and a
 * dialog the user dismisses is not a record of anything. Here the acknowledgement is the state the request
 * carries, and the dry-run history beside it is the second condition.
 */
function Warning({
  config,
  acknowledged,
  history,
  onAcknowledge,
}: {
  config: CopyConfig;
  acknowledged: boolean;
  history: number;
  onAcknowledge: (next: boolean) => void;
}) {
  const facts = slippageWarning(config.warning);
  const blockers = goLiveBlockers(config, history, acknowledged);
  return (
    <div className="pgm-whales__feed">
      <p className="pgm-whales__rule">{facts.headline}</p>
      <p className="pgm-whales__counts">{facts.measured}</p>
      {facts.skipRate ? <p className="pgm-whales__counts">{facts.skipRate}</p> : null}
      <p className="pgm-whales__counts">{facts.stance}</p>
      <label>
        <input checked={acknowledged} onChange={(e) => onAcknowledge(e.target.checked)} type="checkbox" />{" "}
        {t("terminal.copy.acknowledge")}
      </label>
      <p className="pgm-whales__counts">
        {t("terminal.copy.history", { n: history })}
        {blockers.length > 0 ? ` — ${t("terminal.copy.blocked")}` : ` — ${t("terminal.copy.ready")}`}
      </p>
      <ul className="pgm-dossier__methodology">
        {blockers.map((b) => (
          <li key={b}>{b}</li>
        ))}
      </ul>
    </div>
  );
}
