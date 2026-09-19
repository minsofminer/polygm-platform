/**
 * D9 · the alerts screen's rules.
 *
 * Everything here is a *sentence the user reads before an alert exists*, which is why it lives in a module with
 * tests instead of inside the component: a quiet window evaluated in the wrong timezone, a cooldown shown as a
 * number nobody can check, or a "held" alert that looks identical to a sent one are all failures that never
 * throw. They just make the product quietly wrong at 2am.
 *
 * Three of the choices are worth naming:
 *
 *  * **The cooldown is derived and shown as a rule.** The API stores `firesPerWindow`/`windowMs` and the server
 *    computes the cooldown; this module repeats that arithmetic for the *pending* edit so the user sees "3 per
 *    hour = one every 20 minutes at most" while typing, and `cooldownAgrees` checks the two agree. A UI that
 *    proved the number on its own would be a second opinion about a budget the server owns.
 *  * **Quiet hours hold everything, urgent included.** The screen says so next to the control rather than in a
 *    tooltip, because the only way to get an urgent alert through is to turn quiet hours off — a product
 *    decision the user has to be able to find.
 *  * **A test fire is not a fire.** `testFireSummary` refuses to let the screen call it an alert, and the
 *    delivery history keeps test rows in their own list: a test that inflated the history would make the
 *    history useless for the question it exists to answer.
 */
import { centsFromDecimal, centsToMicro, formatCents, integerUnits, microToCents } from "@/money/cents";
import { t } from "@/i18n/terminal";
import type { AlertDeliveryRow, AlertPlanRow, AlertRule, AlertsPayload, NotificationSettings } from "./wire";

export const CHANNELS = ["telegram", "email", "webhook"] as const;
export const SEVERITIES = ["info", "notice", "urgent"] as const;
export const ALERT_KINDS = ["price_level", "spread_widen", "whale_fill", "resolve_lead", "illiquid_top", "new_market", "manual"] as const;
export const DIGESTS = ["off", "hourly", "daily"] as const;

/** How the last fire's per-channel status reads. `digest_scheduled` is *held*, not sent, and saying "sent"
 *  would be the one lie that makes a user stop trusting the list. */
export const STATUS_LABEL: Record<string, string> = {
  queued: t("terminal.alerts.status.queued"),
  sent: t("terminal.alerts.status.sent"),
  digest_scheduled: t("terminal.alerts.status.held"),
  dropped_rate_limited: t("terminal.alerts.status.rateLimited"),
  failed: t("terminal.alerts.status.failed"),
};

export function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status;
}

export function kindLabel(kind: string): string {
  const map: Record<string, string> = {
    price_level: t("terminal.alerts.kind.priceLevel"),
    spread_widen: t("terminal.alerts.kind.spreadWiden"),
    whale_fill: t("terminal.alerts.kind.whaleFill"),
    resolve_lead: t("terminal.alerts.kind.resolveLead"),
    illiquid_top: t("terminal.alerts.kind.illiquidTop"),
    new_market: t("terminal.alerts.kind.newMarket"),
    manual: t("terminal.alerts.kind.manual"),
  };
  return map[kind] ?? kind;
}

/** `90000` → "1m 30s". Durations are shown in the unit a person thinks in, never as raw milliseconds. */
export function durationText(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  const s = total % 60;
  if (m < 60) return s === 0 ? `${m}m` : `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest === 0 ? `${h}h` : `${h}h ${rest}m`;
}

/**
 * The cooldown, as the rule it is: `firesPerWindow` per `windowMs` is one fire per `windowMs/firesPerWindow`.
 * The server computes the same number and sends it; this exists for the editor's *pending* values.
 */
export function cooldownFor(firesPerWindow: number, windowMs: number): { cooldownMs: number; rule: string } {
  const fires = Math.max(1, Math.floor(firesPerWindow));
  const window = Math.max(60_000, Math.floor(windowMs));
  const cooldownMs = Math.floor(window / fires);
  return { cooldownMs, rule: t("terminal.alerts.cooldownRule", { fires, window: durationText(window), cooldown: durationText(cooldownMs) }) };
}

/** Does the screen's arithmetic agree with the server's? A disagreement is shown, not hidden. */
export function cooldownAgrees(rule: AlertRule): boolean {
  return cooldownFor(rule.firesPerWindow, rule.windowMs).cooldownMs === rule.cooldownMs;
}

export function quietHoursText(settings: NotificationSettings): string {
  if (!settings.quietHours.configured) return t("terminal.alerts.quietOff");
  return settings.quietHours.note;
}

export function clockOf(minute: number): string {
  if (minute < 0) return t("terminal.alerts.off");
  const m = ((minute % 1440) + 1440) % 1440;
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
}

/** What a rule would do right now, in the API's own words. The server's sentence is used verbatim: this is the
 *  answer to "why is my alert quiet", and a re-worded version would be a second answer. */
export function wouldDoText(rule: AlertRule): string {
  return rule.wouldDoNow.sentence;
}

/** The channel a rule uses, with its plan note when the plan does not cover it. */
export function channelText(rule: AlertRule): string {
  return rule.channelAllowed ? rule.channel : t("terminal.alerts.channelNeedsPlan", { note: rule.channelNote });
}

/**
 * The condition each kind needs the engine to have. A kind whose condition is missing is refused before the
 * request leaves the screen, because the alternative is a rule that is listed, shows a cooldown, and can never
 * fire — the failure this whole screen exists to make visible.
 *
 * The keys are the API's own wire names (`priceMicro`, `spreadBp`, ...) so this table and the server's
 * `KIND_PARAMS` are the same vocabulary read twice, and the defaults below are the server's own defaults: a form
 * that seeded a different number would write a rule nobody chose.
 */
export const KIND_PARAMS: Record<string, readonly string[]> = {
  price_level: ["priceMicro", "op"],
  spread_widen: ["spreadBp"],
  whale_fill: ["absUsdMicro"],
  resolve_lead: ["hours"],
  illiquid_top: ["minDepthUsdMicro"],
  new_market: [],
  manual: [],
};

/** USDC and hours, as text: a number input that silently turns "10,000" into NaN is how a rule gets saved with
 *  a zero in it. Every one of these is parsed through the money layer, never by `Number` on a money string. */
export const PARAM_DEFAULTS = { absUsd: "10000", hours: "6", depthUsd: "500" } as const;

export const OPS = [">=", "<="] as const;

/**
 * A price in micro (a probability, scaled by 1e6) rendered with four decimals — "0.6200" — by integer
 * arithmetic.
 *
 * `formatCents(microToCents(x), {decimals: 4})` is the wrong tool and looks right: cents are a HUNDREDTH of a
 * dollar and a probability is not money, so 620000 micro came back as "0.0062" — a level nobody typed, on the
 * one screen where a wrong level silently changes what the rule does.
 */
export function priceMicroText(micro: number): string {
  const v = integerUnits("micro-price", micro);
  const whole = Math.trunc(v / 1_000_000);
  const frac = String(Math.abs(v) % 1_000_000).padStart(6, "0").slice(0, 4);
  return `${whole < 0 ? "-" : ""}${Math.abs(whole)}.${frac}`;
}

/**
 * A price level the user typed -> micro, by string arithmetic. `Number("0.62")` is a double, and the level is
 * the field that decides when a rule fires: the digits the user typed are the digits the engine gets.
 *
 * Returns `null` for anything that is not a plain decimal, so the caller can refuse it with a sentence rather
 * than saving a zero.
 */
export function priceMicroFromText(text: string): number | null {
  const m = /^(\d+)(?:\.(\d+))?$/.exec(String(text).trim());
  if (!m) return null;
  const whole = Number(m[1]);
  const frac = Number((m[2] ?? "").padEnd(6, "0").slice(0, 6));
  if (!Number.isSafeInteger(whole * 1_000_000 + frac)) return null;
  return whole * 1_000_000 + frac;
}

export type Draft = {
  ruleId?: string;
  kind: string;
  marketId: string;
  channel: string;
  severity: string;
  firesPerWindow: number;
  windowMs: number;
  enabled: boolean;
  levelText: string;
  op: string;
  spreadBpText: string;
  depthUsdText: string;
  absUsdText: string;
  hoursText: string;
};

function draftParamsFrom(rule: AlertRule): Pick<Draft, "levelText" | "op" | "spreadBpText" | "depthUsdText" | "absUsdText" | "hoursText"> {
  const p = (rule.params ?? {}) as Record<string, unknown>;
  const price = typeof p.priceMicro === "number" ? p.priceMicro : null;
  const depth = typeof p.minDepthUsdMicro === "number" ? p.minDepthUsdMicro : null;
  const abs = typeof p.absUsdMicro === "number" ? p.absUsdMicro : null;
  return {
    levelText: price === null ? "" : priceMicroText(price),
    op: typeof p.op === "string" ? p.op : ">=",
    spreadBpText: typeof p.spreadBp === "number" ? String(p.spreadBp) : "",
    depthUsdText: depth === null ? PARAM_DEFAULTS.depthUsd : formatCents(microToCents(depth), { currency: null }),
    absUsdText: abs === null ? PARAM_DEFAULTS.absUsd : formatCents(microToCents(abs), { currency: null }),
    hoursText: typeof p.hours === "number" ? String(p.hours) : PARAM_DEFAULTS.hours,
  };
}

export function draftFrom(rule: AlertRule | null, settings: NotificationSettings): Draft {
  if (rule) {
    return { ruleId: rule.ruleId, kind: rule.kind, marketId: rule.target.marketId ?? "", channel: rule.channel,
             severity: rule.severity, firesPerWindow: rule.firesPerWindow, windowMs: rule.windowMs,
             enabled: rule.enabled, ...draftParamsFrom(rule) };
  }
  return { kind: "whale_fill", marketId: "", channel: settings.defaultChannel, severity: "notice",
           firesPerWindow: 3, windowMs: 3_600_000, enabled: true,
           levelText: "", op: ">=", spreadBpText: "",
           depthUsdText: PARAM_DEFAULTS.depthUsd, absUsdText: PARAM_DEFAULTS.absUsd,
           hoursText: PARAM_DEFAULTS.hours };
}

/** A whole-number field, or a sentence saying what it wanted. `centsFromDecimal`/`Number.parseInt` are used
 *  rather than `Number(...)`: `Number("10000 USDC")` is NaN and `Number("")` is 0, and a rule saved with a
 *  silent zero is a rule that fires on everything or nothing. */
function wholeText(text: string): number | string {
  const trimmed = text.trim().replace(/[,\s]/g, "");
  if (!/^\d+$/.test(trimmed)) return t("terminal.alerts.problem.wholeNumber");
  return Number.parseInt(trimmed, 10);
}

/** The per-kind condition problems, on their own so the test can ask about them without the rest. */
export function paramProblems(draft: Draft): string[] {
  const out: string[] = [];
  const needs = KIND_PARAMS[draft.kind] ?? [];
  if (needs.includes("priceMicro")) {
    const raw = draft.levelText.trim();
    if (!raw) {
      out.push(t("terminal.alerts.problem.level"));
    } else {
      const micro = priceMicroFromText(raw);
      if (micro === null || micro < 1 || micro > 999_999) out.push(t("terminal.alerts.problem.levelRange"));
    }
    if (!OPS.includes(draft.op as (typeof OPS)[number])) out.push(t("terminal.alerts.problem.op"));
  }
  if (needs.includes("spreadBp")) {
    const bp = wholeText(draft.spreadBpText);
    if (typeof bp !== "number" || bp < 1 || bp > 10_000) out.push(t("terminal.alerts.problem.spread"));
  }
  if (needs.includes("minDepthUsdMicro")) {
    const usd = wholeText(draft.depthUsdText);
    if (typeof usd !== "number" || usd < 1) out.push(t("terminal.alerts.problem.depth"));
  }
  if (needs.includes("absUsdMicro")) {
    const usd = wholeText(draft.absUsdText);
    if (typeof usd !== "number" || usd < 1) out.push(t("terminal.alerts.problem.notional"));
  }
  if (needs.includes("hours")) {
    const h = wholeText(draft.hoursText);
    if (typeof h !== "number" || h < 1 || h > 720) out.push(t("terminal.alerts.problem.hours"));
  }
  return out;
}

/** The wire params for this draft, in the API's own names. Money goes through the money layer: `0.62` is 620000
 *  micro, and the conversion is done once, here. */
export function draftParams(draft: Draft): Record<string, unknown> {
  const needs = KIND_PARAMS[draft.kind] ?? [];
  const out: Record<string, unknown> = {};
  if (needs.includes("priceMicro")) {
    const micro = priceMicroFromText(draft.levelText.trim());
    // Left out when it does not parse: `paramProblems` is what refuses it, and the API refuses it again if it
    // arrives anyway. A zero here would be a level nobody chose.
    if (micro !== null) out.priceMicro = micro;
    out.op = draft.op;
  }
  if (needs.includes("spreadBp")) {
    const bp = wholeText(draft.spreadBpText);
    if (typeof bp === "number") out.spreadBp = bp;
  }
  if (needs.includes("minDepthUsdMicro")) {
    const usd = wholeText(draft.depthUsdText);
    if (typeof usd === "number") out.minDepthUsdMicro = centsToMicro(centsFromDecimal(String(usd)));
  }
  if (needs.includes("absUsdMicro")) {
    const usd = wholeText(draft.absUsdText);
    if (typeof usd === "number") out.absUsdMicro = centsToMicro(centsFromDecimal(String(usd)));
  }
  if (needs.includes("hours")) {
    const h = wholeText(draft.hoursText);
    if (typeof h === "number") out.hours = h;
  }
  return out;
}

/** Whether a loop is watching this rule, in the server's own sentence. A rule that fires only when its owner
 *  presses a button has to say so: the alternative is a user waiting for an alert that was never coming. */
export function evaluatorText(rule: AlertRule): string {
  return rule.evaluator;
}

/**
 * The editor's own refusals, in the same shape the API uses (`where` + a sentence), so the screen can show the
 * problem before the request is sent and the two vocabularies cannot drift.
 */
export function draftProblems(draft: Draft, plan: string): string[] {
  const problems: string[] = [];
  // The condition first: it is the reason the rule exists, and a rule with no condition is the one kind of
  // problem a user cannot see from the list.
  for (const p of paramProblems(draft)) problems.push(p);
  if (!draft.marketId.trim()) problems.push(t("terminal.alerts.problem.target"));
  if (!Number.isInteger(draft.firesPerWindow) || draft.firesPerWindow < 1 || draft.firesPerWindow > 24) {
    problems.push(t("terminal.alerts.problem.fires"));
  }
  if (!Number.isInteger(draft.windowMs) || draft.windowMs < 60_000) problems.push(t("terminal.alerts.problem.window"));
  const need: Record<string, string> = { telegram: "free", email: "trader", webhook: "pro" };
  const rank: Record<string, number> = { free: 0, trial: 1, trader: 2, pro: 3, team: 4 };
  if ((rank[plan] ?? 0) < (rank[need[draft.channel] ?? "free"] ?? 0)) {
    problems.push(t("terminal.alerts.problem.plan", { channel: draft.channel, plan: need[draft.channel] ?? "pro" }));
  }
  return problems;
}

export function draftPayload(draft: Draft): Record<string, unknown> {
  const params = draftParams(draft);
  return {
    kind: draft.kind,
    marketId: draft.marketId.trim(),
    channel: draft.channel,
    severity: draft.severity,
    firesPerWindow: draft.firesPerWindow,
    windowMs: draft.windowMs,
    enabled: draft.enabled,
    ...(draft.ruleId ? { ruleId: draft.ruleId } : {}),
    ...(Object.keys(params).length > 0 ? { params } : {}),
  };
}

/** The quiet-hours editor's payload: seconds since midnight in the user's offset, or off. Values are minute
 *  counts because that is what the column holds, and a control that sent "22:00" would be parsed by somebody's
 *  guess about timezones. */
export function settingsPayload(draft: { quietStartMin: number; quietEndMin: number; digestMode: string; digestAtMin: number; defaultChannel: string; tzOffsetMin: number }): Record<string, unknown> {
  return {
    quietStartMin: draft.quietStartMin,
    quietEndMin: draft.quietEndMin,
    digestMode: draft.digestMode,
    digestAtMin: draft.digestAtMin,
    defaultChannel: draft.defaultChannel,
    tzOffsetMin: draft.tzOffsetMin,
  };
}

export function settingsProblems(draft: { quietStartMin: number; quietEndMin: number; digestMode: string; digestAtMin: number }): string[] {
  const problems: string[] = [];
  const off = draft.quietStartMin < 0 && draft.quietEndMin < 0;
  if (!off && (draft.quietStartMin < 0 || draft.quietEndMin < 0)) problems.push(t("terminal.alerts.problem.quietEnds"));
  if (!off && draft.quietStartMin === draft.quietEndMin) problems.push(t("terminal.alerts.problem.quietZero"));
  if (draft.digestMode === "daily" && (draft.digestAtMin < 0 || draft.digestAtMin > 1439)) {
    problems.push(t("terminal.alerts.problem.digestAt"));
  }
  return problems;
}

/**
 * Channels the test-fire button offers: only the ones this plan can deliver on. Offering a channel that the
 * plan refuses turns a test into a mystery — the button appears to work and the alert never arrives.
 */
export function testableChannels(settings: NotificationSettings, plan: string): string[] {
  return settings.channels.map((c) => c.channel).filter((ch) => channelAllowed(ch, plan));
}

export function channelAllowed(channel: string, plan: string): boolean {
  const need: Record<string, string> = { telegram: "free", email: "trader", webhook: "pro" };
  const rank: Record<string, number> = { free: 0, trial: 1, trader: 2, pro: 3, team: 4 };
  return (rank[plan] ?? 0) >= (rank[need[channel] ?? "free"] ?? 0);
}

/** A test fire's result, as one sentence plus the per-channel decisions. It never says "sent": no transport
 *  runs in this build, so the honest word is "would". */
export function testFireSummary(plan: AlertPlanRow[]): string {
  const sends = plan.filter((p) => p.decision === "send_now");
  if (sends.length > 0) return t("terminal.alerts.test.wouldSend", { channels: sends.map((p) => p.channel).join(", ") });
  // A window-capped alert is HELD, not refused: the rule's own budget is a delay, and a screen that said "not
  // delivered" for it would send a user looking for a broken channel. Only a plan refusal or a dead channel is
  // a refusal.
  const held = plan.filter((p) => p.status === "digest_scheduled" || p.status === "dropped_rate_limited");
  if (held.length > 0) return t("terminal.alerts.test.held", { why: held.map((p) => p.sentence).join("; ") });
  return t("terminal.alerts.test.refused", { why: plan.map((p) => p.sentence).join("; ") });
}

/** Test rows are separated from real ones. A test that appeared in the history as a delivery would make the
 *  history a to-do list of things that never happened. */
export function splitHistory(rows: AlertDeliveryRow[]): { real: AlertDeliveryRow[]; tests: AlertDeliveryRow[] } {
  return { real: rows.filter((r) => !r.isTest), tests: rows.filter((r) => r.isTest) };
}

export function historyLatencyText(row: AlertDeliveryRow): string {
  if (row.latencyMs === null) return t("terminal.alerts.latency.none");
  return t("terminal.alerts.latency.value", { ms: durationText(row.latencyMs) });
}

export function countsText(payload: AlertsPayload): string {
  const capped = payload.rules.filter((r) => r.remainingInWindow === 0).length;
  return t("terminal.alerts.counts", { rules: payload.rules.length, capped, plan: payload.plan });
}
