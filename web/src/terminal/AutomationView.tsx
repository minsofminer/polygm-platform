"use client";
/**
 * D8 · the automation screen.
 *
 * The four things the phase asks a user to be able to do, in the order they need them:
 *
 *  1. **see why a rule is not firing** — the status badge, the sentence beside it, the last run with its reason
 *     and the leaf values the trigger was read with, and the next evaluation time. "It did nothing" is never an
 *     acceptable answer, and the engine writes a row for every evaluation precisely so this screen can say more;
 *  2. **build a rule without an expression language** — trigger rows, an AND/OR joiner, action rows, all from the
 *     API's own vocabulary (`/v1/automations` returns it), with the engine's limits enforced in the form;
 *  3. **run it in dry mode before it can arm** — the preview button is the only path to the first
 *     `dry_run_completed_ms`, and the arm button refuses until that exists, which is the engine's rule surfaced
 *     where the user can act on it;
 *  4. **see the halt banner** — when the risk service has stopped the account, every rule shows *halted* rather
 *     than *active*, and the banner above the list says which limit tripped and that acknowledging is a record,
 *     not a reset.
 *
 * The templates section carries D8's arithmetic rule: the 5-minute crypto entry is listed as *withheld* with the
 * fee numbers that withheld it, never silently dropped. The protective templates are offered because they can
 * only reduce a loss the user was already risking.
 */
import { useMemo, useState } from "react";
import { t } from "@/i18n/terminal";
import { Number } from "@/num/Number";
import { microToCents } from "@/money/cents";
import { request } from "@/api/client";
import { RefusalNotice } from "@/ui/RefusalNotice";
import {
  actionSummary,
  capsText,
  emptyDraft,
  builderProblems,
  draftPayload,
  feeArithmeticLines,
  fieldsFor,
  haltBanner,
  lastFiredText,
  newRow,
  nextEvalText,
  previewPayload,
  ruleStatusLabel,
  runOutcomeLabel,
  runSentence,
  templateSplit,
  type BuilderDraft,
} from "./automation";
import type { AutomationList, AutomationRule, AutomationRunRow, TemplateCatalog } from "./wire";

const MAX_RUNS_SHOWN = 25;

export function AutomationView({ initial, catalog }: { initial: AutomationList | null; catalog: TemplateCatalog | null }) {
  const [list, setList] = useState<AutomationList | null>(initial);
  const [runs, setRuns] = useState<AutomationRunRow[]>([]);
  const [openRule, setOpenRule] = useState<string | null>(null);
  const [draft, setDraft] = useState<BuilderDraft>(() => emptyDraft(initial?.vocabulary ?? null));
  const [problems, setProblems] = useState<string[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [simulation, setSimulation] = useState<string | null>(null);
  const nowMs = Date.now();

  const vocab = list?.vocabulary ?? initial?.vocabulary ?? null;
  const banner = haltBanner(list?.halt ?? null);
  const templates = useMemo(() => templateSplit(catalog), [catalog]);
  const caps = list ? capsText(list) : "";

  const refresh = async () => {
    const res = await request<AutomationList>({ key: "automations" });
    if (res.ok) setList(res.data);
  };

  const loadRuns = async (ruleId: string) => {
    setOpenRule(ruleId);
    const res = await request<{ rows: AutomationRunRow[] }>({ key: "automationRuns", query: { ruleId, limit: 50 } });
    if (res.ok) setRuns(res.data.rows);
    else { setRuns([]); setErr(res.error.message); }
  };

  const post = async (key: "createAutomation" | "automationPreview" | "automationGuards", body: Record<string, unknown>) => {
    setBusy(true);
    setErr(null);
    setNotice(null);
    const res = await request<Record<string, unknown>>({ key, body });
    setBusy(false);
    if (!res.ok) {
      setErr(`${res.error.code}: ${res.error.message}`);
      return null;
    }
    return res.data;
  };

  const save = async () => {
    const bad = builderProblems(draft, vocab);
    setProblems(bad);
    if (bad.length > 0) return;
    const out = await post("createAutomation", draftPayload(draft));
    if (out) {
      setNotice(t("terminal.automation.notice.saved"));
      await refresh();
    }
  };

  const preview = async (ruleId?: string) => {
    const out = await post("automationPreview", previewPayload(draft, ruleId));
    if (out) {
      const sim = (out.simulation ?? {}) as { sentence?: string; fires?: boolean };
      setSimulation(sim.sentence ?? null);
      setNotice(t("terminal.automation.notice.previewed"));
      if (ruleId) await refresh();
    }
  };

  const setState = async (ruleId: string, state: "active" | "paused") => {
    const out = await post("automationGuards", { ruleId, state, reason: state === "paused" ? t("terminal.automation.pauseReason") : undefined });
    if (out) {
      setNotice(String(out.note ?? ""));
      await refresh();
    }
  };

  const setRow = (side: "triggers" | "actions", id: string, field: string, value: string | number | boolean) => {
    setDraft((d) => ({
      ...d,
      [side]: d[side].map((row) => (row.id === id ? { ...row, fields: { ...row.fields, [field]: value } } : row)),
    }));
  };

  const changeKind = (side: "triggers" | "actions", id: string, kind: string) => {
    setDraft((d) => ({ ...d, [side]: d[side].map((row) => (row.id === id ? { ...row, kind, fields: {} } : row)) }));
  };

  return (
    <div className="pgm-dossier">
      <header className="pgm-dossier__head">
        <h2>{t("terminal.automation.title")}</h2>
        <p className="pgm-whales__counts">{caps}</p>
        <p className="pgm-whales__counts">{t("terminal.automation.rule", { min: (vocab?.limits.minIntervalMs ?? 60000) / 1000 })}</p>
      </header>

      {banner.show ? (
        <div className="pgm-terminal__panel" role="alert" data-testid="halt-banner">
          <strong>{banner.title}</strong>
          <p>{banner.body}</p>
          <p className="pgm-whales__counts">{banner.hint}</p>
          <p className="pgm-whales__counts">
            {t("terminal.automation.halt.tripped")} <Number kind="count" value={list?.halt?.trippedMs ?? 0} />
          </p>
        </div>
      ) : null}

      {err ? <RefusalNotice route="createAutomation" extra={err} /> : null}
      {notice ? <p className="pgm-whales__counts" role="status" data-testid="notice">{notice}</p> : null}

      <section className="pgm-terminal__panel">
        <h3>{t("terminal.automation.list.title")}</h3>
        {list && list.rules.length === 0 ? <p className="pgm-whales__counts">{t("terminal.automation.list.empty")}</p> : null}
        <table className="pgm-table">
          <thead>
            <tr>
              <th>{t("terminal.automation.list.rule")}</th>
              <th>{t("terminal.automation.list.status")}</th>
              <th>{t("terminal.automation.list.lastFired")}</th>
              <th>{t("terminal.automation.list.next")}</th>
              <th>{t("terminal.automation.list.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {(list?.rules ?? []).map((rule) => (
              <tr key={rule.ruleId} data-testid={`rule-${rule.ruleId}`} data-status={rule.status}>
                <td>
                  <strong>{rule.name}</strong>
                  <div className="pgm-whales__why">{rule.statusWhy}</div>
                  {rule.lastRun ? (
                    <div className="pgm-whales__why">{t("terminal.automation.list.lastRun")} {runSentence(rule.lastRun)}</div>
                  ) : null}
                </td>
                <td><span className="pgm-badge" data-status={rule.status}>{ruleStatusLabel(rule)}</span></td>
                <td>{lastFiredText(rule, nowMs)}</td>
                <td>{nextEvalText(rule, nowMs)}</td>
                <td>
                  <button type="button" onClick={() => void preview(rule.ruleId)} disabled={busy}>
                    {t("terminal.automation.action.dryRun")}
                  </button>{" "}
                  <button type="button" onClick={() => void setState(rule.ruleId, rule.enabled ? "paused" : "active")} disabled={busy || rule.status === "halted"}>
                    {rule.enabled ? t("terminal.automation.action.pause") : t("terminal.automation.action.arm")}
                  </button>{" "}
                  <button type="button" onClick={() => void loadRuns(rule.ruleId)}>
                    {t("terminal.automation.action.history")}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {openRule ? (
          <div data-testid="run-history">
            <h4>{t("terminal.automation.history.title")}</h4>
            <p className="pgm-whales__counts">{t("terminal.automation.history.note")}</p>
            {runs.length === 0 ? <p className="pgm-whales__counts">{t("terminal.automation.history.empty")}</p> : null}
            <ul>
              {runs.slice(0, MAX_RUNS_SHOWN).map((run, i) => (
                <li key={`${run.atMs}-${i}`} className="pgm-whales__why">
                  {runOutcomeLabel(run.outcome)} · {run.mode} · {runSentence(run)}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>

      <section className="pgm-terminal__panel">
        <h3>{t("terminal.automation.builder.title")}</h3>
        <p className="pgm-whales__counts">{t("terminal.automation.builder.note")}</p>

        <label>
          {t("terminal.automation.builder.name")}
          <input value={draft.name} maxLength={80} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        </label>
        <label>
          {t("terminal.automation.builder.market")}
          <input value={draft.marketId} onChange={(e) => setDraft({ ...draft, marketId: e.target.value })} />
        </label>
        <label>
          {t("terminal.automation.builder.joiner")}
          <select value={draft.match} onChange={(e) => setDraft({ ...draft, match: e.target.value as "all" | "any" })}>
            {Object.entries(vocab?.joiners ?? { all: "AND", any: "OR" }).map(([key, label]) => (
              <option key={key} value={key}>{label}</option>
            ))}
          </select>
        </label>

        {(["triggers", "actions"] as const).map((side) => (
          <div key={side} data-testid={`builder-${side}`}>
            {/* Two explicit lookups rather than a template key: the i18n check refuses dynamic keys, and it is
                right to — a key built at runtime is a key no build can prove exists. */}
            <h4>{side === "triggers" ? t("terminal.automation.builder.triggers") : t("terminal.automation.builder.actions")}</h4>
            {draft[side].map((row) => (
              <div key={row.id}>
                <select aria-label={t("terminal.automation.builder.kind")} value={row.kind} onChange={(e) => changeKind(side, row.id, e.target.value)}>
                  {(vocab?.[side] ?? []).map((opt) => (
                    <option key={opt.kind} value={opt.kind}>{opt.label}</option>
                  ))}
                </select>
                {fieldsFor(vocab, side, row.kind).map((f) => (
                  <label key={f.name}>
                    {f.label}
                    {f.type === "select" ? (
                      <select value={String(row.fields[f.name] ?? "")} onChange={(e) => setRow(side, row.id, f.name, e.target.value)}>
                        <option value="">—</option>
                        {(f.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
                      </select>
                    ) : f.type === "bool" ? (
                      <input type="checkbox" checked={Boolean(row.fields[f.name])} onChange={(e) => setRow(side, row.id, f.name, e.target.checked)} />
                    ) : (
                      <input
                        inputMode="numeric"
                        value={String(row.fields[f.name] ?? "")}
                        onChange={(e) => setRow(side, row.id, f.name, e.target.value.replace(/[^0-9]/g, ""))}
                      />
                    )}
                  </label>
                ))}
                <button type="button" onClick={() => setDraft({ ...draft, [side]: draft[side].filter((r) => r.id !== row.id) })}>
                  {t("terminal.automation.builder.remove")}
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={() => {
                const first = vocab?.[side]?.[0]?.kind ?? "";
                setDraft({ ...draft, [side]: [...draft[side], newRow(first)] });
              }}
            >
              {t("terminal.automation.builder.addRow")}
            </button>
          </div>
        ))}

        <label>
          {t("terminal.automation.builder.maxPerDay")}
          <input inputMode="numeric" value={String(draft.maxPerDay)} onChange={(e) => setDraft({ ...draft, maxPerDay: window.Number.parseInt(e.target.value || "0", 10) })} />
        </label>
        <label>
          {t("terminal.automation.builder.maxLoss")}
          <input inputMode="numeric" value={String(draft.maxLossMicro)} onChange={(e) => setDraft({ ...draft, maxLossMicro: window.Number.parseInt(e.target.value || "0", 10) })} />
        </label>

        {problems.length > 0 ? (
          <ul data-testid="builder-problems">
            {problems.map((p) => <li key={p}>{p}</li>)}
          </ul>
        ) : null}
        {simulation ? <p className="pgm-whales__counts" data-testid="simulation">{simulation}</p> : null}
        <button type="button" onClick={() => void save()} disabled={busy}>{t("terminal.automation.builder.save")}</button>{" "}
        <button type="button" onClick={() => void preview()} disabled={busy}>{t("terminal.automation.builder.preview")}</button>
      </section>

      <section className="pgm-terminal__panel">
        <h3>{t("terminal.automation.templates.title")}</h3>
        <p className="pgm-whales__counts">{catalog?.verdict ?? t("terminal.automation.templates.noCatalog")}</p>
        <ul>
          {templates.usable.map((tpl) => (
            <li key={tpl.templateId} data-testid={`template-${tpl.templateId}`}>
              <strong>{tpl.name}</strong> · {tpl.kind} · {actionSummary(tpl.actions)}
              <div className="pgm-whales__why">{tpl.why}</div>
            </li>
          ))}
          {templates.withheld.map((tpl) => (
            <li key={tpl.templateId} className="pgm-whales__why" data-testid={`withheld-${tpl.templateId}`}>
              <strong>{tpl.name}</strong> · {t("terminal.automation.templates.withheld")}
              <div>{tpl.why}</div>
              <ul>
                {feeArithmeticLines(tpl.feeArithmetic).map((line) => (
                  <li key={line.label}>
                    {line.label}:{" "}
                    {line.kind === "money" && typeof line.value === "number"
                      ? <Number kind="money" value={line.value} />
                      : line.kind === "percent" && typeof line.value === "number"
                        ? <Number kind="percent" value={line.value} />
                        : String(line.value ?? t("terminal.automation.fee.unmeasured"))}
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
        <p className="pgm-whales__counts">{catalog?.why ?? ""}</p>
      </section>
    </div>
  );
}

/** The list's own helper for the confirmation step: what the rule will do, in words, before it is armed. */
export function confirmSentence(rule: AutomationRule): string {
  return t("terminal.automation.confirm", { name: rule.name, actions: actionSummary(rule.actions), cap: rule.maxPerDay });
}

export const MAX_LOSS_TEXT = (micro: number): number => microToCents(micro);
