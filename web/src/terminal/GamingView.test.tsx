/**
 * D7 · the dashboard, rendered against a stubbed tape.
 *
 * Four things a pure test cannot prove, and each is a rule from the phase:
 *  1. the row shows BOTH readings — the rule and the innocent explanation — from the API's own strings;
 *  2. a click posts the decision through the same client every other mutation uses (admin header, idempotency
 *     key, the finding kind in the body) and then RE-READS rather than assuming it worked;
 *  3. a click without a usable reason does not leave the client at all;
 *  4. a refusal from the API is rendered as the API's sentence, not as a generic failure.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { GamingView } from "./GamingView";

type Call = { url: string; method: string; body: unknown; key: string | null; admin: string | null };

const climb = {
  kind: "fast_climb",
  severity: 3,
  suggested: "flag",
  rule: "gains at least 25 places inside seven days and the gain is three times the median",
  evidence: ["climb is 870 place(s), 3x or more the board's median of 2", "9 settled market(s)"],
  wallet: "0xLBFARM0000000000000000000000000000000001",
  anon: "w_5c037b2ab8",
  board: "risk_adjusted",
  climb: 870,
  fromRank: 900,
  toRank: 30,
};

const dashboard = {
  atMs: Date.now(),
  limit: 25,
  rules: {
    fast_climb: { rule: climb.rule, innocent: "a lucky streak is a thing that happens every week" },
    correlated_cluster: { rule: "co-timed", innocent: "a copy trader" },
    synthetic_chain: { rule: "floor hits", innocent: "friends signing up together" },
    builder_anomaly: { rule: "bursts", innocent: "a market maker" },
  },
  climbers: [climb],
  clusters: [],
  chains: [],
  builder: [],
  counts: { climbers: 1, clusters: 0, chains: 0, builder: 0, reviewed: 0 },
  evidence: { historyRows: 26, decisions: 0, newestSnapshotMs: Date.now() - 60_000, fills: 2990 },
};

function stub(opts: { refuse?: boolean } = {}) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string | URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      calls.push({
        url: String(url),
        method: (init?.method ?? "GET").toUpperCase(),
        body: init?.body ? JSON.parse(String(init.body)) : null,
        key: headers.get("idempotency-key"),
        admin: headers.get("x-admin-token"),
      });
      const json = (payload: unknown, status = 200) =>
        new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });
      if (opts.refuse) {
        return json({ error: { code: "ADMIN_REQUIRED", message: "the token was refused", retryable: false, requestId: "r" } }, 403);
      }
      if ((init?.method ?? "GET").toUpperCase() === "POST") {
        return json({ wallet: climb.wallet, anon: climb.anon, action: "flag", board: "all", finding: "fast_climb", atMs: 1 });
      }
      // The real route's envelope: `asOf`/`staleAfter`/`cache` are what the client insists on for a read, and
      // the first version of this stub omitted them — which is exactly how the missing stamp on the real route
      // was found, because the screen said "the tape was not read" on a 200.
      return json({
        ...dashboard,
        asOf: dashboard.evidence.newestSnapshotMs,
        staleAfter: dashboard.evidence.newestSnapshotMs + 120_000,
        serverAsOf: dashboard.atMs,
        cache: { ttlMs: 0, key: null, public: false, immutable: false },
      });
    }),
  );
  return calls;
}

async function read(admin = "adm_token_token") {
  render(<GamingView />);
  fireEvent.change(screen.getByLabelText("operator token"), { target: { value: admin } });
  fireEvent.click(screen.getByRole("button", { name: /read the tape|re-read/ }));
  await screen.findByText(/climb is 870 place/);
}

afterEach(() => vi.unstubAllGlobals());

describe("GamingView", () => {
  it("shows the rule and the innocent reading of the same shape, side by side", async () => {
    stub();
    await read();
    expect(screen.getByText(/gains at least 25 places inside seven days/)).toBeTruthy();
    expect(screen.getByText(/a lucky streak is a thing that happens every week/)).toBeTruthy();
    expect(screen.getByText("w_5c037b2ab8")).toBeTruthy();
    expect(screen.getByText("severity 3 of 3")).toBeTruthy();
    // The word is in the attribute rather than the sentence on purpose: the row's colour and its word must
    // agree, and a screen reader should not have to hear "high" twice.
    expect(screen.getByText("severity 3 of 3").getAttribute("data-sev")).toBe("high");
  });

  it("posts the decision with the operator token, a fresh key and the finding kind, then re-reads", async () => {
    const calls = stub();
    await read();
    fireEvent.change(screen.getByLabelText(/why \(8 characters or more/), { target: { value: "confirmed: same funder" } });
    fireEvent.click(screen.getByRole("button", { name: /flag for review/ }));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST").length).toBe(1));
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.url).toContain("/v1/admin/gaming/decide");
    expect(post.admin).toBe("adm_token_token");
    expect(post.key).toMatch(/^[A-Za-z0-9_-]{8,128}$/);
    expect(post.body).toMatchObject({ wallet: climb.wallet, action: "flag", finding: "fast_climb", board: "all" });
    // the re-read proves the screen shows what the server holds rather than what the click assumed
    await waitFor(() => expect(calls.filter((c) => c.method === "GET").length).toBeGreaterThan(1));
  });

  it("does not send a decision whose reason cannot answer an appeal", async () => {
    const calls = stub();
    await read();
    fireEvent.change(screen.getByLabelText(/why \(8 characters or more/), { target: { value: "hmm" } });
    const button = screen.getByRole("button", { name: /flag for review/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(calls.filter((c) => c.method === "POST").length).toBe(0);
  });

  it("renders a refused token as the API's own sentence", async () => {
    stub({ refuse: true });
    render(<GamingView />);
    fireEvent.change(screen.getByLabelText("operator token"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: /read the tape/ }));
    await screen.findByText(/the tape was not read: the token was refused/);
    expect(screen.queryByText(/climb is 870 place/)).toBeNull();
  });
});
