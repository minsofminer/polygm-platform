"use client";
/**
 * D9 · the alerts screen.
 *
 * Three panels, in the order a user asks the questions:
 *
 *  1. **what will fire, and will it reach me** — the rule list with its channel, its budget, the cooldown stated
 *     as the rule it is ("3 per 1h = one every 20m at most"), and the API's own sentence for what this rule
 *     would do *right now* (sent, held for quiet hours, batched, or refused by the plan). A rule list that shows
 *     "on" while quiet hours are holding everything is a list that gets called broken at 2am;
 *  2. **can I change it without a page reload** — the inline editor writes through the API's upsert and refuses
 *     in the form what the API would refuse: a missing target, a cap outside 1..24, a window under a minute, and
 *     a channel this plan does not cover. The plan refusal is the one that matters: a free account saving a
 *     webhook rule would be a rule that can never deliver;
 *  3. **did it actually arrive** — the delivery history, with per-channel status and the reason for anything
 *     that was held or dropped, and test fires kept in their own list. `digest_scheduled` reads as "held", never
 *     as "sent", because the difference is the whole reason the status exists.
 *
 * The test-fire button says what it is: no delivery transport runs in this build, so a `queued` row is the record
 * of what would be sent. The screen shows the API's note rather than paraphrasing it.
 */
import { useEffect, useState } from "react";
import { t } from "@/i18n/terminal";
import { request } from "@/api/client";
import { RefusalNotice } from "@/ui/RefusalNotice";
import {
  ALERT_KINDS,
  CHANNELS,
  DIGESTS,
  SEVERITIES,
  channelText,
  clockOf,
  countsText,
  cooldownFor,
  draftFrom,
  draftPayload,
  draftProblems,
  evaluatorText,
  OPS,
  historyLatencyText,
  kindLabel,
  quietHoursText,
  settingsPayload,
  settingsProblems,
  splitHistory,
  statusLabel,
  testFireSummary,
  testableChannels,
  type Draft,
} from "./alerts";
import type { AlertDeliveryRow, AlertPlanRow, AlertsPayload, NotificationSettings } from "./wire";

type SettingsDraft = {
  quietStartMin: number;
  quietEndMin: number;
  digestMode: string;
  digestAtMin: number;
  defaultChannel: string;
  tzOffsetMin: number;
};

export function AlertsView({ initial, history }: { initial: AlertsPayload | null; history: AlertDeliveryRow[] }) {
  const [payload, setPayload] = useState<AlertsPayload | null>(initial);
  const [rows, setRows] = useState<AlertDeliveryRow[]>(history);
  const [draft, setDraft] = useState<Draft>(() => draftFrom(null, initial?.settings ?? fallbackSettings()));
  const [settings, setSettings] = useState<SettingsDraft>(() => settingsDraft(initial?.settings ?? fallbackSettings()));
  const [editing, setEditing] = useState<string | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [testPlan, setTestPlan] = useState<AlertPlanRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const plan = payload?.plan ?? "free";
  const settingsObj = payload?.settings ?? fallbackSettings();
  const split = splitHistory(rows);

  useEffect(() => {
    setDraft((d) => (d.marketId || initial?.rules.length ? d : draftFrom(null, initial?.settings ?? fallbackSettings())));
  }, [initial]);

  const refresh = async () => {
    const res = await request<AlertsPayload>({ key: "alerts" });
    if (res.ok) setPayload(res.data);
    const hist = await request<{ rows: AlertDeliveryRow[] }>({ key: "alertDeliveries", query: { limit: 50 } });
    if (hist.ok) setRows(hist.data.rows);
  };

  const post = async (key: "createAlert" | "alertTest" | "alertSettings", body: Record<string, unknown>) => {
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

  const saveRule = async () => {
    const bad = draftProblems(draft, plan);
    setProblems(bad);
    if (bad.length > 0) return;
    const out = await post("createAlert", draftPayload(draft));
    if (out) {
      setNotice(t("terminal.alerts.notice.saved", { action: String(out.action ?? "saved") }));
      setEditing(null);
      setDraft(draftFrom(null, settingsObj));
      await refresh();
    }
  };

  const testFire = async (ruleId: string) => {
    const out = await post("alertTest", { ruleId, channels: testableChannels(settingsObj, plan) });
    if (out) {
      setTestPlan((out.plan ?? []) as AlertPlanRow[]);
      setNotice(String(out.note ?? ""));
      await refresh();
    }
  };

  const saveSettings = async () => {
    const bad = settingsProblems(settings);
    setProblems(bad);
    if (bad.length > 0) return;
    const out = await post("alertSettings", settingsPayload(settings));
    if (out) {
      setNotice(t("terminal.alerts.notice.settingsSaved"));
      await refresh();
    }
  };

  const startEdit = (ruleId: string) => {
    const rule = payload?.rules.find((r) => r.ruleId === ruleId) ?? null;
    if (!rule) return;
    setEditing(ruleId);
    setDraft(draftFrom(rule, settingsObj));
  };

  const cooldown = cooldownFor(draft.firesPerWindow, draft.windowMs);

  return (
    <div className="pgm-dossier">
      <header className="pgm-dossier__head">
        <h2>{t("terminal.alerts.title")}</h2>
        {payload ? <p className="pgm-whales__counts">{countsText(payload)}</p> : null}
        <p className="pgm-whales__counts">{quietHoursText(settingsObj)}</p>
        <p className="pgm-whales__counts">{settingsObj.note}</p>
      </header>

      {err ? <RefusalNotice route="createAlert" extra={err} /> : null}
      {notice ? <p className="pgm-whales__counts" role="status" data-testid="alert-notice">{notice}</p> : null}

      <section className="pgm-terminal__panel">
        <h3>{t("terminal.alerts.list.title")}</h3>
        {payload && payload.rules.length === 0 ? <p className="pgm-whales__counts">{t("terminal.alerts.list.empty")}</p> : null}
        <table className="pgm-table">
          <thead>
            <tr>
              <th>{t("terminal.alerts.list.rule")}</th>
              <th>{t("terminal.alerts.list.channel")}</th>
              <th>{t("terminal.alerts.list.cooldown")}</th>
              <th>{t("terminal.alerts.list.now")}</th>
              <th>{t("terminal.alerts.list.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {(payload?.rules ?? []).map((rule) => (
              <tr key={rule.ruleId} data-testid={`alert-${rule.ruleId}`} data-channel={rule.channel}>
                <td>
                  <strong>{kindLabel(rule.kind)}</strong>
                  <div className="pgm-whales__why">{rule.target.marketId ?? rule.target.eventId ?? ""}</div>
                  {/* Whether a loop is watching this rule at all. A rule that fires only when its owner presses
                      the test button has to say so, or the user waits for an alert that was never coming. */}
                  <div className="pgm-whales__why" data-testid={`evaluator-${rule.ruleId}`}
                       data-evaluated={rule.evaluated ? "yes" : "no"}>
                    {evaluatorText(rule)}
                  </div>
                  {rule.lastDelivery ? (
                    <div className="pgm-whales__why">
                      {t("terminal.alerts.list.last", { status: statusLabel(rule.lastDelivery.status), channel: rule.lastDelivery.channel })}
                    </div>
                  ) : null}
                </td>
                <td>{channelText(rule)}</td>
                <td>
                  {rule.cooldownRule}
                  <div className="pgm-whales__why">{rule.cooldownNote}</div>
                </td>
                <td>
                  {rule.wouldDoNow.sentence}
                  {rule.quietHours.active ? <div className="pgm-whales__why">{rule.quietHours.note}</div> : null}
                </td>
                <td>
                  <button type="button" onClick={() => startEdit(rule.ruleId)}>{t("terminal.alerts.action.edit")}</button>{" "}
                  <button type="button" onClick={() => void testFire(rule.ruleId)} disabled={busy}>{t("terminal.alerts.action.test")}</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="pgm-terminal__panel" data-testid="alert-editor">
        <h3>{editing ? t("terminal.alerts.editor.editTitle") : t("terminal.alerts.editor.title")}</h3>
        <label>
          {t("terminal.alerts.editor.kind")}
          <select value={draft.kind} onChange={(e) => setDraft({ ...draft, kind: e.target.value })}>
            {ALERT_KINDS.map((k) => <option key={k} value={k}>{kindLabel(k)}</option>)}
          </select>
        </label>
        {/* The condition, in the fields the ENGINE needs for this kind. A kind whose condition is missing is
            refused here and by the API: a rule that looks armed and can never fire is the failure this screen
            exists to make visible. */}
        {draft.kind === "price_level" ? (
          <>
            <label>
              {t("terminal.alerts.editor.level")}
              <input inputMode="decimal" value={draft.levelText}
                     onChange={(e) => setDraft({ ...draft, levelText: e.target.value })} />
            </label>
            <label>
              {t("terminal.alerts.editor.op")}
              <select value={draft.op} onChange={(e) => setDraft({ ...draft, op: e.target.value })}>
                {OPS.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            </label>
          </>
        ) : null}
        {draft.kind === "spread_widen" ? (
          <label>
            {t("terminal.alerts.editor.spread")}
            <input inputMode="numeric" value={draft.spreadBpText}
                   onChange={(e) => setDraft({ ...draft, spreadBpText: e.target.value })} />
          </label>
        ) : null}
        {draft.kind === "illiquid_top" ? (
          <label>
            {t("terminal.alerts.editor.depth")}
            <input inputMode="numeric" value={draft.depthUsdText}
                   onChange={(e) => setDraft({ ...draft, depthUsdText: e.target.value })} />
          </label>
        ) : null}
        {draft.kind === "whale_fill" ? (
          <label>
            {t("terminal.alerts.editor.notional")}
            <input inputMode="numeric" value={draft.absUsdText}
                   onChange={(e) => setDraft({ ...draft, absUsdText: e.target.value })} />
          </label>
        ) : null}
        {draft.kind === "resolve_lead" ? (
          <label>
            {t("terminal.alerts.editor.hours")}
            <input inputMode="numeric" value={draft.hoursText}
                   onChange={(e) => setDraft({ ...draft, hoursText: e.target.value })} />
          </label>
        ) : null}
        <label>
          {t("terminal.alerts.editor.market")}
          <input value={draft.marketId} onChange={(e) => setDraft({ ...draft, marketId: e.target.value })} />
        </label>
        <label>
          {t("terminal.alerts.editor.channel")}
          <select value={draft.channel} onChange={(e) => setDraft({ ...draft, channel: e.target.value })}>
            {CHANNELS.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label>
          {t("terminal.alerts.editor.severity")}
          <select value={draft.severity} onChange={(e) => setDraft({ ...draft, severity: e.target.value })}>
            {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label>
          {t("terminal.alerts.editor.fires")}
          <input inputMode="numeric" value={String(draft.firesPerWindow)} onChange={(e) => setDraft({ ...draft, firesPerWindow: window.Number.parseInt(e.target.value || "0", 10) })} />
        </label>
        <label>
          {t("terminal.alerts.editor.window")}
          <input inputMode="numeric" value={String(draft.windowMs)} onChange={(e) => setDraft({ ...draft, windowMs: window.Number.parseInt(e.target.value || "0", 10) })} />
        </label>
        <p className="pgm-whales__counts" data-testid="editor-cooldown">{cooldown.rule}</p>
        {problems.length > 0 ? (
          <ul data-testid="alert-problems">{problems.map((p) => <li key={p}>{p}</li>)}</ul>
        ) : null}
        <button type="button" onClick={() => void saveRule()} disabled={busy}>{t("terminal.alerts.editor.save")}</button>
        {editing ? <button type="button" onClick={() => { setEditing(null); setDraft(draftFrom(null, settingsObj)); }}>{t("terminal.alerts.editor.cancel")}</button> : null}
      </section>

      <section className="pgm-terminal__panel">
        <h3>{t("terminal.alerts.settings.title")}</h3>
        <label>
          {t("terminal.alerts.settings.quietFrom")}
          <select value={String(settings.quietStartMin)} onChange={(e) => setSettings({ ...settings, quietStartMin: window.Number.parseInt(e.target.value, 10) })}>
            <option value="-1">{t("terminal.alerts.off")}</option>
            {HOURS.map((m) => <option key={m} value={String(m)}>{clockOf(m)}</option>)}
          </select>
        </label>
        <label>
          {t("terminal.alerts.settings.quietTo")}
          <select value={String(settings.quietEndMin)} onChange={(e) => setSettings({ ...settings, quietEndMin: window.Number.parseInt(e.target.value, 10) })}>
            <option value="-1">{t("terminal.alerts.off")}</option>
            {HOURS.map((m) => <option key={m} value={String(m)}>{clockOf(m)}</option>)}
          </select>
        </label>
        <label>
          {t("terminal.alerts.settings.digest")}
          <select value={settings.digestMode} onChange={(e) => setSettings({ ...settings, digestMode: e.target.value })}>
            {DIGESTS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
        <label>
          {t("terminal.alerts.settings.defaultChannel")}
          <select value={settings.defaultChannel} onChange={(e) => setSettings({ ...settings, defaultChannel: e.target.value })}>
            {CHANNELS.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <p className="pgm-whales__counts">{t("terminal.alerts.settings.quietNote")}</p>
        <button type="button" onClick={() => void saveSettings()} disabled={busy}>{t("terminal.alerts.settings.save")}</button>
      </section>

      {testPlan ? (
        <section className="pgm-terminal__panel" data-testid="test-result">
          <h3>{t("terminal.alerts.test.title")}</h3>
          <p className="pgm-whales__counts">{testFireSummary(testPlan)}</p>
          <ul>
            {testPlan.map((p) => <li key={p.channel}>{p.channel}: {p.sentence}</li>)}
          </ul>
        </section>
      ) : null}

      <section className="pgm-terminal__panel">
        <h3>{t("terminal.alerts.history.title")}</h3>
        <p className="pgm-whales__counts">{t("terminal.alerts.history.note")}</p>
        <h4>{t("terminal.alerts.history.real")}</h4>
        <DeliveryTable rows={split.real} />
        <h4>{t("terminal.alerts.history.tests")}</h4>
        <p className="pgm-whales__counts">{t("terminal.alerts.history.testsNote")}</p>
        <DeliveryTable rows={split.tests} />
      </section>
    </div>
  );
}

const HOURS = Array.from({ length: 48 }, (_, i) => i * 30);

function DeliveryTable({ rows }: { rows: AlertDeliveryRow[] }) {
  if (rows.length === 0) return <p className="pgm-whales__counts">{t("terminal.alerts.history.empty")}</p>;
  return (
    <table className="pgm-table">
      <thead>
        <tr>
          <th>{t("terminal.alerts.history.channel")}</th>
          <th>{t("terminal.alerts.history.status")}</th>
          <th>{t("terminal.alerts.history.latency")}</th>
          <th>{t("terminal.alerts.history.reason")}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.deliveryId} data-testid={`delivery-${row.deliveryId}`} data-status={row.status}>
            <td>{row.channel}</td>
            <td>{statusLabel(row.status)}</td>
            <td>{historyLatencyText(row)}</td>
            <td className="pgm-whales__why">{row.reason}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function fallbackSettings(): NotificationSettings {
  return {
    quietStartMin: -1,
    quietEndMin: -1,
    tzOffsetMin: 0,
    digestMode: "off",
    digestAtMin: 480,
    defaultChannel: "telegram",
    channels: [
      { channel: "telegram", plan: "free", isDefault: true },
      { channel: "email", plan: "trader", isDefault: false },
      { channel: "webhook", plan: "pro", isDefault: false },
    ],
    quietHours: { configured: false, active: false, untilMs: null, note: "" },
    digestNow: { mode: "off", deferred: false, atMs: null, note: "" },
    note: "",
  };
}

function settingsDraft(s: NotificationSettings): SettingsDraft {
  return {
    quietStartMin: s.quietStartMin,
    quietEndMin: s.quietEndMin,
    digestMode: s.digestMode,
    digestAtMin: s.digestAtMin,
    defaultChannel: s.defaultChannel,
    tzOffsetMin: s.tzOffsetMin,
  };
}
