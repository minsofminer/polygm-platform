/**
 * D5 · the referral dashboard, rendered.
 *
 * Three things a pure-function test cannot prove: the screen is fed by the API's own answers, a refused short code
 * is rendered as the rule it broke (not as a generic failure), and claiming a code writes through the same client
 * every other mutation uses — key and all — then re-reads rather than assuming.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ReferralsView } from "./ReferralsView";

const asOf = Date.now() - 1_000;
const stamp = { asOf, staleAfter: asOf + 5_000, cache: { ttlMs: 1_000, public: false } };

const terms = {
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
  rules: ["a short code is 4 to 16 characters", "a referral funded from the same source is refused"],
  rejectedModels: ["flat bounty on a first funded trade", "deposit-size reward", "Pro credit"],
  taxNote: "[UNVERIFIED] the forms and thresholds are a jurisdiction review",
  noReferrerLeaderboard: "no public referrer leaderboard: it would be a spam contest with a scoreboard",
};

const me = {
  ...stamp,
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
  ],
  referralCount: 1,
  review: { open: 0, note: "" },
  payout: {
    minimumMicro: 20_000_000,
    schedule: terms.schedule,
    nextAtMs: 1_800_000_000_000,
    method: "usdc",
    tax: { required: true, form: "W-9", reportForm: "1099-NEC", reportThresholdMicro: 60_000_000_000, reportable: false, withholding: "", note: "a W-9 before the first payout" },
    note: "payments are monthly by the 10th",
  },
  terms,
  hidden: { refused: 2, note: "refused attempts are not listed here: the refusal is answered to the account that made it" },
  funnelFindings: [] as string[],
  funnelMeaning: { clicks: "the link was opened, which is a count of interest and nothing else", earned: "your share of those fees is still owed to you" },
  note: "the seatbelt on the numbers above",
};

type Call = { url: string; method: string; body: unknown; key: string | null };

function stub(opts: { unauthenticated?: boolean; refuse?: { code: string; message: string; status: number } } = {}) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string | URL, init?: RequestInit) => {
      const href = String(url);
      const method = (init?.method ?? "GET").toUpperCase();
      const json = (payload: unknown, status = 200) =>
        new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });
      calls.push({
        url: href,
        method,
        body: init?.body ? JSON.parse(String(init.body)) : null,
        key: new Headers(init?.headers).get("idempotency-key"),
      });
      if (opts.unauthenticated && href.includes("/v1/referrals/me")) {
        return json({ error: { code: "UNAUTHENTICATED", message: "a session is required", retryable: false, requestId: "r" } }, 401);
      }
      if (method === "POST") {
        if (opts.refuse) {
          return json(
            { error: { code: opts.refuse.code, message: opts.refuse.message, retryable: false, requestId: "r" } },
            opts.refuse.status,
          );
        }
        return json({ ...stamp, code: "polymarketmike", previous: "", shortUrl: "https://polygm.app/c/polymarketmike", link: me.link, note: "the code is live" });
      }
      if (href.includes("/v1/referrals/terms")) return json({ ...stamp, terms, note: "" });
      return json(me);
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the referral dashboard", () => {
  it("draws the money chain, the leading count, every bucket and the payout reality", async () => {
    stub();
    render(<ReferralsView />);
    expect(await screen.findByText("Funded")).toBeInTheDocument();
    // the leading count is labelled with the API's own sentence, not ours
    expect(screen.getByText("the link was opened, which is a count of interest and nothing else")).toBeInTheDocument();
    // every bucket, including the two that are not a win
    expect(screen.getByText("Clawed back")).toBeInTheDocument();
    expect(screen.getByText("Paid to date")).toBeInTheDocument();
    // the payout reality
    expect(screen.getByText(terms.schedule)).toBeInTheDocument();
    expect(screen.getByText(/1099-NEC/)).toBeInTheDocument();
    expect(screen.getByText(/UNVERIFIED/)).toBeInTheDocument();
    // the refused attempts are counted and explained rather than shown as referrals
    expect(screen.getByText(me.hidden.note)).toBeInTheDocument();
    expect(screen.getByText("signed up, has not placed a matched order yet")).toBeInTheDocument();
  });

  it("renders a refused short code as the rule that broke, and writes nothing else", async () => {
    const calls = stub({
      refuse: {
        code: "CODE_INVALID",
        message: "that short code cannot be used: that code is reserved (it reads as us, or as somebody else's name)",
        status: 422,
      },
    });
    render(<ReferralsView />);
    fireEvent.change(await screen.findByLabelText("Short code to claim"), { target: { value: "polygm" } });
    fireEvent.click(screen.getByRole("button", { name: "Claim this code" }));
    expect(await screen.findByText(/that code is reserved/)).toBeInTheDocument();
    const writes = calls.filter((c) => c.method === "POST");
    expect(writes).toHaveLength(1);
    // The key the server's own regex accepts, sent once per intent: `client.ts` owns the shape, and this asserts
    // the screen went through it rather than calling fetch itself.
    expect(writes[0]?.key).toMatch(/^[A-Za-z0-9_-]{8,128}$/);
  });

  it("claims a code through the client and re-reads rather than assuming the write landed", async () => {
    const calls = stub();
    render(<ReferralsView />);
    fireEvent.change(await screen.findByLabelText("Short code to claim"), { target: { value: "polymarketmike" } });
    fireEvent.click(screen.getByRole("button", { name: "Claim this code" }));
    expect(await screen.findByText("the code is live")).toBeInTheDocument();
    await waitFor(() => expect(calls.filter((c) => c.method === "GET" && c.url.includes("/v1/referrals/me")).length).toBeGreaterThan(1));
  });

  it("gives a signed-out visitor the public terms instead of an empty panel", async () => {
    stub({ unauthenticated: true });
    render(<ReferralsView />);
    expect(await screen.findByText(/Signed out, so there is no referral dashboard/)).toBeInTheDocument();
    expect(screen.getByText(terms.noReferrerLeaderboard)).toBeInTheDocument();
    expect(screen.getByText(/flat bounty on a first funded trade/)).toBeInTheDocument();
  });
});
