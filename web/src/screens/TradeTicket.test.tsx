/**
 * The web ticket, rendered — and the test that would have caught the bug this file exists for.
 *
 * From P08 to P12 the ticket posted `{market_id, side, amount_cents}` to `/v1/orders`, which requires
 * `{marketId, tokenId, side, price, size}`. The API answered 422 for every trade the site ever attempted, and the
 * reason nothing caught it is that the ticket had no test at all: it was rendered in two screens, both of which
 * tested their own thing. So this file asserts the *wire*: the route key, the shape of the body, and the fact that
 * nothing is sent at all when there is no market to price against.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TradeTicket } from "./TradeTicket";

const sent: { key: string; body: unknown; idempotencyKey?: string }[] = [];

/** The one request the ticket is allowed to make, read with a helper so the test does not index an empty array. */
function firstSent() {
  const [head] = sent;
  if (!head) throw new Error("the ticket sent nothing");
  return head;
}
let answer: { ok: true; data: Record<string, unknown> } | { ok: false; error: { code: string; message: string; requestId: string } } =
  { ok: true, data: { intentId: "int_1", state: "queued" } };

vi.mock("@/api/client", () => ({
  newIdempotencyKey: (seed: string) => `key:${seed}`,
  request: async (input: { key: string; body: unknown; idempotencyKey?: string }) => {
    sent.push(input);
    return answer;
  },
}));

vi.mock("@/ui/Toast", () => ({ pushRefusal: () => undefined }));

afterEach(() => {
  sent.length = 0;
  answer = { ok: true, data: { intentId: "int_1", state: "queued" } };
});

/** The connection store decides whether a trade may be sent at all; connect it so the ticket is live. */
vi.mock("@/shell/connection", () => ({
  useConnection: (pick: (s: { canTrade: () => boolean; whyNot: () => string | null }) => unknown) =>
    pick({ canTrade: () => true, whyNot: () => null }),
}));

function type(amount: string) {
  fireEvent.change(screen.getByLabelText(/size/i), { target: { value: amount } });
}

/** BUY/SELL choose the side; "Confirm trade" is what sends. Clicking the side is not a submission. */
function submit() {
  fireEvent.click(screen.getByRole("button", { name: /confirm trade/i }));
}

describe("the web ticket sends what the API's amount-priced route accepts", () => {
  it("posts a slug, a side and a decimal amount to the amount route", async () => {
    render(<TradeTicket slug="fed-cut-sept" />);
    type("10");
    submit();
    await waitFor(() => expect(sent.length).toBe(1));
    expect(firstSent().key).toBe("orderByAmount");
    // The amount is a decimal *string* built from integer cents: `"10"` is `"10.00"`, and no float is involved at any
    // point on the way out. `amount_cents` is gone, and its absence is the assertion.
    expect(firstSent().body).toEqual({ slug: "fed-cut-sept", side: "yes", amountUsdc: "10.00" });
    expect(firstSent().idempotencyKey).toBe("key:order:fed-cut-sept:BUY:10");
  });

  it("sends the no-side when the ticket is on SELL", async () => {
    render(<TradeTicket slug="fed-cut-sept" />);
    type("5.50");
    fireEvent.click(screen.getByRole("button", { name: /^sell$/i }));
    submit();
    await waitFor(() => expect(sent.length).toBe(1));
    expect(firstSent().body).toEqual({ slug: "fed-cut-sept", side: "no", amountUsdc: "5.50" });
  });

  it("refuses in words when there is no market, and posts nothing", async () => {
    render(<TradeTicket />);
    type("10");
    submit();
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/pick a market/i));
    expect(sent).toHaveLength(0);
  });

  it("does not send an unparseable amount", async () => {
    render(<TradeTicket slug="fed-cut-sept" />);
    type("ten dollars");
    submit();
    await waitFor(() => expect(screen.getByRole("alert").textContent).toBeTruthy());
    expect(sent).toHaveLength(0);
  });

  it("shows the API's sentence on a refusal, not its code", async () => {
    answer = { ok: false, error: { code: "NO_ORDER_BOOK", message: "there is no offer on that side right now", requestId: "r1" } };
    render(<TradeTicket slug="fed-cut-sept" />);
    type("10");
    submit();
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("no offer"));
    expect(screen.getByRole("alert").textContent).not.toContain("NO_ORDER_BOOK");
  });
});
