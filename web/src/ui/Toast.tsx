/**
 * Toasts with dedupe. The dedupe is not a nicety: at 20 updates a second, a refused order produces one
 * toast per frame in the naive implementation, and the user's screen becomes an error waterfall that hides
 * the market. Key = the refusal code plus the request id, and repeats bump a counter.
 */
"use client";
import { create } from "zustand";
import { t } from "@/i18n/t";

export type Toast = {
  id: string;
  text: string;
  tone: "info" | "success" | "error";
  count: number;
  /** 0 means it stays until dismissed — used for a refusal that blocks an action the user is looking at. */
  ttlMs: number;
  /**
   * The toast is playing its exit. `dismiss` stays the only thing that removes a row, and this is what a click
   * sets first, so the removal happens on `animationend` rather than between two frames.
   *
   * It is also the field a TTL would use: `ttlMs` is carried by every row and **no timer reads it yet** (there is
   * no `setTimeout` in this module — verified, and stated here rather than implied, because a comment claiming an
   * auto-dismiss the product does not have is worse than no comment). When one lands it calls `requestDismiss`,
   * so a toast leaving on its own and a toast leaving on a click are the same code path.
   */
  dismissing: boolean;
};

type Store = {
  toasts: Toast[];
  push: (args: { key: string; text: string; tone?: Toast["tone"]; ttlMs?: number }) => void;
  /** Start the exit. Does not remove — `dismiss` does, on the animation's own end. */
  requestDismiss: (id: string) => void;
  dismiss: (id: string) => void;
};

export const useToasts = create<Store>((set) => ({
  toasts: [],
  push: ({ key, text, tone = "info", ttlMs = 6_000 }) =>
    set((state) => {
      const found = state.toasts.find((x) => x.id === key);
      if (found) {
        return {
          // A repeat that arrives while the row is leaving un-cancels the exit: the news is newer than the click,
          // and a toast that vanished mid-update would take the update with it.
          toasts: state.toasts.map((x) =>
            x.id === key ? { ...x, count: x.count + 1, text, tone, ttlMs, dismissing: false } : x,
          ),
        };
      }
      return { toasts: [{ id: key, text, tone, count: 1, ttlMs, dismissing: false }, ...state.toasts].slice(0, 4) };
    }),
  requestDismiss: (id) =>
    set((state) => ({ toasts: state.toasts.map((x) => (x.id === id ? { ...x, dismissing: true } : x)) })),
  dismiss: (id) => set((state) => ({ toasts: state.toasts.filter((x) => x.id !== id) })),
}));

export function pushRefusal(code: string, message: string, requestId: string): void {
  useToasts.getState().push({
    key: `refusal:${code}:${requestId}`,
    text: message,
    tone: "error",
    // A refusal about an action the user just took must not vanish while they are reading it.
    ttlMs: 0,
  });
}

/** Ask the system whether motion is welcome. The exit cannot wait for an `animationend` that will not fire. */
function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
    : false;
}

export function Toasts() {
  const { toasts, dismiss, requestDismiss } = useToasts();
  if (!toasts.length) return null;
  return (
    <div className="toasts" role="region" aria-label={t("common.state.errorTitle")}>
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`toast ${toast.dismissing ? "pgm-toast-out" : "pgm-toast-in"}`}
          data-tone={toast.tone}
          role={toast.tone === "error" ? "alert" : "status"}
          onAnimationEnd={(event) => {
            if (!toast.dismissing || event.target !== event.currentTarget) return;
            dismiss(toast.id);
          }}
        >
          <span>{toast.text}</span>
          {toast.count > 1 ? (
            <span className="toast__count">
              ×{toast.count} — {t("common.button.dismiss")}
            </span>
          ) : null}
          <button
            type="button"
            className="button"
            onClick={() => (prefersReducedMotion() ? dismiss(toast.id) : requestDismiss(toast.id))}
          >
            {t("common.button.dismiss")}
          </button>
        </div>
      ))}
    </div>
  );
}
