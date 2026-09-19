/**
 * The Telegram Mini App bridge, and the boundary it must respect: **the browser never validates `initData`.**
 * The client reads the raw string out of `window.Telegram.WebApp.initData` and hands it to
 * `POST /api/session/telegram` (the Next proxy → P07's `/v1/auth/telegram`), which recomputes the HMAC
 * with the bot's secret key server-side. Anything the page can compute, the page can forge — so nothing is
 * computed here except *presentation* facts (theme, insets, whether the button exists).
 *
 * The shape mirrors what core.telegram.org/bots/webapps documents. `initDataUnsafe` is provided for the UI
 * only (a first name in a greeting); identity decisions never read it.
 */
export type TmaUnsafeUser = { id: number; first_name?: string; username?: string; language_code?: string };

export type TmaGlobal = {
  initData: string;
  initDataUnsafe?: { user?: TmaUnsafeUser };
  colorScheme?: "light" | "dark";
  themeParams?: Record<string, string>;
  platform?: string;
  isExpanded?: boolean;
  viewportHeight?: number;
  safeAreaInset?: { top: number; bottom: number; left: number; right: number };
  contentSafeAreaInset?: { top: number; bottom: number; left: number; right: number };
  MainButton?: { show: () => void; hide: () => void; setText: (t: string) => void; onClick: (fn: () => void) => void; offClick: (fn: () => void) => void; disable: () => void; enable: () => void };
  BackButton?: { show: () => void; hide: () => void; onClick: (fn: () => void) => void; offClick: (fn: () => void) => void };
  HapticFeedback?: { impactOccurred: (style: "light" | "medium" | "heavy" | "rigid" | "soft") => void; notificationOccurred: (type: "error" | "success" | "warning") => void };
  setHeaderColor?: (color: string) => void;
  setBackgroundColor?: (color: string) => void;
  ready?: () => void;
  expand?: () => void;
};

declare global {
  interface Window {
    Telegram?: { WebApp?: TmaGlobal };
  }
}

export function tma(): TmaGlobal | null {
  if (typeof window === "undefined") return null;
  return window.Telegram?.WebApp ?? null;
}

/** Detection is `initData` being a non-empty string, not "the object exists": the script tag that defines
 *  `window.Telegram.WebApp` is loaded on every Telegram page, and an app opened in a browser tab that
 *  happens to define the object must not start pretending it has a signed session. */
export function isTma(): boolean {
  const app = tma();
  return !!app && typeof app.initData === "string" && app.initData.length > 0;
}

/** Which flows use the native MainButton, stated once so it is consistent across the app: the MainButton
 *  is the *confirm* affordance and nothing else. Navigation gets the BackButton; every other action stays
 *  in the page, because a user who learns "the big bar means continue" must never be wrong about it. */
export type MainButtonUse = "trade-confirm" | "withdraw-confirm" | "key-export-confirm";
const USES_MAIN_BUTTON = new Set<MainButtonUse>(["trade-confirm", "withdraw-confirm", "key-export-confirm"]);

export function usesMainButton(use: MainButtonUse): boolean {
  return USES_MAIN_BUTTON.has(use);
}

export function showMainButton(text: string, onTap: () => void): () => void {
  const app = tma();
  if (!app?.MainButton) return () => undefined;
  const button = app.MainButton;
  button.setText(text);
  button.onClick(onTap);
  button.show();
  return () => {
    button.offClick(onTap);
    button.hide();
  };
}

/**
 * Back behaviour inside the webview: Telegram draws its own back chevron and users press it, so a screen
 * that owns the browser history and not the BackButton ends up with two different back stories. The rule
 * the shell implements: the BackButton pops *our* stack first, and when our stack is empty it hides itself
 * and lets Telegram leave the app.
 */
export function showBackButton(onPress: () => void): () => void {
  const app = tma();
  if (!app?.BackButton) return () => undefined;
  const bb = app.BackButton;
  bb.onClick(onPress);
  bb.show();
  return () => {
    bb.offClick(onPress);
    bb.hide();
  };
}

/** Haptics are for the confirmation of a trade and nothing else (P08 D2). A product that buzzes on hover
 *  teaches the user to turn feedback off — including the one that matters. */
export function hapticConfirm(): void {
  tma()?.HapticFeedback?.impactOccurred("medium");
}

export function hapticRefused(): void {
  tma()?.HapticFeedback?.notificationOccurred("error");
}

/** Theme sync: inside the webview the *host's* colour scheme wins over our stored preference, because a
 *  dark Telegram with a bright white app is a physical discomfort, not a style choice. */
export function telegramTheme(): "dark" | "light" | null {
  const scheme = tma()?.colorScheme;
  return scheme === "dark" || scheme === "light" ? scheme : null;
}

/**
 * The viewport-height problem, and the jank that comes with fixing it naively. The keyboard opening shrinks
 * the visual viewport, so any `height: <viewport>px` written on resize makes the layout jump mid-typing.
 * We therefore report an inset for the *bottom bar* only (safe area), and never write a height. `dvh` in
 * CSS handles the rest; this number exists so the fixed tab bar can sit above the keyboard instead of
 * under it.
 */
export function bottomInsetPx(): number {
  const app = tma();
  const safe = app?.safeAreaInset?.bottom ?? 0;
  const content = app?.contentSafeAreaInset?.bottom ?? safe;
  return Math.max(0, Math.round(content));
}
