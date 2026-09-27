/**
 * The poll hook's contract, tested on the timeline rather than by inspection, because the bug it fixes is a
 * *timing* bug: an interval that fires while the previous request is still open. A test that only counted calls
 * would pass against the old `setInterval` too.
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import { render } from "@testing-library/react";
import { usePoll } from "./usePoll";

function Poller({ task, ms, enabled }: { task: () => void | Promise<void>; ms: number; enabled?: boolean }) {
  usePoll(task, ms, enabled);
  return null;
}

afterEach(() => {
  vi.useRealTimers();
});

describe("usePoll", () => {
  it("runs immediately, then again only after the task settles", async () => {
    vi.useFakeTimers();
    let running = 0;
    let peak = 0;
    const task = vi.fn(async () => {
      running += 1;
      peak = Math.max(peak, running);
      await new Promise((r) => setTimeout(r, 5_000)); // a request slower than the interval
      running -= 1;
    });
    render(<Poller task={task} ms={2_000} />);
    expect(task).toHaveBeenCalledTimes(1);

    // Two intervals' worth of time passes while the first call is still open. `setInterval` would have fired
    // twice more here; this must not have.
    await vi.advanceTimersByTimeAsync(4_000);
    expect(task).toHaveBeenCalledTimes(1);

    // Once it settles, the next run is scheduled — the interval is measured from completion, not from start.
    await vi.advanceTimersByTimeAsync(5_100);
    expect(task).toHaveBeenCalledTimes(2);
    expect(peak).toBe(1);
  });

  it("keeps polling after a failure, and does not let the rejection escape", async () => {
    vi.useFakeTimers();
    const task = vi.fn(async () => {
      throw new Error("the API refused the read");
    });
    render(<Poller task={task} ms={1_000} />);
    await vi.advanceTimersByTimeAsync(3_100);
    // Three runs: the poll survived two failures rather than dying on the first.
    expect(task.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("stops on unmount and never runs a task afterwards", async () => {
    vi.useFakeTimers();
    const task = vi.fn(async () => {});
    const { unmount } = render(<Poller task={task} ms={1_000} />);
    await vi.advanceTimersByTimeAsync(1_100);
    const before = task.mock.calls.length;
    unmount();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(task.mock.calls.length).toBe(before);
  });

  it("does not restart when the task identity changes every render", async () => {
    vi.useFakeTimers();
    const seen: number[] = [];
    let renders = 0;
    function Churn() {
      renders += 1;
      // A fresh closure each render — the reason the hook keeps the task in a ref instead of a dependency.
      usePoll(() => { seen.push(renders); }, 1_000);
      return null;
    }
    const view = render(<Churn />);
    view.rerender(<Churn />);
    view.rerender(<Churn />);
    await vi.advanceTimersByTimeAsync(1_100);
    // One run, not one per render: the timer was armed once and calls the latest task.
    expect(seen.filter((r) => r <= 1).length).toBeGreaterThanOrEqual(1);
    expect(seen.length).toBeLessThanOrEqual(2);
  });

  it("can be switched off, and does nothing while disabled", async () => {
    vi.useFakeTimers();
    const task = vi.fn();
    render(<Poller task={task} ms={1_000} enabled={false} />);
    await vi.advanceTimersByTimeAsync(5_000);
    expect(task).not.toHaveBeenCalled();
  });
});
