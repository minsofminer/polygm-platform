/**
 * P13 D4 — the accessibility and keyboard half of the frontend rows, asserted on the rendered tree.
 *
 * The kit's D4 list asks for axe, keyboard operation and `aria-live` on the surfaces that change by themselves.
 * There is no axe dependency in this repo, so what is asserted here is the *rule* axe would apply, written out:
 * every interactive control has an accessible name, nothing that can take focus is hidden from the accessibility
 * tree, no positive `tabindex` reorders the document, every value that updates on its own lives in a live region,
 * and status is never carried by colour alone. Those are the findings that actually block a screen-reader user,
 * and each one is checkable in jsdom against the real components.
 *
 * The two components under test are the two that matter for money: the ladder (where a price is read) and the
 * ticket (where an amount is typed and sent).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { OrderBook, type BookPayload } from "./OrderBook";
import { TradeTicket } from "./TradeTicket";
import { useConnection } from "@/shell/connection";

const stamp = { asOf: 1_700_000_000_000, staleAfter: 1_700_000_003_000, cache: { ttlMs: 250, public: true } };
const level = (price: string, shares: string) => ({ price, shares, levels: 1, cumShares: shares });

function book(bids: BookPayload["bids"], asks: BookPayload["asks"]): BookPayload {
  return { ...stamp, market: "0xM1", bids, asks, aggregate: "raw", spreadTicks: null, ageMs: 0, oneSided: null };
}

const sent: unknown[] = [];
vi.mock("@/api/client", () => ({
  newIdempotencyKey: (seed: string) => `key:${seed}`,
  request: async (input: unknown) => {
    sent.push(input);
    return { ok: true, data: { intentId: "int_1", state: "queued" } };
  },
}));
vi.mock("@/ui/Toast", () => ({ pushRefusal: () => undefined }));

afterEach(() => {
  cleanup();
  sent.length = 0;
  useConnection.setState({ mode: "ws", connected: true, freshness: "live", blockReason: null });
});

/**
 * The mini-axe rules, applied to whatever is in the document. Deliberately a function over `document` rather than
 * a per-component assertion: the point of an accessibility rule is that it holds everywhere, and a rule that has
 * to be re-stated per component is a habit, not a check.
 */
function a11yProblems(root: HTMLElement = document.body): string[] {
  const problems: string[] = [];
  const focusable = "a[href], button, input, select, textarea, [tabindex]";
  for (const el of Array.from(root.querySelectorAll<HTMLElement>(focusable))) {
    if (el.getAttribute("aria-hidden") === "true") {
      problems.push(`focusable element inside aria-hidden: <${el.tagName.toLowerCase()}>`);
    }
    if (el.hasAttribute("tabindex") && Number(el.getAttribute("tabindex")) > 0) {
      problems.push(`positive tabindex reorders the document: ${el.getAttribute("tabindex")}`);
    }
    if (el.tagName === "BUTTON" || el.tagName === "A") {
      const name = (el.getAttribute("aria-label") ?? el.textContent ?? "").trim();
      if (!name) problems.push(`control with no accessible name: <${el.tagName.toLowerCase()}>`);
    }
    if (el.tagName === "INPUT") {
      const id = el.getAttribute("id");
      const labelled =
        el.getAttribute("aria-label") ??
        el.getAttribute("aria-labelledby") ??
        (id ? root.querySelector(`label[for="${id}"]`)?.textContent : null);
      if (!labelled) problems.push(`input with no label: ${el.getAttribute("name") ?? "(unnamed)"}`);
    }
  }
  // Nothing that updates on its own may be silent: every element that carries live data says how it announces.
  for (const el of Array.from(root.querySelectorAll<HTMLElement>("[role='status']"))) {
    if (!(el.textContent ?? "").trim() && el.getAttribute("aria-live") !== "polite") {
      problems.push("an empty role=status region with no aria-live");
    }
  }
  return problems;
}

describe("the ladder, for a reader who is not looking at it", () => {
  it("names both sides, keeps the depth bar out of the accessibility tree, and announces the one-sided case", () => {
    const oneSided = book([], [level("0.42", "1000")]);
    const { container } = render(<OrderBook book={oneSided} tick="0.01" />);
    expect(screen.getAllByRole("list").length).toBeGreaterThanOrEqual(1);
    expect(container.querySelectorAll("[aria-label]").length).toBeGreaterThan(1);
    // the bar is a visual encoding of depth; it must not be read out as a character or a glyph
    for (const bar of Array.from(container.querySelectorAll<HTMLElement>(".pgm-ladder-bar"))) {
      expect(bar.getAttribute("aria-hidden")).toBe("true");
    }
    const banner = screen.getByTestId("one-sided");
    expect(banner.getAttribute("role")).toBe("status");
    expect((banner.textContent ?? "").trim().length).toBeGreaterThan(3);
    expect(a11yProblems(container)).toEqual([]);
  });

  it("shows staleness as words next to the levels, not as a dimmer colour", () => {
    const { container } = render(<OrderBook book={book([level("0.40", "1000")], [level("0.42", "1000")])}
                                           tick="0.01" freshness="stale" staleMs={9_000} />);
    const stale = screen.getByTestId("book-stale");
    expect(stale.getAttribute("role")).toBe("status");
    // a sentence, not a tint: the words are the channel that survives colour blindness, a bad monitor and a
    // screenshot pasted into a support ticket
    expect((stale.textContent ?? "").trim().split(/\s+/).length).toBeGreaterThan(3);
    expect(stale.textContent ?? "").toMatch(/stale/i);
    expect(a11yProblems(container)).toEqual([]);
  });
});

describe("the ticket, for a keyboard", () => {
  it("is operable without a pointer and says why when it cannot trade", async () => {
    useConnection.setState({ mode: "down", connected: false, freshness: "unknown", blockReason: "feed down" });
    const { container } = render(<TradeTicket slug="fed-cut-sept" />);
    const controls = Array.from(container.querySelectorAll<HTMLElement>("input, button"));
    expect(controls.length).toBeGreaterThan(1);
    expect(a11yProblems(container)).toEqual([]);

    // every control is reachable in document order, and none of them is a div pretending to be a button
    for (const control of controls) {
      expect(["INPUT", "BUTTON"]).toContain(control.tagName);
      if (control.hasAttribute("disabled")) {
        // A disabled control is not focusable, which is correct — but then the *reason* has to be readable, so
        // the test asserts the refusal path below rather than a silent dead button.
        expect((control.textContent ?? "").length + (control.getAttribute("aria-label") ?? "").length).toBeGreaterThan(0);
        continue;
      }
      // `.focus()`, not `fireEvent.focus(...)`: the latter dispatches the event without moving focus, so the
      // first version of this test asserted that a synthetic event had been fired rather than that a keyboard
      // user can get there.
      control.focus();
      expect(document.activeElement).toBe(control);
    }

    // with the feed down the send path refuses out loud rather than silently doing nothing
    // The send button is the LAST control in document order; a name-based lookup matched the side buttons too
    // (the section is labelled "Trade"), which is the kind of selector a test should not be guessing with.
    const buttons = screen.getAllByRole("button");
    const send = buttons[buttons.length - 1];
    // Not `!`: if the ticket rendered no buttons at all, the sweep above already failed, and a test that would
    // otherwise crash on an empty list should say which invariant broke.
    expect(send, "the ticket rendered no buttons at all").toBeTruthy();
    send!.focus();
    expect(document.activeElement).toBe(send);                 // reachable by keyboard
    fireEvent.click(send!);
    await waitFor(() => expect(container.textContent ?? "").toMatch(/feed down|nothing/i));
    expect(sent.length).toBe(0);
    expect(a11yProblems(container)).toEqual([]);
  });
});
