/**
 * D5 · the referral screen's rules.
 *
 * What is being pinned here is the part of the screen that is an argument rather than a layout: that the funnel is
 * read as the money chain plus a leading count, that a bucket is never hidden, and that the sentence a referee
 * gets back distinguishes "under review" from "refused".
 */
import { describe, expect, it } from "vitest";
import {
  MONEY_STEPS,
  applyOutcome,
  earningsLines,
  funnelSteps,
  moneyStepsAreMonotone,
  payoutGap,
  payoutLines,
  qualificationLine,
  referrerRows,
  reviewLine,
  type ReferralMe,
  type ReferralTerms,
} from "./referrals";

const terms: ReferralTerms = {
  model: "builder-fee share",
  modelSentence: "a share of the builder fee we are paid on the referee's own orders, for one year",
  qualifyNotionalMicro: 25_000_000,
  shareBps: 2500,
  termDays: 365,
  settleHoldDays: 30,
  payoutMinMicro: 20_000_000,
  reviewThresholdMicro: 2_000_000_000,
  clawbackMinMicro: 5_000_000,
  paidFrom: "the fee the venue actually paid us (observed), never the fee we expected",
  schedule: "monthly, by the 10th, for the month before",
  rules: ["a second level does not exist", "a referral funded from the same source is refused"],
  rejectedModels: ["flat bounty on a first funded trade", "deposit-size reward", "Pro credit"],
  taxNote: "[UNVERIFIED] the forms and thresholds are a jurisdiction review",
  noReferrerLeaderboard: "no public referrer leaderboard: it would be a spam contest with a scoreboard",
};

const me = (over: Partial<ReferralMe> = {}): ReferralMe => ({
  link: { token: "ref_abcdefghijklmnopqrstuv", url: "https://polygm.app/r/ref_abcdefghijklmnopqrstuv", code: "", shortUrl: "", created: true }, // lint-allow: a fixture referral code, not a credential
  funnel: { clicks: 40, signups: 9, funded: 4, trading: 3, earned: 2 },
  earnings: {
    accruedMicro: 31_000_000,
    settledMicro: 11_000_000,
    holdingMicro: 20_000_000,
    payableMicro: 11_000_000,
    toMinimumMicro: 9_000_000,
    minimumMicro: 20_000_000,
    holdDays: 30,
    reviewRequired: false,
    paidMicro: 6_000_000,
    clawedBackMicro: 2_000_000,
    note: "$11.00 is payable now; $20.00 is inside the 30-day settlement hold",
  },
  referrals: [
    {
      referee: "w_aa11bb22cc",
      state: "pending",
      stateText: "signed up, has not placed a matched order yet",
      reason: "",
      signedUpMs: 1,
      qualifiedMs: null,
      notionalMicro: 0,
      termEndsMs: null,
      daysLeft: null,
      earnedMicro: 0,
      builderCode: "polygm-referral",
      decidedMs: null,
      note: "",
    },
    {
      referee: "w_dd33ee44ff",
      state: "clawed_back",
      stateText: "clawed back: the two wallets shared a funding source",
      reason: "duplicate_funding",
      signedUpMs: 2,
      qualifiedMs: 3,
      notionalMicro: 40_000_000,
      termEndsMs: 4,
      daysLeft: 300,
      earnedMicro: 10_000_000,
      builderCode: "polygm-referral",
      decidedMs: 5,
      note: "",
    },
  ],
  referralCount: 2,
  review: { open: 1, note: "one referral is in review" },
  payout: {
    minimumMicro: 20_000_000,
    schedule: "monthly, by the 10th, for the month before",
    nextAtMs: 1_800_000_000_000,
    method: "usdc",
    tax: { required: true, form: "W-9", reportForm: "1099-NEC", reportThresholdMicro: 60_000_000_000, reportable: false, withholding: "", note: "a W-9 before the first payout" },
    note: "payments are monthly by the 10th",
  },
  terms,
  hidden: { refused: 2, note: "refused attempts are not listed here" },
  funnelFindings: [],
  funnelMeaning: { clicks: "the link was opened, which is a count of interest and nothing else", earned: "your share of those fees is still owed to you" },
  note: "the seatbelt on the numbers above",
  ...over,
});

describe("the funnel", () => {
  it("reads the four money steps as a chain and the leading count apart from it", () => {
    const steps = funnelSteps(me());
    expect(steps.filter((s) => !s.leading).map((s) => s.key)).toEqual([...MONEY_STEPS]);
    expect(steps.filter((s) => s.leading).map((s) => s.key)).toEqual(["clicks"]);
    // The API's meaning is rendered as it arrived, and a step with no sentence of its own falls back rather than
    // rendering an empty cell.
    expect(steps.find((s) => s.key === "earned")?.meaning).toBe("your share of those fees is still owed to you");
    expect(steps.find((s) => s.key === "signups")?.meaning).toBe("an account applied your code");
  });

  it("calls a funnel that cannot be true, instead of drawing it", () => {
    expect(moneyStepsAreMonotone(me())).toBe(true);
    expect(moneyStepsAreMonotone(me({ funnel: { clicks: 1, signups: 2, funded: 8, trading: 3, earned: 1 } }))).toBe(false);
  });
});

describe("the money", () => {
  it("shows every bucket, including the two that are not a win", () => {
    const keys = earningsLines(me()).map((r) => r.key);
    expect(keys).toEqual(["accrued", "holding", "payable", "paid", "clawedBack"]);
    expect(earningsLines(me()).find((r) => r.key === "clawedBack")?.micro).toBe(2_000_000);
  });

  it("measures the gap to the payout from what is PAYABLE, not from everything accrued", () => {
    const gap = payoutGap(me());
    expect(gap.micro).toBe(9_000_000);
    expect(gap.holding).toBe(true);
    expect(gap.sentence).toContain("settlement hold");
    const eligible = me({
      earnings: { ...me().earnings, accruedMicro: 40_000_000, settledMicro: 40_000_000, holdingMicro: 0, payableMicro: 40_000_000, toMinimumMicro: 0 },
    });
    expect(payoutGap(eligible)).toEqual({ micro: 0, holding: false, sentence: "eligible for the next payout run" });
  });

  it("states the payout reality as rows, so none of it can be dropped by a screen", () => {
    const rows = payoutLines(me());
    expect(rows.map((r) => r.key)).toEqual(["schedule", "method", "taxForm", "taxReport"]);
    expect(rows.find((r) => r.key === "taxReport")?.value).toContain("1099-NEC");
    expect(rows.find((r) => r.key === "taxReport")?.value).toContain("[UNVERIFIED]");
  });

  it("states the qualification in the referee's currency and term, not as a rate on a signup", () => {
    const line = qualificationLine(terms);
    expect(line).toContain("25%");
    expect(line).toContain("365 days");
    expect(line).toContain("actually paid");
  });
});

describe("the referral rows and the referee's answer", () => {
  it("keeps a clawed-back referee on the list with their history and no money", () => {
    const rows = referrerRows(me());
    expect(rows.map((r) => r.referee)).toEqual(["w_aa11bb22cc", "w_dd33ee44ff"]);
    const ghost = rows[1];
    expect(ghost?.state).toBe("clawed_back");
    expect(ghost?.earnedMicro).toBe(0);
    expect(ghost?.stateText).toContain("clawed back");
  });

  it("labels a paused referral as paused rather than cancelled", () => {
    const line = reviewLine(me());
    expect(line).toContain("review");
    expect(line).toContain("paused");
    expect(line).toContain("is paid");
    expect(reviewLine(me({ review: { open: 0, note: "" } }))).toBeNull();
  });

  it("separates under review from refused in the referee's own sentence", () => {
    const held = applyOutcome({
      referee: "w_1",
      referrer: "w_2",
      state: "review",
      reason: "shared_device_or_ip",
      sentence: "this referral is held for a person to look at.",
      review: 7,
      builderCode: "polygm-referral",
      note: "",
    });
    expect(held).toContain("Nothing has been paid out");
    const clean = applyOutcome({
      referee: "w_1",
      referrer: "w_2",
      state: "pending",
      reason: "",
      sentence: "the referral is recorded.",
      review: 0,
      builderCode: "polygm-referral",
      note: "",
    });
    expect(clean).toContain("signup and a deposit are not the reward");
  });
});
