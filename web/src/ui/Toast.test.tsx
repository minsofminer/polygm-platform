import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Toasts, pushRefusal, useToasts } from "./Toast";

describe("toast dedupe", () => {
  beforeEach(() => useToasts.setState({ toasts: [] }));

  it("collapses a repeated refusal into one row with a count", () => {
    for (let i = 0; i < 40; i++) pushRefusal("RISK_HALT", "trading is halted", "req_9");
    const { toasts } = useToasts.getState();
    expect(toasts.length).toBe(1);
    expect(toasts[0]?.count).toBe(40);
    // 40 frames of refusals must not be 40 DOM nodes: the queue has a ceiling by design.
    expect(toasts[0]?.ttlMs).toBe(0);
  });

  it("keeps a refusal and an unrelated notice apart, and caps the stack at four", () => {
    useToasts.getState().push({ key: "a", text: "one", tone: "info" });
    for (let i = 0; i < 10; i++) {
      useToasts.getState().push({ key: `k${i}`, text: `t${i}`, tone: "success" });
    }
    expect(useToasts.getState().toasts.length).toBe(4);
    expect(useToasts.getState().toasts.some((x) => x.id === "a")).toBe(false);
  });
});

describe("the toast exits instead of vanishing", () => {
  beforeEach(() => useToasts.setState({ toasts: [] }));
  afterEach(() => vi.unstubAllGlobals());

  it("plays the exit, and removes the row only when the animation ends", () => {
    useToasts.getState().push({ key: "k", text: "saved", tone: "success" });
    const { container } = render(<Toasts />);
    const row = container.querySelector(".toast");
    expect(row?.className).toContain("pgm-toast-in");

    fireEvent.click(screen.getByRole("button"));
    // Still in the DOM, now leaving: removal between frames is the jump this pass exists to remove.
    expect(useToasts.getState().toasts.length).toBe(1);
    const leaving = container.querySelector(".toast");
    expect(leaving?.className).toContain("pgm-toast-out");

    fireEvent.animationEnd(leaving!);
    expect(useToasts.getState().toasts.length).toBe(0);
  });

  it("ignores an animationend from a child, so a nested motion cannot dismiss the row", () => {
    useToasts.getState().push({ key: "k2", text: "saved", tone: "success" });
    render(<Toasts />);
    fireEvent.click(screen.getByRole("button"));
    const leaving = document.querySelector(".toast")!;
    const inner = leaving.querySelector("span")!;
    fireEvent.animationEnd(inner);
    expect(useToasts.getState().toasts.length).toBe(1);
  });

  it("removes immediately when the user has asked for reduced motion", () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduced-motion"), media: q,
                                                  addEventListener() {}, removeEventListener() {} }));
    useToasts.getState().push({ key: "k3", text: "saved", tone: "success" });
    render(<Toasts />);
    fireEvent.click(screen.getByRole("button"));
    // No animation will run (`animation: none !important`), so an exit that waited for `animationend` would hang.
    expect(useToasts.getState().toasts.length).toBe(0);
  });

  it("un-cancels a leaving row when the same key is pushed again", () => {
    useToasts.getState().push({ key: "k4", text: "first", tone: "info" });
    useToasts.getState().requestDismiss("k4");
    expect(useToasts.getState().toasts[0]?.dismissing).toBe(true);
    useToasts.getState().push({ key: "k4", text: "second", tone: "info" });
    const row = useToasts.getState().toasts[0];
    expect(row?.dismissing).toBe(false);
    expect(row?.count).toBe(2);
  });
});
