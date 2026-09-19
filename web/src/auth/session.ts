/**
 * Auth state as a single source of truth (P08 D3). Four states, each with its own UI, and no fifth state
 * that means "probably signed in":
 *
 *   unauthenticated  — nothing on this device; the logged-out frame is rendered as itself, not as a flash
 *   authenticating   — a token was on the device and the server has not answered yet: the *last* shell is
 *                      kept on screen with a status line, never replaced by the logged-out page. That
 *                      choice is the whole "no flash of logged-out state" requirement.
 *   authenticated    — the shell is the real thing and `user.id` is in it
 *   expired          — the refresh failed, and the reason is part of the state, because "session ended"
 *                      and "this device was signed out because a refresh token was used twice" are
 *                      different sentences to different people (P07's REFRESH_REUSED family revocation).
 *
 * The store holds *no* token. The tokens live in httpOnly cookies set by the session proxy on this origin;
 * inside the Telegram webview the same cookies apply, with the fallback in src/telegram/reauth.ts.
 */
import { create } from "zustand";
import { subscribeWithSelector } from "zustand/middleware";
import type { ApiError } from "@/api/envelope";

export type SessionUser = { id: string; handle?: string; email?: string; secondFactor?: "totp" | null };

export type AuthState = "unauthenticated" | "authenticating" | "authenticated" | "expired";

export type AuthSnapshot = {
  state: AuthState;
  user: SessionUser | null;
  /** The last authenticated user, kept while `authenticating` so the shell does not blank. */
  heldUser: SessionUser | null;
  reason: string | null;
  error: ApiError | null;
  /** Device recognition: an unknown device gets a notice, not a wall. */
  newDevice: boolean;
  secondFactorPending: boolean;
};

type Actions = {
  beginProbe: (held: SessionUser | null) => void;
  authenticated: (user: SessionUser, extra?: { newDevice?: boolean }) => void;
  expired: (args: { reason: string; error?: ApiError | null }) => void;
  signedOut: () => void;
  needSecondFactor: (args: { challenge: string }) => void;
  clearSecondFactor: () => void;
  snapshot: () => AuthSnapshot;
};

export const INITIAL: AuthSnapshot = {
  state: "unauthenticated",
  user: null,
  heldUser: null,
  reason: null,
  error: null,
  newDevice: false,
  secondFactorPending: false,
};

export const useAuth = create<AuthSnapshot & Actions>()(
  subscribeWithSelector((set, get) => ({
    ...INITIAL,
    beginProbe: (held) =>
      set({ state: "authenticating", user: null, reason: null, error: null, heldUser: held ?? get().user ?? get().heldUser }),
    authenticated: (user, extra) =>
      set({ state: "authenticated", user, heldUser: user, reason: null, error: null, secondFactorPending: false, newDevice: extra?.newDevice ?? false }),
    expired: ({ reason, error = null }) =>
      set({ state: "expired", user: null, reason, error, secondFactorPending: false }),
    signedOut: () => set({ ...INITIAL }),
    needSecondFactor: ({ challenge }) => set({ secondFactorPending: true, reason: challenge }),
    clearSecondFactor: () => set({ secondFactorPending: false, reason: null }),
    snapshot: () => get(),
  })),
);

/**
 * Route protection without the logged-out flash: the server decides (see app/(app)/layout.tsx reading the
 * cookie through the session proxy), and the client only ever *renders the decision*. A client-side
 * "if no token then redirect" is the flash — it paints the logged-out page for a frame and then leaves.
 */
export function isBlockedFor(snapshot: AuthSnapshot, needsAuth: boolean): boolean {
  if (!needsAuth) return false;
  return snapshot.state !== "authenticated";
}

/**
 * The frame stays up during a probe only if there is a *user* to keep it up for. `authenticating` with
 * nothing held is the logged-out page in disguise, and the disguise is the flash this rule exists to remove.
 */
export function shellVisibleDuring(snapshot: AuthSnapshot): boolean {
  if (snapshot.state === "authenticated") return true;
  return snapshot.state === "authenticating" && snapshot.heldUser !== null;
}
