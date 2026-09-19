import { describe, expect, it } from "vitest";
import { t } from "@/i18n/terminal";
import {
  channelText,
  clockOf,
  cooldownAgrees,
  cooldownFor,
  countsText,
  draftFrom,
  draftParams,
  draftPayload,
  draftProblems,
  durationText,
  evaluatorText,
  paramProblems,
  PARAM_DEFAULTS,
  historyLatencyText,
  quietHoursText,
  settingsProblems,
  splitHistory,
  statusLabel,
  testFireSummary,
  testableChannels,
  wouldDoText,
} from "./alerts";
import type { AlertDeliveryRow, AlertPlanRow, AlertRule, AlertsPayload, NotificationSettings } from "./wire";

/** The envelope fields every read carries. A fixture without them is a fixture of a response the API cannot
 *  send, which is why they are here rather than optional in the wire types. */
const STAMP = { asOf: 1_789_000_000_000, serverAsOf: 1_789_000_000_000,
                staleAfter: 1_789_000_003_000, cache: { ttlMs: 3_000, public: false } };

function settings(over: Partial<NotificationSettings> = {}): NotificationSettings {
  return {
    quietStartMin: -1,
    quietEndMin: -1,
    tzOffsetMin: 330,
    digestMode: "off",
    digestAtMin: 480,
    defaultChannel: "telegram",
    channels: [
      { channel: "telegram", plan: "free", isDefault: true },
      { channel: "email", plan: "trader", isDefault: false },
      { channel: "webhook", plan: "pro", isDefault: false },
    ],
    quietHours: { configured: false, active: false, untilMs: null, note: t("terminal.alerts.quietOff") },
    digestNow: { mode: "off", deferred: false, atMs: null, note: "" },
    note: "",
    ...over,
  };
}

function rule(over: Partial<AlertRule> = {}): AlertRule {
  return {
    ruleId: "al-1",
    kind: "whale_fill",
    target: { marketId: "0xM1", eventId: null },
    enabled: true,
    severity: "notice",
    channel: "telegram",
    channelAllowed: true,
    channelNote: "",
    firesPerWindow: 3,
    windowMs: 3_600_000,
    cooldownMs: 1_200_000,
    cooldownRule: "3 per 1h = one every 20m at most",
    cooldownNote: "0 of 3 fires used in the current window",
    firesInWindow: 0,
    remainingInWindow: 3,
    nextAllowedMs: null,
    quietHours: { configured: false, active: false, untilMs: null, note: t("terminal.alerts.quietOff") },
    digest: { mode: "off", deferred: false, atMs: null, note: "" },
    wouldDoNow: { channel: "telegram", decision: "send_now", status: "queued", reason: "", atMs: 1,
                  sentence: "queued for telegram" },
    lastDelivery: null,
    params: {},
    engineKind: "large_fill",
    evaluated: true,
    evaluator: "the ingest engine evaluates this rule and records every fire in the history below",
    createdMs: 1,
    ...over,
  };
}

describe("D9 · the cooldown is the window budget, stated as a rule", () => {
  it("turns three fires an hour into the rule a user can check", () => {
    const out = cooldownFor(3, 3_600_000);
    expect(out.cooldownMs).toBe(1_200_000);
    expect(out.rule).toBe(t("terminal.alerts.cooldownRule", { fires: 3, window: "1h", cooldown: "20m" }));
  });

  it("agrees with the server's own number, and would say so if it did not", () => {
    expect(cooldownAgrees(rule())).toBe(true);
    expect(cooldownAgrees(rule({ cooldownMs: 60_000 }))).toBe(false);
  });

  it("writes durations in the unit a person thinks in", () => {
    expect(durationText(45_000)).toBe("45s");
    expect(durationText(90_000)).toBe("1m 30s");
    expect(durationText(7_200_000)).toBe("2h");
  });
});

describe("D9 · the editor refuses in the form what the API would refuse", () => {
  it("refuses a webhook rule on a free plan and names the plan, before the request", () => {
    const draft = draftFrom(null, settings());
    draft.marketId = "0xM1";
    draft.channel = "webhook";
    const problems = draftProblems(draft, "free");
    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain("webhook");
    expect(problems[0]).toContain("pro");
    expect(draftProblems(draft, "pro")).toEqual([]);
  });

  it("offers only the channels this plan can actually deliver on, so a test is never a mystery", () => {
    expect(testableChannels(settings(), "free")).toEqual(["telegram"]);
    expect(testableChannels(settings(), "pro")).toEqual(["telegram", "email", "webhook"]);
  });

  it("refuses a rule with no target: a rule that cannot fire is not a rule", () => {
    const draft = draftFrom(null, settings());
    expect(draftProblems(draft, "pro").join(" ")).toContain("market");
  });

  it("refuses a cap outside 1..24 and a window under a minute", () => {
    const draft = draftFrom(null, settings());
    draft.marketId = "0xM1";
    expect(draftProblems({ ...draft, firesPerWindow: 25 }, "free").join(" ")).toContain("24");
    expect(draftProblems({ ...draft, windowMs: 30_000 }, "free").join(" ")).toContain("minute");
  });

  it("sends the rule as fields, with the rule id only when editing", () => {
    const draft = draftFrom(null, settings());
    draft.marketId = "0xM1";
    expect(draftPayload(draft)).not.toHaveProperty("ruleId");
    const edited = draftFrom(rule(), settings());
    expect(draftPayload(edited).ruleId).toBe("al-1");
  });
});

describe("D9 · quiet hours are an instruction, so nothing breaches them", () => {
  it("says so when they are off, and repeats the API's own sentence when they are on", () => {
    expect(quietHoursText(settings())).toBe(t("terminal.alerts.quietOff"));
    const quiet = settings({ quietStartMin: 1320, quietEndMin: 420,
                             quietHours: { configured: true, active: true, untilMs: 1, note: "quiet hours until 07:00 local" } });
    expect(quietHoursText(quiet)).toContain("07:00");
  });

  it("refuses a window with one end, a window that never closes, and a digest time off the clock", () => {
    expect(settingsProblems({ quietStartMin: 1320, quietEndMin: -1, digestMode: "off", digestAtMin: 480 }).join(" ")).toContain("both ends");
    expect(settingsProblems({ quietStartMin: 600, quietEndMin: 600, digestMode: "off", digestAtMin: 480 }).join(" ")).toContain("never closes");
    expect(settingsProblems({ quietStartMin: -1, quietEndMin: -1, digestMode: "daily", digestAtMin: 2000 }).join(" ")).toContain("time of day");
    expect(settingsProblems({ quietStartMin: -1, quietEndMin: -1, digestMode: "daily", digestAtMin: 480 })).toEqual([]);
  });

  it("renders clock minutes, with -1 as off rather than 23:59", () => {
    expect(clockOf(-1)).toBe(t("terminal.alerts.off"));
    expect(clockOf(90)).toBe("01:30");
    expect(clockOf(1439)).toBe("23:59");
  });
});

describe("D9 · held is not the same fact as sent", () => {
  it("labels a held alert as held", () => {
    expect(statusLabel("digest_scheduled")).toBe(t("terminal.alerts.status.held"));
    expect(statusLabel("dropped_rate_limited")).toBe(t("terminal.alerts.status.rateLimited"));
    expect(statusLabel("sent")).toBe(t("terminal.alerts.status.sent"));
  });

  it("says 'would', not 'sent', for a test fire, and names the channels", () => {
    const plan: AlertPlanRow[] = [
      { channel: "telegram", decision: "send_now", status: "queued", reason: "", atMs: 1, sentence: "queued for telegram" },
      { channel: "webhook", decision: "refused", status: "failed", reason: "webhook needs the pro plan", atMs: null,
        sentence: "not delivered on webhook: webhook needs the pro plan" },
    ];
    const sentence = testFireSummary(plan);
    expect(sentence).toContain("telegram");
    expect(sentence).not.toContain("sent");
  });

  it("explains a quiet-hours hold and a window-cap hold differently", () => {
    const quiet = testFireSummary([{ channel: "telegram", decision: "quiet_hours", status: "digest_scheduled",
                                     reason: "quiet hours until 07:00 local", atMs: 2_000,
                                     sentence: "held for quiet hours; it goes out at 07:00 local" }]);
    // The API's sentence is repeated verbatim: the screen does not re-word why an alert is quiet.
    expect(quiet).toContain("held for quiet hours; it goes out at 07:00 local");
    // The server's own sentence for a capped channel, verbatim: the note inside it is what makes the cap checkable.
    const capped = testFireSummary([{ channel: "telegram", decision: "rate_limited", status: "dropped_rate_limited",
                                      reason: "window spent", atMs: 5_000,
                                      sentence: "held by the rule's own cap: 3 of 3 fires used in the current window (next slot 5000)" }]);
    expect(capped).toContain("held, not dropped");
    expect(capped).toContain("3 of 3 fires used in the current window");
  });

  it("keeps test fires out of the real delivery history", () => {
    const row = (id: number, isTest: boolean): AlertDeliveryRow => ({ deliveryId: id, ruleId: "al-1",
      kind: "whale_fill", engineKind: "large_fill", channel: "telegram", status: "sent", reason: "",
      queuedMs: 1, sentMs: 2, latencyMs: 1, severity: "notice", title: "", isTest });
    const { real, tests } = splitHistory([row(1, false), row(2, true), row(3, false)]);
    expect(real.map((r) => r.deliveryId)).toEqual([1, 3]);
    expect(tests.map((r) => r.deliveryId)).toEqual([2]);
  });

  it("reports a never-sent row as never sent rather than as zero latency", () => {
    const row: AlertDeliveryRow = { deliveryId: 9, ruleId: "al-1", kind: "whale_fill", engineKind: "large_fill", channel: "email",
      status: "digest_scheduled", reason: "batched", queuedMs: 1, sentMs: null, latencyMs: null, severity: "notice",
      title: "", isTest: false };
    expect(historyLatencyText(row)).toBe(t("terminal.alerts.latency.none"));
    expect(historyLatencyText({ ...row, sentMs: 1_500, latencyMs: 1_499 })).toContain("1s");
  });

  it("uses the API's own sentence for what a rule would do now", () => {
    expect(wouldDoText(rule())).toBe("queued for telegram");
    expect(wouldDoText(rule({ wouldDoNow: { channel: "telegram", decision: "refused", status: "failed",
                                             reason: "the rule is paused", atMs: null, sentence: "not delivered: the rule is paused" } })))
      .toBe("not delivered: the rule is paused");
  });

  it("explains a channel the plan does not cover instead of hiding the rule", () => {
    expect(channelText(rule({ channelAllowed: false, channelNote: "webhook needs the pro plan" })))
      .toBe(t("terminal.alerts.channelNeedsPlan", { note: "webhook needs the pro plan" }));
  });

  it("counts the rules that are at their window cap", () => {
    const payload: AlertsPayload = { ...STAMP, rules: [rule(), rule({ ruleId: "al-2", remainingInWindow: 0 })],
                                     settings: settings(), plan: "free", ruleCount: 2, note: "" };
    expect(countsText(payload)).toContain("2 rules");
    expect(countsText(payload)).toContain("1 at their window cap");
  });
});

describe("D9 · the condition the engine needs is a field on the form", () => {
  it("refuses a level alert with no level, naming the field rather than the rule", () => {
    const draft = draftFrom(null, settings());
    draft.kind = "price_level";
    draft.marketId = "0xM1";
    const problems = paramProblems(draft);
    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain("price");
    // And it is not a general refusal: filling the field clears it.
    expect(paramProblems({ ...draft, levelText: "0.62" })).toEqual([]);
  });

  it("sends the level in micro, through the money layer, and never as a float", () => {
    const draft = { ...draftFrom(null, settings()), kind: "price_level", marketId: "0xM1", levelText: "0.62", op: ">=" };
    const params = draftParams(draft);
    expect(params.priceMicro).toBe(620_000);
    expect(params.op).toBe(">=");
    expect(draftPayload(draft).params).toMatchObject({ priceMicro: 620_000 });
  });

  it("refuses a level outside the price range instead of letting it through as text", () => {
    const draft = { ...draftFrom(null, settings()), kind: "price_level", levelText: "1.4" };
    expect(paramProblems(draft).join(" ")).toContain("price");
    const nonsense = { ...draft, levelText: "sixty cents" };
    expect(paramProblems(nonsense).join(" ")).toContain("price");
  });

  it("seeds the defaults the SERVER has, so a saved rule is not a number nobody chose", () => {
    const draft = draftFrom(null, settings());
    // `whale_fill` is the default kind and its gate is the engine's own default of 10,000 USDC.
    expect(draftPayload(draft).params).toMatchObject({ absUsdMicro: 10_000 * 10 ** 6 });
    expect(PARAM_DEFAULTS.hours).toBe("6");
    expect(PARAM_DEFAULTS.depthUsd).toBe("500");
  });

  it("reopens on the numbers the rule was saved with, not on the defaults", () => {
    const saved = rule({ kind: "price_level", params: { priceMicro: 620_000, op: "<=", channel: "telegram" } });
    const draft = draftFrom(saved, settings());
    expect(draft.levelText).toBe("0.6200");
    expect(draft.op).toBe("<=");
    expect(draftParams(draft)).toEqual({ priceMicro: 620_000, op: "<=" });
  });

  it("reports a whole-number field that is not a whole number", () => {
    const draft = { ...draftFrom(null, settings()), kind: "spread_widen", spreadBpText: "three hundred" };
    expect(paramProblems(draft).join(" ")).toContain("basis points");
  });

  it("says that a manual alert has no loop behind it", () => {
    const manual = rule({ kind: "manual", evaluated: false, engineKind: null,
                          evaluator: "manual: this rule fires only when you test it — no loop is watching it" });
    expect(evaluatorText(manual)).toContain("no loop is watching it");
  });
});
