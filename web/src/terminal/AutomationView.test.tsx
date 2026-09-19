import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AutomationView } from "./AutomationView";
import type { AutomationList, AutomationRunRow, BuilderVocabulary, TemplateCatalog } from "./wire";

/**
 * D8 on the screen, as render tests: the four things the phase says a user must be able to do, and the two
 * refusals that make them safe.
 *
 *  1. why a rule is not firing (badge + sentence + last run + next evaluation, and "never fired" is said);
 *  2. the builder is the engine's vocabulary, and it will not send a rule the API refuses;
 *  3. the dry run is the only path to arming — the arm button is disabled on a halted rule and the preview is
 *     what earns the arm, so the screen never offers a control the engine will reject;
 *  4. the withheld 5-minute crypto template is listed with the fee arithmetic that withheld it, never dropped.
 *
 * Every stubbed read carries the clock (`asOf`/`staleAfter`): an unstamped body is discarded by the hooks and the
 * screen renders empty, which would read as "element not found" rather than "the fixture was wrong".
 */
const asOf = 1_789_000_000_000;
const STAMP = { asOf, serverAsOf: asOf, staleAfter: asOf + 3_000, cache: { ttlMs: 3_000, public: false } };

const VOCAB: BuilderVocabulary = {
  triggers: [
    { kind: "price_cross", label: "a price crosses a level you name", fields: [
      { name: "uses", label: "price", type: "select", options: ["mid", "last"], default: "mid" },
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

const RULE = {
  ruleId: "rule-1",
  name: "exit before resolution",
  kind: "exit",
  status: "dry_run" as const,
  statusWhy: "no completed dry run yet, so it cannot go live",
  mode: "dry_run" as const,
  enabled: false,
  pausedReason: "",
  failureCount: 0,
  lastError: "",
  dryRunCompletedMs: null,
  lastFiredMs: null,
  lastEvaluationMs: null,
  nextEvaluationMs: asOf,
  runsToday: 0,
  maxPerDay: 24,
  minIntervalMs: 60_000,
  humanPriorityMs: 120_000,
  maxLossMicro: 5_000_000,
  targets: [{ marketId: "0xM1", tokenId: "" }],
  trigger: { any: [{ kind: "time", at_ms: 0, once: true }] },
  actions: [{ kind: "close_position" }],
  lastRun: null,
};

/** A DISTINCT second rule: what a never-fired, never-evaluated rule looks like beside one that has run. */
const QUIET_RULE = { ...RULE, ruleId: "rule-2", name: "cancel on a volume spike", status: "paused" as const,
                     statusWhy: "paused: waiting for the week's news", enabled: false, pausedReason: "waiting for the week's news" };

const RUNS: AutomationRunRow[] = [
  { ruleId: "rule-1", mode: "dry_run", outcome: "would_place", reason: "the trigger fired; nothing was sent because the rule is a dry run",
    denyCode: "", intentId: "", atMs: asOf - 1_000, leaves: "price 0.42 <= 0.42", sentence: "would place: the trigger fired" },
  { ruleId: "rule-1", mode: "dry_run", outcome: "skipped", reason: "the price level was not reached",
    denyCode: "", intentId: "", atMs: asOf - 60_000, leaves: "price 0.55 > 0.42", sentence: "skipped: the price level was not reached" },
];

const CATALOG: TemplateCatalog = {
  ...STAMP,
  templates: [
    { templateId: "entry-momentum-5m", name: "5-minute crypto entry (momentum)", kind: "entry",
      trigger: {}, actions: [], available: false,
      verdict: "ENTRY RULE NOT SHIPPED (protective half ships)", blockingReason: "no_measured_edge",
      why: "no measured edge: nothing on the leaderboard has a fee-adjusted sample big enough",
      feeArithmetic: { feeType: "taker", feeRateBps: 200, maker: false, legs: 2, priceMicro: 500_000,
                       breakEvenWinRateBp: 5_200, impliedProbBp: 5_000, feesPerShareMicro: 20_000,
                       spreadCostMicro: 100, spreadCostBp: 2, latencyMs: 250, latencyCostBp: 5,
                       edgeNeededBp: 205, edgeAvailableBp: null, builderBps: 100, assumptions: "taker on both legs" } },
    { templateId: "protect-exit-before-resolution", name: "exit before resolution", kind: "protect",
      trigger: {}, actions: [{ kind: "close_position" }], available: true, verdict: "SHIPPED",
      blockingReason: "", why: "ships because it can only reduce a loss the user was already risking",
      feeArithmetic: null },
  ],
  feeArithmetic: { feeType: "taker", feeRateBps: 200, maker: false, legs: 2, priceMicro: 500_000,
                   breakEvenWinRateBp: 5_200, impliedProbBp: 5_000, feesPerShareMicro: 20_000,
                   spreadCostMicro: 100, spreadCostBp: 2, latencyMs: 250, latencyCostBp: 5,
                   edgeNeededBp: 205, edgeAvailableBp: null, builderBps: 100, assumptions: "taker on both legs" },
  ships: false,
  verdict: "ENTRY RULE NOT SHIPPED (protective half ships)",
  why: "no measured edge: nothing on the leaderboard has a fee-adjusted sample big enough",
  blockingReason: "no_measured_edge",
};

function list(over: Partial<AutomationList> = {}): AutomationList {
  // The caps shape is the contract's (`AutomationCaps`), not a guess: `capsText` reads `activeRules`, and a
  // fixture with a made-up key renders "undefined of 10 rules armed" — a test that passes against a body the
  // API cannot send is a test of nothing.
  return { ...STAMP, rules: [RULE, QUIET_RULE], halt: null, vocabulary: VOCAB,
           caps: { concurrentRuleCap: 10, globalRunsPerDay: 5_000, activeRules: 2,
                   note: "the platform budget is separate from your own cap" },
           note: "", ...over } as AutomationList;
}

function stub(body: { list?: AutomationList; runs?: AutomationRunRow[]; post?: unknown } = {}) {
  vi.stubGlobal("fetch", vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const json = (payload: unknown) =>
      new Response(JSON.stringify({ ...STAMP, ...(payload as object) }), { status: 200,
                                                                          headers: { "content-type": "application/json" } });
    if (init?.method === "POST") return json(body.post ?? { rule: RULE, dryRunOnly: true });
    if (href.includes("/v1/automations/runs")) return json({ rows: body.runs ?? RUNS });
    return json(body.list ?? list());
  }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("D8 · the automation screen", () => {
  it("explains a rule that is not firing, down to the run that did nothing", async () => {
    stub();
    render(<AutomationView initial={list()} catalog={CATALOG} />);
    // By test id, not by name: the protective template shares the rule's name ("exit before resolution"), which
    // is a real ambiguity on this screen rather than a fixture accident.
    await waitFor(() => expect(screen.getByTestId("rule-rule-1")).toBeInTheDocument());
    expect(screen.getByTestId("rule-rule-1").textContent).toContain("exit before resolution");
    // The badge is not enough on its own: the sentence beside it says why, and a rule that has never fired says
    // "never fired" rather than showing a dash or a zero.
    expect(screen.getByText("no completed dry run yet, so it cannot go live")).toBeInTheDocument();
    expect(screen.getByText("paused: waiting for the week's news")).toBeInTheDocument();
    expect(screen.getAllByText(/never fired/).length).toBeGreaterThan(0);

    // `noUncheckedIndexedAccess` is on, so the pluck is checked rather than asserted away: the length is a real
    // assertion (the row has a history control) and the cast only tells the compiler what the assertion proved.
    const historyButtons = screen.getAllByRole("button", { name: /run history/i });
    expect(historyButtons.length).toBeGreaterThan(0);
    fireEvent.click(historyButtons[0] as HTMLElement);
    // The section is created by the click and filled by the read, so both waits are needed: waiting for the
    // section alone asserts "the button worked", not "the history arrived".
    await waitFor(() => expect(screen.getByTestId("run-history")).toBeInTheDocument());
    // Every evaluation, including the ones that did nothing, with the reason and the leaf values the trigger
    // was read with.
    await waitFor(() => expect(screen.getByText(/would place: the trigger fired/)).toBeInTheDocument());
    expect(screen.getByText(/skipped: the price level was not reached/)).toBeInTheDocument();
    expect(screen.getByText(/price 0\.55 > 0\.42/)).toBeInTheDocument();
  });

  it("shows the 5-minute crypto template as withheld, with the fee arithmetic that withheld it", async () => {
    stub();
    render(<AutomationView initial={list()} catalog={CATALOG} />);
    await waitFor(() => expect(screen.getByTestId("withheld-entry-momentum-5m")).toBeInTheDocument());
    // Listed, not hidden: the user can read the numbers instead of guessing whether the feature is missing.
    // Twice on purpose — the catalog's verdict and the template's own `why` both carry the reason.
    expect(screen.getAllByText(/no measured edge/).length).toBeGreaterThan(0);
    // The arithmetic itself: the break-even the entry has to clear, and — because nothing has measured an edge
    // yet — "not measured" rather than a 0, which is the one number that would read as "no edge needed".
    const withheld = screen.getByTestId("withheld-entry-momentum-5m");
    expect(withheld.textContent).toMatch(/break-even win rate/);
    expect(withheld.textContent).toMatch(/not measured/);
    // And the protective half is offered, with its reason.
    expect(screen.getByTestId("template-protect-exit-before-resolution")).toBeInTheDocument();
    expect(screen.getByText(/can only reduce a loss/)).toBeInTheDocument();
    expect(screen.queryByTestId("withheld-protect-exit-before-resolution")).toBeNull();
  });

  it("says halted, and offers no way to arm, when the risk service has stopped the account", async () => {
    const halted = list({
      // `enabled: true` with `status: "halted"` is the pair the API actually sends when the risk service has
      // stopped the account: halted outranks enabled, and the row must not read "armed and firing".
      rules: [{ ...RULE, status: "halted" as const, enabled: true, dryRunCompletedMs: asOf,
                statusWhy: "stopped by the daily-loss halt — trading resumes when you acknowledge it" }],
      halt: { halted: true, thresholdMicro: 50_000_000, realizedMicro: -60_000_000, trippedMs: asOf - 5_000,
              lossMicro: 0, acknowledgeHint: "acknowledge in the risk panel: acknowledging is a record, not a reset",
              note: "the daily loss limit tripped, so every money-moving path for this account is stopped" },
    });
    stub({ list: halted });
    render(<AutomationView initial={halted} catalog={CATALOG} />);
    await waitFor(() => expect(screen.getByTestId("halt-banner")).toBeInTheDocument());
    const banner = screen.getByTestId("halt-banner");
    expect(banner.textContent).toMatch(/daily loss limit tripped/);
    expect(banner.textContent).toMatch(/record, not a reset/);
    // The rule itself says halted, with the reason, and the control that would arm it is disabled rather than
    // hidden: the row still explains itself instead of going blank.
    expect(screen.getByTestId("rule-rule-1").getAttribute("data-status")).toBe("halted");
    expect(screen.getByTestId("rule-rule-1").textContent).toMatch(/stopped by the daily-loss halt/);
    expect((screen.getByRole("button", { name: /pause/i }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("refuses in the form what the API would refuse, before anything is sent", async () => {
    stub();
    render(<AutomationView initial={list()} catalog={CATALOG} />);
    await waitFor(() => expect(screen.getByTestId("rule-rule-1")).toBeInTheDocument());
    const posts = () => (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
      .filter((c) => (c[1] as RequestInit | undefined)?.method === "POST").length;
    const before = posts();
    // An empty builder: no market, no rows with required fields, and no loss ceiling. Saving must not reach the
    // API at all — a form that submits what it knows is invalid makes the server the author of the error message.
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(screen.getByTestId("builder-problems")).toBeInTheDocument());
    expect(screen.getByTestId("builder-problems").textContent ?? "").not.toBe("");
    expect(posts()).toBe(before);
  });
});
