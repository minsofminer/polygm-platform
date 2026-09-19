import { beforeEach, describe, expect, it } from "vitest";
import { connectionLabel, IDLE, useConnection } from "./connection";

const apply = (patch: Partial<typeof IDLE>) => useConnection.getState().set(patch);

describe("the connection store is the only answer to 'can I trade'", () => {
  beforeEach(() => useConnection.setState(IDLE));

  it("starts closed, because 'we do not know yet' must not default to tradable", () => {
    expect(useConnection.getState().canTrade()).toBe(false);
    expect(useConnection.getState().whyNot()).toContain("has not started");
    expect(connectionLabel(useConnection.getState())).toBe("down");
  });

  it("closes on a disconnect and says which kind of disconnect it is", () => {
    apply({ mode: "ws", connected: true, freshness: "live", blockReason: null });
    expect(useConnection.getState().canTrade()).toBe(true);
    apply({ mode: "down", connected: false, blockReason: "disconnected (heartbeat-deadline)" });
    expect(useConnection.getState().canTrade()).toBe(false);
    expect(useConnection.getState().whyNot()).toContain("heartbeat-deadline");
  });

  it("closes on staleness alone, while still connected — the market moved, we did not hear", () => {
    apply({ mode: "ws", connected: true, freshness: "stale", blockReason: null });
    expect(useConnection.getState().canTrade()).toBe(false);
    expect(useConnection.getState().whyNot()).toContain("may have moved");
    apply({ freshness: "blocking" });
    expect(useConnection.getState().whyNot()).toContain("too old");
  });

  it("never closes the escape hatch", () => {
    useConnection.setState(IDLE);
    expect(useConnection.getState().cancelAllAvailable()).toBe(true);
    apply({ mode: "down", freshness: "blocking" });
    expect(useConnection.getState().cancelAllAvailable()).toBe(true);
  });

  it("surfaces the gap count as part of the label path, so the banner and the dot agree", () => {
    apply({ mode: "ws", connected: true, freshness: "live", gapCount: 611, resyncing: true });
    expect(connectionLabel(useConnection.getState())).toBe("live");
    expect(useConnection.getState().gapCount).toBe(611);
  });
});
