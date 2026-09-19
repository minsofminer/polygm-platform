import { describe, expect, it, beforeEach } from "vitest";
import { pushRefusal, useToasts } from "./Toast";

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
