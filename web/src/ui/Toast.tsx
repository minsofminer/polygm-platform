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
};

type Store = {
  toasts: Toast[];
  push: (args: { key: string; text: string; tone?: Toast["tone"]; ttlMs?: number }) => void;
  dismiss: (id: string) => void;
};

export const useToasts = create<Store>((set) => ({
  toasts: [],
  push: ({ key, text, tone = "info", ttlMs = 6_000 }) =>
    set((state) => {
      const found = state.toasts.find((x) => x.id === key);
      if (found) {
        return {
          toasts: state.toasts.map((x) => (x.id === key ? { ...x, count: x.count + 1, text, tone, ttlMs } : x)),
        };
      }
      return { toasts: [{ id: key, text, tone, count: 1, ttlMs }, ...state.toasts].slice(0, 4) };
    }),
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

export function Toasts() {
  const { toasts, dismiss } = useToasts();
  if (!toasts.length) return null;
  return (
    <div className="toasts" role="region" aria-label={t("common.state.errorTitle")}>
      {toasts.map((toast) => (
        <div key={toast.id} className="toast" data-tone={toast.tone} role={toast.tone === "error" ? "alert" : "status"}>
          <span>{toast.text}</span>
          {toast.count > 1 ? (
            <span className="toast__count">
              ×{toast.count} — {t("common.button.dismiss")}
            </span>
          ) : null}
          <button type="button" className="button" onClick={() => dismiss(toast.id)}>
            {t("common.button.dismiss")}
          </button>
        </div>
      ))}
    </div>
  );
}
