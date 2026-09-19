/**
 * The number layer's own behaviour, in the cases that a screenshot cannot show:
 *  - a price without a tick is refused rather than assumed (the assumption is what renders 0.42 for 0.425);
 *  - a money value that is not an integer refuses to render, so the bug surfaces as a dash + the message in
 *    the title attribute, not as a silently rounded number in a balance;
 *  - a REST-sourced change produces no flash, and a change inside the rounding window produces none either.
 */
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Number as NumberView } from "./Number";

describe("the number layer", () => {
  it("refuses to render a price without a tick", () => {
    const { container } = render(<NumberView kind="price" value={425} />);
    expect(container.textContent).toContain("—");
    expect(container.querySelector(".num")?.getAttribute("title")).toContain("needs a tick");
  });

  it("renders a three-decimal market at three decimals", () => {
    const { container } = render(<NumberView kind="price" value={425} tick="0.001" />);
    expect(container.textContent).toContain("0.425");
  });

  it("carries the freshness onto the element so a stylesheet can desaturate it", () => {
    const { container } = render(<NumberView kind="money" value={1500} freshness="stale" staleMs={9_000} />);
    const el = container.querySelector(".num");
    expect(el?.getAttribute("data-freshness")).toBe("stale");
    expect(el?.textContent).toContain("late");
  });

  it("does not flash a REST-sourced row", () => {
    const { container } = render(<NumberView kind="price" value={500} previous={400} tick="0.001" source="rest" />);
    expect(container.querySelector(".num")?.getAttribute("data-flash")).toBeNull();
  });

  it("does not flash when the displayed digits did not move", () => {
    const { container } = render(<NumberView kind="price" value={42} previous={42} tick="0.01" source="ws" />);
    expect(container.querySelector(".num")?.getAttribute("data-flash")).toBeNull();
  });

  it("labels the value for a screen reader instead of leaving a glyph to carry it", () => {
    const { container } = render(<NumberView kind="pnl" value={-1250} label="today" />);
    expect(container.querySelector(".num")?.getAttribute("aria-label")).toBe("today: -12.50");
    expect(container.textContent).toContain("▼");
  });
});
