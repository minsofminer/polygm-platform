/**
 * D7's screen logic, without a browser.
 *
 * The three things these tests defend are the three the screen would otherwise decide by accident: that a finding
 * without BOTH readings is not given to a reviewer, that a decision without a usable reason never leaves the
 * client, and that the body a click sends matches what the route declares (including the `finding` kind, which is
 * how the dashboard's precision becomes a number later).
 */
import { describe, expect, it } from "vitest";
import { actionsFor, decideTarget, decision, pairing, severityLabel, subjects, type Finding } from "./gaming";

const rules = {
  fast_climb: { rule: "at least 25 places inside seven days …", innocent: "a lucky streak is a thing" },
};

const climb: Finding = {
  kind: "fast_climb",
  severity: 3,
  suggested: "flag",
  rule: rules.fast_climb.rule,
  evidence: ["climb is 870 place(s), 3x or more the board's median of 2"],
  wallet: "0xLBFARM0000000000000000000000000000000001",
  anon: "w_5c037b2ab8",
  board: "risk_adjusted",
  climb: 870,
  fromRank: 900,
  toRank: 30,
  settled: 9,
  medianClimb: 2,
  percentileBps: 9756,
};

describe("pairing", () => {
  it("hands over both readings or nothing", () => {
    expect(pairing(climb, rules)?.innocent).toMatch(/lucky streak/);
    expect(pairing(climb, { fast_climb: { rule: "why", innocent: "" } })).toBeNull();
    expect(pairing({ ...climb, kind: "unknown" }, rules)).toBeNull();
  });
});

describe("subjects and targets", () => {
  it("quotes pseudonyms and keys the decision by the wallet", () => {
    expect(subjects(climb)).toEqual(["w_5c037b2ab8"]);
    expect(decideTarget(climb)).toBe(climb.wallet);
  });

  it("names a cluster's members and refuses to invent a target when there is none", () => {
    const cluster: Finding = { ...climb, kind: "correlated_cluster", wallet: undefined, anon: undefined,
      wallets: ["0xa", "0xb"], anonWallets: ["w_a", "w_b"] };
    expect(subjects(cluster)).toEqual(["w_a", "w_b"]);
    expect(decideTarget(cluster)).toBe("0xa");
    expect(decideTarget({ ...cluster, wallets: [], referrer: "" })).toBe("");
  });
});

describe("decision", () => {
  it("mints a key per click and sends the finding kind with the reason", () => {
    const d = decision(climb, "exclude", "  confirmed wash pair with w_other  ");
    expect(d).not.toBeNull();
    expect(d!.key).toBe("adminGamingDecide");
    expect(d!.body).toEqual({
      wallet: climb.wallet,
      action: "exclude",
      reason: "confirmed wash pair with w_other",
      board: "all",
      finding: "fast_climb",
    });
    expect(d!.idempotencyKey).toMatch(/^[A-Za-z0-9_-]{8,128}$/);
    expect(decision(climb, "flag", "second click, same row")!.idempotencyKey).not.toBe(d!.idempotencyKey);
  });

  it("refuses a reason that cannot answer an appeal", () => {
    expect(decision(climb, "exclude", "suspect")).toBeNull();
    expect(decision({ ...climb, wallet: undefined, anon: undefined }, "flag", "long enough reason")).toBeNull();
  });
});

describe("actions", () => {
  it("offers the reversal of whatever was already recorded", () => {
    expect(actionsFor(climb)).toEqual(["flag", "exclude"]);
    expect(actionsFor({ ...climb, decided: { action: "exclude", atMs: 1 } })).toEqual(["include", "flag"]);
    expect(actionsFor({ ...climb, decided: { action: "flag", atMs: 1 } })).toEqual(["exclude", "clear"]);
    expect(actionsFor({ ...climb, suggested: "exclude" })).toEqual(["exclude", "flag"]);
  });

  it("names the severity in words, because a number is not a priority", () => {
    expect([severityLabel(3), severityLabel(2), severityLabel(1)]).toEqual(["high", "medium", "low"]);
  });
});
