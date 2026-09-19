/**
 * D8 · the automation screen's rules.
 *
 * The engine decides whether a rule fires; this module decides what the screen may *offer* and how it explains
 * what happened. Two properties are the whole point, and both are testable without a browser:
 *
 *  1. **The builder cannot express something the engine refuses.** `builderProblems` is the client half of the
 *     same checks `automation.console.compile_builder` runs on the server, and `draftPayload` emits rows and a
 *     joiner — never a string, never an expression. A form that let a user save a rule the engine will reject at
 *     fire time would produce exactly the silent failure D8 exists to prevent: a rule that looks armed and does
 *     nothing.
 *  2. **A withheld template is shown with its arithmetic.** The 5-minute crypto entry template ships only if the
 *     fee maths clears at current fees; when it does not, the catalog still lists it, marks it unavailable, and
 *     carries the numbers that withheld it. Hiding it would make a user guess whether the feature is missing or
 *     the maths said no — and the second is worth reading.
 *
 * The status colours are not here: `ruleStatusLabel` returns words, because "why is this not firing" is a
 * question about a sentence, and a badge with a colour and no sentence is a badge nobody can act on.
 */
import { t } from "@/i18n/terminal";
import { microToCents } from "@/money/cents";
import type {
  AutomationList,
  AutomationRule,
  AutomationRunRow,
  BuilderField,
  BuilderVocabulary,
  HaltState,
  TemplateCatalog,
} from "./wire";

export const STATUS_LABEL: Record<string, string> = {
  active: t("terminal.automation.status.active"),
  paused: t("terminal.automation.status.paused"),
  dry_run: t("terminal.automation.status.dryRun"),
  halted: t("terminal.automation.status.halted"),
};

export function ruleStatusLabel(rule: AutomationRule): string {
  return STATUS_LABEL[rule.status] ?? rule.status;
}

export function haltBanner(halt: HaltState | null): { show: boolean; title: string; body: string; hint: string } {
  if (!halt) return { show: false, title: "", body: "", hint: "" };
  return {
    show: true,
    title: t("terminal.automation.halt.title"),
    body: halt.note,
    hint: halt.acknowledgeHint,
  };
}

/** "when will this rule look again" — and the honest answer for a rule that has never fired is "as soon as the
 *  engine next runs", not a countdown measured from a fire that never happened. */
export function nextEvalText(rule: AutomationRule, nowMs: number): string {
  if (rule.status === "paused" || rule.status === "halted") return t("terminal.automation.next.notRunning");
  const delta = rule.nextEvaluationMs - nowMs;
  if (!rule.lastFiredMs || delta <= 0) return t("terminal.automation.next.now");
  return t("terminal.automation.next.in", { wait: durationText(delta) });
}

export function lastFiredText(rule: AutomationRule, nowMs: number): string {
  if (!rule.lastFiredMs) return t("terminal.automation.lastFired.never");
  return t("terminal.automation.lastFired.ago", { ago: durationText(Math.max(0, nowMs - rule.lastFiredMs)) });
}

export function durationText(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return t("terminal.automation.duration.seconds", { s });
  const m = Math.floor(s / 60);
  if (m < 60) return t("terminal.automation.duration.minutes", { m, s: s % 60 });
  const h = Math.floor(m / 60);
  return t("terminal.automation.duration.hours", { h, m: m % 60 });
}

/** A run row as one sentence: the API's own wording plus the leaf values when the trigger was read. */
export function runSentence(run: AutomationRunRow): string {
  return run.leaves ? `${run.sentence} · ${run.leaves}` : run.sentence;
}

export function runOutcomeLabel(outcome: string): string {
  const map: Record<string, string> = {
    placed: t("terminal.automation.outcome.placed"),
    would_place: t("terminal.automation.outcome.wouldPlace"),
    skipped: t("terminal.automation.outcome.skipped"),
    failed: t("terminal.automation.outcome.failed"),
  };
  return map[outcome] ?? outcome;
}

export function capsText(list: AutomationList): string {
  return t("terminal.automation.caps", {
    active: list.caps.activeRules,
    cap: list.caps.concurrentRuleCap,
    global: list.caps.globalRunsPerDay,
  });
}

/** Which templates a user may attach right now, and which are withheld. Returned as two lists so the screen
 *  cannot accidentally render a withheld one as a button. */
export function templateSplit(catalog: TemplateCatalog | null): { usable: TemplateCatalog["templates"]; withheld: TemplateCatalog["templates"] } {
  const all = catalog?.templates ?? [];
  return { usable: all.filter((x) => x.available), withheld: all.filter((x) => !x.available) };
}

/** The fee arithmetic, in the order it has to be read: what the venue charges, what that costs per share, what
 *  break-even win rate it implies, and what edge the entry needs versus what we measured. */
export function feeArithmeticLines(arith: TemplateCatalog["feeArithmetic"] | null): { label: string; kind: "percent" | "money" | "count" | "text"; value: number | string | null }[] {
  if (!arith) return [];
  const bps = (v: unknown) => (typeof v === "number" ? v / 100 : null);
  return [
    { label: t("terminal.automation.fee.feeType"), kind: "text", value: (arith.feeType as string) ?? null },
    { label: t("terminal.automation.fee.feeRate"), kind: "percent", value: bps(arith.feeRateBps) },
    { label: t("terminal.automation.fee.feesPerShare"), kind: "money", value: typeof arith.feesPerShareMicro === "number" ? microToCents(arith.feesPerShareMicro) : null },
    { label: t("terminal.automation.fee.spreadCost"), kind: "money", value: typeof arith.spreadCostMicro === "number" ? microToCents(arith.spreadCostMicro) : null },
    { label: t("terminal.automation.fee.latency"), kind: "text", value: typeof arith.latencyMs === "number" ? `${arith.latencyMs} ms` : null },
    { label: t("terminal.automation.fee.breakEven"), kind: "percent", value: bps(arith.breakEvenWinRateBp) },
    { label: t("terminal.automation.fee.edgeNeeded"), kind: "percent", value: bps(arith.edgeNeededBp) },
    { label: t("terminal.automation.fee.edgeAvailable"), kind: "percent", value: bps(arith.edgeAvailableBp) },
  ];
}

export type DraftRow = { id: string; kind: string; fields: Record<string, string | number | boolean> };
export type BuilderDraft = {
  kind: string;
  name: string;
  match: "all" | "any";
  triggers: DraftRow[];
  actions: DraftRow[];
  marketId: string;
  maxPerDay: number;
  minIntervalMs: number;
  maxLossMicro: number;
};

let rowSeq = 0;

export function newRow(kind: string, fields: Record<string, string | number | boolean> = {}): DraftRow {
  rowSeq += 1;
  return { id: `row-${rowSeq}`, kind, fields };
}

export function emptyDraft(vocab: BuilderVocabulary | null): BuilderDraft {
  const trigger = vocab?.triggers.find((x) => x.kind === "price_cross")?.kind ?? vocab?.triggers[0]?.kind ?? "price_cross";
  const action = vocab?.actions.find((x) => x.kind === "limit")?.kind ?? vocab?.actions[0]?.kind ?? "limit";
  return {
    kind: "entry",
    name: "",
    match: "all",
    triggers: [newRow(trigger, defaultsFor(vocab, "triggers", trigger))],
    actions: [newRow(action, defaultsFor(vocab, "actions", action))],
    marketId: "",
    maxPerDay: 24,
    minIntervalMs: 60_000,
    maxLossMicro: 5_000_000,
  };
}

function defaultsFor(vocab: BuilderVocabulary | null, side: "triggers" | "actions", kind: string): Record<string, string | number | boolean> {
  const fields = (vocab?.[side] ?? []).find((x) => x.kind === kind)?.fields ?? [];
  const out: Record<string, string | number | boolean> = {};
  for (const f of fields) {
    if (f.default !== undefined && (typeof f.default === "string" || typeof f.default === "number" || typeof f.default === "boolean")) {
      out[f.name] = f.default;
    }
  }
  return out;
}

export function fieldsFor(vocab: BuilderVocabulary | null, side: "triggers" | "actions", kind: string): BuilderField[] {
  return (vocab?.[side] ?? []).find((x) => x.kind === kind)?.fields ?? [];
}

/** Leaves are what the engine counts; `MAX_LEAVES` is 8 and the builder must refuse the ninth here, where the
 *  user can still fix it, rather than at save time with a 422 naming a field they cannot see. */
export function leafCount(draft: BuilderDraft): number {
  return draft.triggers.length + draft.actions.length;
}

/**
 * The client half of `validate_rule`: required fields, the leaf cap, at least one target, and the two numbers a
 * rule cannot be saved without. Every message names the row it is about.
 */
export function builderProblems(draft: BuilderDraft, vocab: BuilderVocabulary | null): string[] {
  const problems: string[] = [];
  const leaves = leafCount(draft);
  const maxLeaves = vocab?.limits.maxLeaves ?? 8;
  if (draft.triggers.length === 0) problems.push(t("terminal.automation.problem.noTrigger"));
  if (draft.actions.length === 0) problems.push(t("terminal.automation.problem.noAction"));
  if (leaves > maxLeaves) problems.push(t("terminal.automation.problem.tooManyLeaves", { n: leaves, max: maxLeaves }));
  if (!draft.marketId.trim()) problems.push(t("terminal.automation.problem.noMarket"));
  const missing = (side: "triggers" | "actions") => {
    for (const [i, row] of draft[side].entries()) {
      for (const f of fieldsFor(vocab, side, row.kind)) {
        const v = row.fields[f.name];
        if (f.required && (v === undefined || v === "" || v === null)) {
          problems.push(t("terminal.automation.problem.missing", { side, n: i + 1, field: f.label }));
        }
      }
    }
  };
  missing("triggers");
  missing("actions");
  if (!Number.isInteger(draft.maxPerDay) || draft.maxPerDay < 1 || draft.maxPerDay > 288) {
    problems.push(t("terminal.automation.problem.maxPerDay"));
  }
  if (!Number.isInteger(draft.minIntervalMs) || draft.minIntervalMs < (vocab?.limits.minIntervalMs ?? 60_000)) {
    problems.push(t("terminal.automation.problem.minInterval", { ms: vocab?.limits.minIntervalMs ?? 60_000 }));
  }
  if (draft.maxLossMicro <= 0) problems.push(t("terminal.automation.problem.maxLoss"));
  return problems;
}

/** Rows + joiner, never an expression. `targets` is one market here because the builder's UI picks one; the API
 *  accepts a list, so a future multi-market builder does not need a new payload shape. */
export function draftPayload(draft: BuilderDraft): Record<string, unknown> {
  const clean = (row: DraftRow) => ({ kind: row.kind, ...row.fields });
  return {
    kind: draft.kind,
    ...(draft.name.trim() ? { name: draft.name.trim() } : {}),
    match: draft.match,
    triggers: draft.triggers.map(clean),
    actions: draft.actions.map(clean),
    targets: [{ marketId: draft.marketId.trim() }],
    maxPerDay: draft.maxPerDay,
    minIntervalMs: draft.minIntervalMs,
    maxLossMicro: draft.maxLossMicro,
  };
}

/** The dry-run preview payload: a saved rule is previewed by id, a draft by its compiled rows. */
export function previewPayload(draft: BuilderDraft, ruleId?: string): Record<string, unknown> {
  if (ruleId) return { ruleId };
  const { name: _name, ...rest } = draftPayload(draft);
  return rest;
}

/** The action rows' human summary, used in the list and the confirm step. */
export function actionSummary(actions: { kind: string }[]): string {
  if (!actions.length) return t("terminal.automation.action.none");
  return actions.map((a) => a.kind).join(", ");
}
