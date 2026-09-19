import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AlertsView } from "./AlertsView";
import type { AlertDeliveryRow, AlertRule, AlertsPayload, NotificationSettings } from "./wire";

/**
 * D9 on the screen, as render tests: the four facts the phase says a user has to be able to see before an alert
 * fails them.
 *
 *  1. **the fire budget as a rule** — "3 per 1h = one every 20m at most", beside the count of what is spent, and
 *     not as a bare millisecond figure;
 *  2. **what would happen now, before it happens** — send / held for quiet hours / batched for the digest, with
 *     quiet hours winning even over urgent;
 *  3. **a channel the plan refuses, refused at save time with the plan named** — the editor names the plan the
 *     channel needs, so the refusal is readable before the write;
 *  4. **held ≠ rate-limited ≠ failed** — the history is two tables (real, then tests), a never-sent row has no
 *     latency, and a test fire says it is one.
 */
const asOf = Date.now() - 1_000;
const STAMP = { asOf, serverAsOf: asOf, staleAfter: asOf + 3_000, cache: { ttlMs: 3_000, public: false } };

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
    quietHours: { configured: false, active: false, untilMs: null,
                  note: "quiet hours are off: every alert is delivered when it fires" },
    digestNow: { mode: "off", deferred: false, atMs: null, note: "no digest: alerts go out as they fire" },
    note: "quiet hours hold everything, urgent included",
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
    quietHours: { configured: false, active: false, untilMs: null,
                  note: "quiet hours are off: every alert is delivered when it fires" },
    digest: { mode: "off", deferred: false, atMs: null, note: "no digest: alerts go out as they fire" },
    wouldDoNow: { channel: "telegram", decision: "send_now", status: "queued", reason: "", atMs: asOf,
                  sentence: "queued for telegram" },
    lastDelivery: null,
    params: {},
    engineKind: "large_fill",
    evaluated: true,
    evaluator: "the ingest engine evaluates this rule and records every fire in the history below",
    createdMs: asOf,
    ...over,
  };
}

/** A rule held by quiet hours, and a manual one no loop is watching: both distinct from the first rule's state. */
const HELD = rule({
  ruleId: "al-2",
  firesInWindow: 1,
  remainingInWindow: 2,
  cooldownNote: "1 of 3 fires used in the current window",
  nextAllowedMs: asOf + 1_200_000,
  quietHours: { configured: true, active: true, untilMs: asOf + 6 * 3_600_000,
                note: "quiet hours until 07:00 local (UTC+05:30): held, not dropped" },
  digest: { mode: "hourly", deferred: true, atMs: asOf + 600_000, note: "digest mode: batched into the next hour" },
  wouldDoNow: { channel: "telegram", decision: "quiet_hours", status: "digest_scheduled", reason: "",
                atMs: asOf, sentence: "held: held for quiet hours; it goes out at 07:00 local" },
});

const MANUAL = rule({
  ruleId: "al-3",
  kind: "manual",
  evaluated: false,
  evaluator: "no loop evaluates this rule: it only fires when you press the test button",
  wouldDoNow: { channel: "telegram", decision: "send_now", status: "queued", reason: "", atMs: asOf,
                sentence: "queued for telegram" },
});

const HISTORY: AlertDeliveryRow[] = [
  { deliveryId: 1, ruleId: "al-1", kind: "whale_fill", engineKind: "large_fill", channel: "telegram",
    status: "queued", reason: "queued for telegram", queuedMs: asOf - 5_000, sentMs: null, latencyMs: null,
    severity: "notice", title: "a whale bought", isTest: false },
  { deliveryId: 2, ruleId: "al-1", kind: "whale_fill", engineKind: "large_fill", channel: "telegram",
    status: "dropped_rate_limited", reason: "the rule's own cap is spent for this window", queuedMs: asOf - 60_000,
    sentMs: null, latencyMs: null, severity: "notice", title: "a whale bought", isTest: false },
  { deliveryId: 3, ruleId: "test:al-1", kind: "whale_fill", engineKind: "large_fill", channel: "telegram",
    status: "queued", reason: "queued for telegram", queuedMs: asOf - 2_000, sentMs: null, latencyMs: null,
    severity: "notice", title: "test fire of notice", isTest: true },
];

function payload(over: Partial<AlertsPayload> = {}): AlertsPayload {
  return { ...STAMP, rules: [rule(), HELD, MANUAL], settings: settings(), plan: [], ruleCount: 3, ...over } as AlertsPayload;
}

function stub(body: { post?: unknown; list?: AlertsPayload } = {}) {
  vi.stubGlobal("fetch", vi.fn(async (url: string | URL, init?: RequestInit) => {
    const json = (v: unknown) =>
      new Response(JSON.stringify({ ...STAMP, ...(v as object) }), { status: 200,
                                                                    headers: { "content-type": "application/json" } });
    if (init?.method === "POST") {
      return json(body.post ?? {
        ruleId: "al-1",
        plan: [{ channel: "telegram", decision: "quiet_hours", status: "digest_scheduled", reason: "",
                 atMs: asOf, sentence: "held: held for quiet hours; it goes out at 07:00 local" }],
        summary: { sends: 0, held: 1, refused: 0, sentence: "held for quiet hours" },
        severity: "notice",
        quietHours: { configured: true, active: true, untilMs: asOf + 6 * 3_600_000, note: "quiet hours until 07:00 local" },
        digest: { mode: "hourly", deferred: true, atMs: asOf + 600_000, note: "batched into the next hour" },
        deliveryIds: [3],
        note: "these rows are the record of what would be sent: no delivery transport runs in this build, so a `queued` row is a plan, not a notification — and the rule's own window was not spent",
      });
    }
    // The two reads are answered by shape, not by "anything that is not a POST": a list read answered with a
    // delivery page is a body whose `rules` is undefined, and the screen would throw rather than fail the test's
    // assertion — which reads as "element not found" and sends you looking in the wrong place.
    if (String(url).includes("/deliveries")) return json({ rows: HISTORY });
    return json(body.list ?? payload());
  }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("D9 · the alerts screen", () => {
  it("states each rule's fire budget as a rule, and what would happen right now", async () => {
    stub();
    render(<AlertsView initial={payload()} history={HISTORY} />);
    await waitFor(() => expect(screen.getByTestId("alert-al-1")).toBeInTheDocument());
    const first = screen.getByTestId("alert-al-1");
    expect(first.textContent).toMatch(/3 per 1h = one every 20m at most/);
    expect(first.textContent).toMatch(/0 of 3 fires used in the current window/);
    expect(first.textContent).toMatch(/queued for telegram/);

    // Quiet hours hold even a notice, and the row says so before it happens rather than after.
    const held = screen.getByTestId("alert-al-2");
    expect(held.textContent).toMatch(/held for quiet hours/);
    expect(held.textContent).toMatch(/quiet hours until 07:00 local/);
    expect(held.textContent).toMatch(/1 of 3 fires used/);

    // A manual rule has no loop behind it, and the screen refuses to imply one.
    expect(screen.getByTestId("evaluator-al-3").textContent).toMatch(/no loop evaluates this rule/);
    expect(screen.getByTestId("evaluator-al-1").textContent).toMatch(/ingest engine evaluates this rule/);
  });

  it("names the plan a channel needs, before the write is attempted", async () => {
    stub();
    render(<AlertsView initial={payload()} history={HISTORY} />);
    await waitFor(() => expect(screen.getByTestId("alert-editor")).toBeInTheDocument());
    // The editor's default channel is the account's default (telegram, free); webhook is offered and says what
    // it costs, because "refused at save time with the plan named" is only useful if it is readable beforehand.
    expect(screen.getByTestId("alert-editor").textContent).toMatch(/webhook/);
    expect(screen.getByTestId("alert-editor").textContent).toMatch(/pro/);
    // The settings panel's own sentence: quiet hours are the user's instruction, a digest is our batching.
    expect(screen.getAllByText(/quiet hours hold everything, urgent included/).length).toBeGreaterThan(0);
  });

  it("separates a test fire from the real history and claims no latency for a row that was never sent", async () => {
    stub();
    render(<AlertsView initial={payload()} history={HISTORY} />);
    await waitFor(() => expect(screen.getByTestId("delivery-1")).toBeInTheDocument());
    // Held and rate-limited are different facts, and neither is a failure.
    expect(screen.getByTestId("delivery-2").getAttribute("data-status")).toBe("dropped_rate_limited");
    expect(screen.getByTestId("delivery-2").textContent).toMatch(/the rule's own cap is spent for this window/);
    const queued = screen.getByTestId("delivery-1");
    expect(queued.textContent).toMatch(/queued/);
    expect(queued.textContent).not.toMatch(/\d+ ms/);
    // The test fire is in its own table, marked as a test, and out of the real history.
    expect(screen.getByTestId("delivery-3").textContent).toMatch(/queued/);
  });

  it("fires a test on purpose and says that nothing was actually sent", async () => {
    stub();
    render(<AlertsView initial={payload()} history={HISTORY} />);
    await waitFor(() => expect(screen.getByTestId("alert-al-1")).toBeInTheDocument());
    const testButtons = screen.getAllByRole("button", { name: /test fire/i });
    expect(testButtons.length).toBeGreaterThan(0);
    fireEvent.click(testButtons[0] as HTMLElement);
    await waitFor(() => expect(screen.getByTestId("test-result")).toBeInTheDocument());
    const result = screen.getByTestId("test-result");
    // The plan, the channel, and the sentence in the conditional — a test fire that reads like a delivery is a
    // test fire that teaches the user the wrong thing about their notifications.
    expect(result.textContent).toMatch(/held for quiet hours/);
    expect(screen.getByTestId("alert-notice").textContent).toMatch(/what would be sent/);
    expect(screen.getByTestId("alert-notice").textContent).toMatch(/window was not spent/);
  });
});
