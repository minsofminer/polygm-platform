import { describe, expect, it } from "vitest";
import { INITIAL, isBlockedFor, shellVisibleDuring, useAuth } from "./session";

describe("the four states are the whole machine", () => {
  it("starts unauthenticated with nothing held", () => {
    expect(useAuth.getState().state).toBe("unauthenticated");
    expect(useAuth.getState().heldUser).toBeNull();
  });

  it("keeps the last user on screen while it probes, so the shell never blinks", () => {
    useAuth.getState().authenticated({ id: "u_1", handle: "ana" });
    useAuth.getState().beginProbe(null);
    const snap = useAuth.getState();
    expect(snap.state).toBe("authenticating");
    expect(snap.user).toBeNull();
    expect(snap.heldUser?.id).toBe("u_1");
    expect(shellVisibleDuring(snap)).toBe(true);
    expect(isBlockedFor(snap, true)).toBe(true); // protected *content* is still gated, the frame is not
  });

  it("a probe with nothing held is the logged-out state, not a fake authenticating state", () => {
    useAuth.setState(INITIAL);
    useAuth.getState().beginProbe(null);
    expect(useAuth.getState().heldUser).toBeNull();
    expect(shellVisibleDuring(useAuth.getState())).toBe(false);
  });

  it("expiry carries the reason the server gave, in words", () => {
    useAuth.setState(INITIAL);
    useAuth.getState().authenticated({ id: "u_2" });
    useAuth.getState().expired({ reason: "a refresh token was used twice, so the family was revoked" });
    expect(useAuth.getState().state).toBe("expired");
    expect(useAuth.getState().reason).toContain("used twice");
  });

  it("a 2FA challenge is a pending flag on the current state, not a fake state of its own", () => {
    useAuth.setState(INITIAL);
    useAuth.getState().needSecondFactor({ challenge: "enter your authenticator code" });
    expect(useAuth.getState().secondFactorPending).toBe(true);
    expect(useAuth.getState().state).toBe("unauthenticated");
    useAuth.getState().authenticated({ id: "u_2", secondFactor: "totp" });
    expect(useAuth.getState().secondFactorPending).toBe(false);
    expect(useAuth.getState().user?.secondFactor).toBe("totp");
  });

  it("sign-out holds nothing at all: a remembered user after logout is a leak on a shared device", () => {
    useAuth.getState().authenticated({ id: "u_3", handle: "sam" });
    useAuth.getState().signedOut();
    expect(useAuth.getState()).toMatchObject({ state: "unauthenticated", user: null, heldUser: null, newDevice: false });
  });
});
