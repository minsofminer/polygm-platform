/**
 * The data layer's contract, asserted rather than assumed.
 *
 * This file exists because replacing a library with 40 lines moves the risk from "the library is wrong" to "our
 * four assumptions about it are wrong", and three of those assumptions are load-bearing in the product:
 *
 *  - a fresh key must not re-request (the feed is the authority on a live price, and a second request is a second,
 *    differently-aged copy of the same number);
 *  - concurrent mounts of the same key must share one request (a screen that renders four panels must not send
 *    four identical reads);
 *  - a failure must stay a failure (no retry storm on a refusal, which is a decision) and must keep its error;
 *  - `invalidate` must refresh what is mounted, or a deleted address would stay on screen after a successful write.
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { invalidate, peek, resetCache, useAction, useResource } from "./data";

beforeEach(() => {
  resetCache();
});

describe("useResource", () => {
  it("serves a fresh key from the cache without a second request", async () => {
    const fn = vi.fn().mockResolvedValue("first");
    const one = renderHook(() => useResource(["k"], fn, { staleTime: 60_000 }));
    await waitFor(() => expect(one.result.current.data).toBe("first"));

    const two = renderHook(() => useResource(["k"], fn, { staleTime: 60_000 }));
    expect(two.result.current.data).toBe("first"); // cache, on the very first render
    expect(two.result.current.isPending).toBe(false);
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(1));
  });

  it("shares one in-flight request between concurrent mounts", async () => {
    let release: (v: string) => void = () => {};
    const fn = vi.fn(() => new Promise<string>((res) => (release = res)));
    const one = renderHook(() => useResource(["shared"], fn));
    const two = renderHook(() => useResource(["shared"], fn));
    await act(async () => {
      release("value");
    });
    await waitFor(() => expect(one.result.current.data).toBe("value"));
    await waitFor(() => expect(two.result.current.data).toBe("value"));
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("refetch ignores freshness, and a stale key refetches when it is mounted again", async () => {
    let n = 0;
    const fn = vi.fn(async () => `v${++n}`);
    const { result, unmount } = renderHook(() => useResource(["k"], fn, { staleTime: 60_000 }));
    await waitFor(() => expect(result.current.data).toBe("v1"));
    act(() => result.current.refetch());
    await waitFor(() => expect(result.current.data).toBe("v2"));

    unmount();
    resetCache();
    const again = renderHook(() => useResource(["k"], fn, { staleTime: 60_000 }));
    await waitFor(() => expect(again.result.current.data).toBe("v3"));
    expect(fn).toHaveBeenCalledTimes(3);
  });

  it("keeps a failure as a failure, with its message, and does not retry by itself", async () => {
    const fn = vi.fn().mockRejectedValue(new Error("TOTP_REQUIRED: enrol first"));
    const { result } = renderHook(() => useResource(["boom"], fn, { staleTime: 0 }));
    await waitFor(() => expect(result.current.isError).toBe(true));
    if (!result.current.isError) throw new Error("unreachable: narrowed above");
    expect(result.current.error.message).toBe("TOTP_REQUIRED: enrol first");
    expect(result.current.isPending).toBe(false);
    await new Promise((r) => setTimeout(r, 25));
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("invalidate marks the key stale and re-runs the fetch that is mounted", async () => {
    let rows = [{ id: "a1" }];
    const fn = vi.fn(async () => rows);
    const { result } = renderHook(() => useResource(["wallet", "addresses"], fn, { staleTime: 60_000 }));
    await waitFor(() => expect(result.current.data).toEqual([{ id: "a1" }]));

    rows = []; // the write happened elsewhere
    await act(async () => {
      invalidate(["wallet", "addresses"]);
    });
    await waitFor(() => expect(result.current.data).toEqual([]));
    expect(fn).toHaveBeenCalledTimes(2);
    // ...and the cache agrees, so the next mount does not fetch again.
    expect(peek(["wallet", "addresses"])).toEqual([]);
  });

  it("invalidate only touches keys under the prefix", async () => {
    const other = vi.fn().mockResolvedValue("billing");
    const target = vi.fn().mockResolvedValue("addresses");
    renderHook(() => useResource(["billing", "entitlement"], other, { staleTime: 60_000 }));
    renderHook(() => useResource(["wallet", "addresses"], target, { staleTime: 60_000 }));
    await waitFor(() => expect(other).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(target).toHaveBeenCalledTimes(1));
    await act(async () => {
      invalidate(["wallet"]);
    });
    await waitFor(() => expect(target).toHaveBeenCalledTimes(2));
    expect(other).toHaveBeenCalledTimes(1);
  });
});

describe("useAction", () => {
  it("reports pending while it runs and keeps the error", async () => {
    let fail: (e: Error) => void = () => {};
    const fn = vi.fn(() => new Promise<void>((_res, rej) => (fail = rej)));
    const { result } = renderHook(() => useAction<number, void>(fn));
    expect(result.current.isPending).toBe(false);
    act(() => result.current.mutate(1));
    expect(result.current.isPending).toBe(true);
    await act(async () => {
      fail(new Error("COOLDOWN: 24 hours between changes"));
    });
    expect(result.current.isPending).toBe(false);
    expect(result.current.error?.message).toBe("COOLDOWN: 24 hours between changes");
    expect(fn).toHaveBeenCalledTimes(1); // no retry: a refusal is a decision
  });
});
