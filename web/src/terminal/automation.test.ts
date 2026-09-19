import { describe, expect, it } from "vitest";
import { t } from "@/i18n/terminal";
import {
  builderProblems,
  capsText,
  draftPayload,
  emptyDraft,
  feeArithmeticLines,
  fieldsFor,
  haltBanner,
  lastFiredText,
  leafCount,
  nextEvalText,
  ruleStatusLabel,
  runSentence,
  templateSplit,
} from "./automation";
import type { AutomationList, AutomationRule, BuilderVocabulary, TemplateCatalog } from "./wire";

const VOCAB: BuilderVocabulary = {
  triggers: [
    { kind: "price_cross", label: "a price crosses a level you name", fields: [
      { name: "uses", label: "price", type: "select", options: ["mid", "last"], default: "mid" },
      { name: "op", label: "crosses", type: "select", options: ["<", "<=", ">", ">="], default: "<=" },
      { name: "price_micro", label: "level", type: "price", required: true },
    ] },
    { kind: "time", label: "a clock time arrives", fields: [{ name: "at_ms", label: "at", type: "time_ms", required: true }] },
  ],
  actions: [
    { kind: "limit", label: "place a limit order at a price you name", fields: [
      { name: "side", label: "side", type: "select", options: ["BUY", "SELL"], required: true },
      { name: "price_micro", label: "limit price", type: "price", required: true },
      { name: "size_shares_micro", label: "size", type: "shares", required: true },
    ] },
    { kind: "cancel_open", label: "cancel the orders this rule's market already has", fields: [
      { name: "scope", label: "cancel", type: "select", options: ["order", "batch", "market"], default: "market", required: true },
    ] },
  ],
  joiners: { all: "AND", any: "OR" },
  limits: { maxLeaves: 8, maxNestDepth: 2, minIntervalMs: 60_000, maxRunsPerDay: 288 },
  note: "every row is a literal",
};

/** A draft that passes every check, so a test can break exactly one thing and see exactly one problem. */
function completeDraft(): ReturnType<typeof emptyDraft> {
  const draft = emptyDraft(VOCAB);
  draft.marketId = "0xM1";
  draft.triggers = [{ id: "t1", kind: "price_cross", fields: { uses: "mid", op: "<=", price_micro: 420000 } }];
  draft.actions = [{ id: "a1", kind: "limit", fields: { side: "BUY", price_micro: 425000, size_shares_micro: 5_000_000 } }];
  return draft;
}

/** The envelope fields every read carries. A fixture without them is a fixture of a response the API cannot
 *  send, which is why they are here rather than optional in the wire types. */
const STAMP = { asOf: 1_789_000_000_000, serverAsOf: 1_789_000_000_000,
                staleAfter: 1_789_000_003_000, cache: { ttlMs: 3_000, public: false } };

function rule(over: Partial<AutomationRule> = {}): AutomationRule {
  return {
    ruleId: "rule-1",
    name: "exit before resolution",
    kind: "exit",
    status: "dry_run",
    statusWhy: "no completed dry run yet, so it cannot go live",
    mode: "dry_run",
    enabled: false,
    pausedReason: "",
    failureCount: 0,
    lastError: "",
    dryRunCompletedMs: null,
    lastFiredMs: null,
    lastEvaluationMs: null,
    nextEvaluationMs: 1_000,
    runsToday: 0,
    maxPerDay: 24,
    minIntervalMs: 60_000,
    humanPriorityMs: 120_000,
    maxLossMicro: 5_000_000,
    targets: [{ marketId: "0xM1", tokenId: "" }],
    trigger: { any: [{ kind: "time", at_ms: 0, once: true }] },
    actions: [{ kind: "close_position" }],
    lastRun: null,
    ...over,
  };
}

describe("D8 · the builder cannot express what the engine refuses", () => {
  it("refuses the ninth row with the engine's own limit, before anything is sent", () => {
    const draft = emptyDraft(VOCAB);
    draft.marketId = "0xM1";
    draft.triggers = Array.from({ length: 6 }, (_, i) => ({ id: `t${i}`, kind: "price_cross", fields: { op: "<=", price_micro: 500000 } }));
    draft.actions = Array.from({ length: 3 }, (_, i) => ({ id: `a${i}`, kind: "cancel_open", fields: { scope: "market" } }));
    const problems = builderProblems(draft, VOCAB);
    expect(leafCount(draft)).toBe(9);
    expect(problems.join(" ")).toContain("8");
    expect(problems.join(" ")).toContain("9");
  });

  it("sends rows and a joiner, and every value is a literal the vocabulary offers", () => {
    const draft = completeDraft();
    draft.match = "any";
    draft.triggers = [
      { id: "t1", kind: "price_cross", fields: { uses: "mid", op: "<=", price_micro: 420000 } },
      { id: "t2", kind: "time", fields: { at_ms: 1_800_000_000_000 } },
    ];
    const payload = draftPayload(draft);
    expect(builderProblems(draft, VOCAB)).toEqual([]);
    expect(payload.match).toBe("any");
    const triggers = payload.triggers as Record<string, unknown>[];
    expect(triggers.map((x) => x.kind)).toEqual(["price_cross", "time"]);
    // No expressions: a string field is either a select option the vocabulary lists, or it is not a field at
    // all. `op: "<="` is a literal from the form's own select, not syntax a user typed.
    for (const row of triggers) {
      for (const [name, value] of Object.entries(row)) {
        if (name === "kind" || typeof value !== "string") continue;
        const field = fieldsFor(VOCAB, "triggers", String(row.kind)).find((f) => f.name === name);
        expect(field?.options ?? []).toContain(value);
      }
    }
    expect(payload.targets).toEqual([{ marketId: "0xM1" }]);
    expect(payload.maxLossMicro).toBe(5_000_000);
  });

  it("names the row and the field that is missing rather than refusing anonymously", () => {
    const draft = completeDraft();
    draft.triggers = [{ id: "t1", kind: "price_cross", fields: { uses: "mid", op: "<=" } }];
    const problems = builderProblems(draft, VOCAB);
    expect(problems).toHaveLength(1);
    // The sentence names the row AND the field. The field's label comes from the vocabulary itself, so this test
    // fails if the two drift rather than passing on a string that happens to look right.
    const row = draft.triggers[0];
    const missing = fieldsFor(VOCAB, "triggers", "price_cross")
      .filter((f) => f.required && !(f.name in (row?.fields ?? {})));
    expect(missing).toHaveLength(1);
    expect(problems[0] ?? "").toContain("triggers");
    expect(problems[0] ?? "").toContain(missing[0]?.label ?? "");
  });

  it("refuses an empty draft for every reason at once: a form names all its problems, not the first", () => {
    // "one error per submit" teaches people to submit four times, which is why the engine returns a list too.
    const problems = builderProblems(emptyDraft(VOCAB), VOCAB);
    expect(problems.join(" | ")).toContain(t("terminal.automation.problem.noMarket"));
    expect(problems.length).toBeGreaterThan(1);
  });

  it("refuses a rule with no market even when every row is complete", () => {
    const draft = completeDraft();
    draft.marketId = "";
    expect(builderProblems(draft, VOCAB)).toEqual([t("terminal.automation.problem.noMarket")]);
  });
});

describe("D8 · status, halt and the honest clock", () => {
  it("reads a halted rule as halted even when it is still enabled", () => {
    expect(ruleStatusLabel(rule({ status: "halted", enabled: true }))).toBe(t("terminal.automation.status.halted"));
  });

  it("shows the halt banner for a halted account and nothing at all otherwise", () => {
    expect(haltBanner(null).show).toBe(false);
    const banner = haltBanner({ halted: true, thresholdMicro: 1_000_000_000, realizedMicro: -1_200_000_000,
                                lossMicro: 200_000_000, trippedMs: 1, acknowledgeHint: "acknowledge in the risk panel",
                                note: "the daily loss limit tripped" });
    expect(banner.show).toBe(true);
    expect(banner.body).toContain("daily loss limit");
    expect(banner.hint).toContain("acknowledge");
  });

  it("says a paused rule is not running instead of counting down to a fire", () => {
    expect(nextEvalText(rule({ status: "paused" }), 2_000)).toBe(t("terminal.automation.next.notRunning"));
    expect(nextEvalText(rule({ status: "active", lastFiredMs: null }), 2_000)).toBe(t("terminal.automation.next.now"));
    expect(nextEvalText(rule({ status: "active", lastFiredMs: 1_000, nextEvaluationMs: 61_000 }), 1_000)).toContain("1m");
  });

  it("says 'never fired' rather than showing a zero timestamp", () => {
    expect(lastFiredText(rule(), 5_000)).toBe(t("terminal.automation.lastFired.never"));
    expect(lastFiredText(rule({ lastFiredMs: 5_000 }), 65_000)).toContain("1m");
  });

  it("carries the trigger's leaf values into the run sentence, because that is the answer to why", () => {
    const sentence = runSentence({ ruleId: "r", mode: "dry_run", outcome: "skipped", reason: "trigger not met",
                                   denyCode: "", intentId: "", atMs: 1,
                                   leaves: "price_cross not met (430000)", sentence: "skipped: trigger not met" });
    expect(sentence).toContain("trigger not met");
    expect(sentence).toContain("price_cross not met");
  });

  it("counts armed rules against the cap from the API's own numbers", () => {
    const list: AutomationList = { ...STAMP, rules: [rule({ status: "active" })], halt: null,
                                   caps: { concurrentRuleCap: 10, globalRunsPerDay: 288, activeRules: 1, note: "" },
                                   vocabulary: VOCAB, note: "" };
    expect(capsText(list)).toContain("1 of 10");
    expect(capsText(list)).toContain("288");
  });
});

describe("D8 · the 5-minute template is shown WITH the arithmetic when the maths withholds it", () => {
  const catalog: TemplateCatalog = {
    ...STAMP,
    ships: false,
    verdict: "ENTRY RULE NOT SHIPPED (protective half ships)",
    why: "the measured edge (12 bps) does not clear the hurdle (185 bps)",
    blockingReason: "the measured edge",
    feeArithmetic: { feeType: "taker", feeRateBps: 200, feesPerShareMicro: 8_400, spreadCostMicro: 10_000,
                     latencyMs: 900, breakEvenWinRateBp: 5_200, edgeNeededBp: 185, edgeAvailableBp: 12,
                     latencyCostBp: 2 },
    templates: [
      { templateId: "entry-momentum-5m", name: "5-minute crypto entry (momentum)", kind: "entry",
        trigger: { all: [{ kind: "book_imbalance", op: ">=", imbalance_bps: 7000 }] },
        actions: [{ kind: "limit" }], available: false, verdict: "ENTRY RULE NOT SHIPPED (protective half ships)",
        blockingReason: "the measured edge", why: "the measured edge does not clear the hurdle",
        feeArithmetic: { feeType: "taker", feeRateBps: 200, feesPerShareMicro: 8_400, edgeNeededBp: 185,
                         edgeAvailableBp: 12 } },
      { templateId: "protect-exit-before-resolution", name: "exit before resolution", kind: "exit",
        trigger: { any: [{ kind: "time", at_ms: 0, once: true }] }, actions: [{ kind: "close_position" }],
        available: true, verdict: "SHIPPED", blockingReason: "", why: "ships because it can only reduce a loss",
        feeArithmetic: null },
    ],
  };

  it("never offers a withheld template as usable", () => {
    const { usable, withheld } = templateSplit(catalog);
    expect(usable.map((x) => x.templateId)).toEqual(["protect-exit-before-resolution"]);
    expect(withheld.map((x) => x.templateId)).toEqual(["entry-momentum-5m"]);
  });

  it("keeps a withheld template visible with its numbers, because the maths is worth reading", () => {
    const entry = catalog.templates.find((x) => x.templateId === "entry-momentum-5m");
    const lines = feeArithmeticLines(entry?.feeArithmetic ?? null);
    const byLabel = Object.fromEntries(lines.map((l) => [l.label, l]));
    const value = (key: string): number => Number(byLabel[key]?.value ?? NaN);
    expect(value(t("terminal.automation.fee.feeRate"))).toBeCloseTo(2);
    expect(value(t("terminal.automation.fee.edgeNeeded"))).toBeCloseTo(1.85);
    expect(value(t("terminal.automation.fee.edgeAvailable"))).toBeCloseTo(0.12);
    // The entry's needed edge is above what was measured — which is exactly why the button is not there.
    expect(value(t("terminal.automation.fee.edgeNeeded"))).toBeGreaterThan(value(t("terminal.automation.fee.edgeAvailable")));
  });

  it("reports an unmeasured field as absent, never as zero", () => {
    const lines = feeArithmeticLines({ feeType: "taker", feeRateBps: 200 });
    const available = lines.find((l) => l.label === t("terminal.automation.fee.edgeAvailable"));
    expect(available?.value).toBeNull();
  });
});
