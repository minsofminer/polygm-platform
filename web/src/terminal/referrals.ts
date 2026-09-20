/**
 * D5 · the referral screen's rules.
 *
 * The kit asks for one screen with the whole referral story on it, and three of its constraints change what the
 * screen IS rather than how it looks:
 *
 *  - **The funnel is the contract's, not the screen's.** `FUNNEL_MEANING` arrives from the API with the meaning of
 *    each step, so a reader cannot be told a different story than the money path implements. `clicks` is rendered
 *    as a LEADING count with the API's own sentence, because it is not a ceiling on signups (a code shouted on a
 *    podcast produces signups with no clicks) and a screen that drew five bars of one pipeline would be lying.
 *  - **The payout reality is on the page, not in a terms link**: the schedule, the minimum, the settlement hold,
 *    the tax form, and the `[UNVERIFIED]` marker that says which parts are a jurisdiction review rather than a
 *    decision. A referrer who is surprised by any of those has been surprised by us.
 *  - **The reward has a qualification and a name.** The model line states what earns (a share of the fee we were
 *    PAID on the referee's own orders, for a year) and why the alternatives were rejected, because "why not a flat
 *    bounty" is the first question asked and the answer is the model's whole argument.
 *
 * Money never becomes a float here: every amount stays in micro-USDC and is rendered through `Number`, which goes
 * through `src/money/cents.ts`.
 */

/** The served term sheet. Micro fields are integers; the sentences are authored server-side. */
export type ReferralTerms = {
  model: string;
  modelSentence: string;
  qualifyNotionalMicro: number;
  shareBps: number;
  termDays: number;
  settleHoldDays: number;
  payoutMinMicro: number;
  reviewThresholdMicro: number;
  clawbackMinMicro: number;
  paidFrom: string;
  schedule: string;
  rules: string[];
  rejectedModels: string[];
  taxNote: string;
  noReferrerLeaderboard: string;
  builderCode?: string;
};

export type ReferralLink = {
  token: string;
  url: string;
  code: string;
  shortUrl: string;
  created: boolean;
};

export type ReferralRow = {
  referee: string;
  state: string;
  stateText: string;
  reason: string;
  signedUpMs: number;
  qualifiedMs: number | null;
  notionalMicro: number;
  termEndsMs: number | null;
  daysLeft: number | null;
  earnedMicro: number;
  builderCode: string;
  decidedMs: number | null;
  note: string;
};

export type ReferralEarnings = {
  accruedMicro: number;
  settledMicro: number;
  holdingMicro: number;
  payableMicro: number;
  toMinimumMicro: number;
  minimumMicro: number;
  holdDays: number;
  reviewRequired: boolean;
  paidMicro: number;
  clawedBackMicro: number;
  note: string;
};

export type ReferralMe = {
  link: ReferralLink;
  funnel: Record<string, number>;
  earnings: ReferralEarnings;
  referrals: ReferralRow[];
  referralCount: number;
  review: { open: number; note: string };
  payout: {
    minimumMicro: number;
    schedule: string;
    nextAtMs: number;
    method: string;
    tax: { required: boolean; form: string; reportForm: string; reportThresholdMicro: number; reportable: boolean; withholding: string; note: string };
    note: string;
  };
  terms: ReferralTerms;
  hidden: { refused: number; note: string };
  funnelFindings: string[];
  funnelMeaning: Record<string, string>;
  note: string;
};

export type ReferralApply = {
  referee: string;
  referrer: string;
  state: string;
  reason: string;
  sentence: string;
  review: number;
  builderCode: string;
  note: string;
};

export type ReferralCode = {
  code: string;
  previous: string;
  shortUrl: string;
  link: ReferralLink;
  note: string;
};

export type ReferralReviewItem = {
  id: number;
  kind: string;
  subject: string;
  referee: string;
  state: string;
  findings: string[];
  openedMs: number;
  decidedMs: number | null;
  decision: string;
  actor: string;
  accruedMicro: number;
};

export type ReferralReviewList = { state: string; items: ReferralReviewItem[]; count: number; note: string };

export type ReferralReviewSet = {
  id: number;
  decision: string;
  state: string;
  kind: string;
  reason: string;
  note: string;
  clawback?: {
    unpaidMicro: number;
    paidMicro: number;
    writtenOffMicro: number;
    requiresRepayment: boolean;
    reason: string;
    sentence: string;
  };
};

/**
 * The money steps, in the order the money moves. `clicks` deliberately sits in its own leading list.
 *
 * The order is the point: a referrer reading this reads the pipeline that pays them, and a step out of order is
 * the diagram being wrong rather than the numbers being interesting.
 */
export const MONEY_STEPS = ["signups", "funded", "trading", "earned"] as const;
export const LEADING_STEPS = ["clicks"] as const;
export type MoneyStep = (typeof MONEY_STEPS)[number];

/** The bucket names the earnings block renders. A union, so a missing label is a type error, not a blank cell. */
export type EarningsKey = "accrued" | "holding" | "payable" | "paid" | "clawedBack";
export type PayoutKey = "schedule" | "method" | "taxForm" | "taxReport";

export const STEP_FALLBACK: Record<string, string> = {
  clicks: "the link was opened, which is a count of interest and nothing else",
  signups: "an account applied your code",
  funded: "the referee placed their first matched order over the qualifying notional",
  trading: "we were paid a builder fee on that referee's orders",
  earned: "your share of those fees is still owed to you",
};

export function stepMeaning(me: ReferralMe, step: string): string {
  return me.funnelMeaning?.[step] ?? STEP_FALLBACK[step] ?? "";
}

/** The four money steps with their counts, and a flag when the API said the funnel cannot be true. */
export function funnelSteps(
  me: ReferralMe,
): { key: MoneyStep | (typeof LEADING_STEPS)[number]; count: number; meaning: string; leading: boolean }[] {
  return [
    ...MONEY_STEPS.map((key) => ({ key, count: me.funnel[key] ?? 0, meaning: stepMeaning(me, key), leading: false })),
    ...LEADING_STEPS.map((key) => ({ key, count: me.funnel[key] ?? 0, meaning: stepMeaning(me, key), leading: true })),
  ];
}

/** True when the four money steps are monotone — the API's own finding array is the authority, not this. */
export function moneyStepsAreMonotone(me: ReferralMe): boolean {
  const counts = MONEY_STEPS.map((k) => me.funnel[k] ?? 0);
  return counts.every((v, i) => i === 0 || (counts[i - 1] ?? 0) >= v);
}

/**
 * The earnings line: every bucket, including the two a screen would rather hide.
 *
 * `clawedBack` and `paid` are rendered even when zero, because a referrer should be able to see the shape of the
 * account they are building. `label` is the copy key; the component renders the amount through `Number`.
 */
export function earningsLines(me: ReferralMe): { key: EarningsKey; micro: number }[] {
  const e = me.earnings;
  return [
    { key: "accrued", micro: e.accruedMicro },
    { key: "holding", micro: e.holdingMicro },
    { key: "payable", micro: e.payableMicro },
    { key: "paid", micro: e.paidMicro },
    { key: "clawedBack", micro: e.clawedBackMicro },
  ];
}

/**
 * The one sentence between this account and its next payment, in the API's arithmetic.
 *
 * `toMinimumMicro` measures the gap from the PAYABLE balance (not from everything ever accrued), so a referrer
 * inside the settlement hold is told the truth: the money exists, it is not payable yet, and this is what is still
 * missing after it becomes payable.
 */
export function payoutGap(me: ReferralMe): { micro: number; holding: boolean; sentence: string } {
  const e = me.earnings;
  if (e.payableMicro > 0 && e.toMinimumMicro === 0) {
    return { micro: 0, holding: e.holdingMicro > 0, sentence: "eligible for the next payout run" };
  }
  return {
    micro: e.toMinimumMicro,
    holding: e.holdingMicro > 0,
    sentence: e.holdingMicro > 0
      ? "below the payout minimum once the settlement hold clears"
      : "below the payout minimum, and it carries forward rather than expiring",
  };
}

/** The payout reality as rows, so the component cannot render one of them without the others. */
export function payoutLines(me: ReferralMe): { key: PayoutKey; value: string }[] {
  const p = me.payout;
  return [
    { key: "schedule", value: p.schedule },
    { key: "method", value: p.method.toUpperCase() },
    { key: "taxForm", value: `${p.tax.form} — ${p.tax.note}` },
    { key: "taxReport", value: `${p.tax.reportForm}; ${me.terms.taxNote}` },
  ];
}

/** The qualification, as a sentence a referrer can check against their own referee's orders. */
export function qualificationLine(terms: ReferralTerms): string {
  return `a share of ${terms.shareBps / 100}% of the builder fee we were actually paid on the referee's own orders, for ${terms.termDays} days from their first matched order over the qualifying notional`;
}

/** What is missing for the account as a whole: review items pause one referral, never the programme. */
export function reviewLine(me: ReferralMe): string | null {
  if (!me.review?.open) return null;
  return `${me.review.open} referral${me.review.open === 1 ? "" : "s"} in review — accrual on ${
    me.review.open === 1 ? "it" : "them"
  } is paused until a person clears it, and what was earned while it waited is paid`;
}

/**
 * The referee's row as the referrer sees it: a state, the number behind it, and the note the API wrote.
 *
 * The state text comes from the API (`stateText`), which reads it off the same vocabulary the accrual engine
 * applies — a screen that invented its own words for `pending`/`review`/`qualified`/`clawed_back` would be the
 * second place those states are defined.
 */
export function referrerRows(me: ReferralMe): {
  key: string;
  referee: string;
  state: string;
  stateText: string;
  daysLeft: number | null;
  earnedMicro: number;
  note: string;
}[] {
  return (me.referrals ?? []).map((r) => ({
    key: r.referee,
    referee: r.referee,
    state: r.state,
    stateText: r.stateText,
    daysLeft: r.daysLeft,
    // A clawed-back referee keeps their row and their history: the money is shown as it stands (zero once it has
    // been taken back), not as it briefly was.
    earnedMicro: r.state === "clawed_back" ? 0 : r.earnedMicro,
    note: r.note,
  }));
}

/** The referee-side answer to "somebody gave me a code": where the referral stands, in one sentence. */
export function applyOutcome(got: ReferralApply): string {
  if (got.state === "review") {
    return `${got.sentence} Nothing has been paid out on it and nothing has been cancelled: an operator clears it, and what was earned while it waited is paid.`;
  }
  return `${got.sentence} It earns nothing until your first matched order over the qualifying notional — a signup and a deposit are not the reward.`;
}

/** Signed-out copy for the panel, kept next to the data it replaces. */
export const SIGNED_OUT =
  "Signed out, so there is no referral dashboard to show: your link, your funnel and your earnings are served per account and there is no anonymous version of them. The terms below are public and are the same page a signed-in referrer reads.";
