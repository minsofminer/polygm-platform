/**
 * The keyboard map, as data, so the help overlay lists every shortcut by construction (P08 D4's "every
 * shortcut listed" is a claim about two things matching, which is only true if one generates the other).
 *
 * Rules: single-key chords are only honoured when the focus is not in an input; every action also has a
 * click target (the keyboard is an accelerator, never the only door); `Esc` is never bound to anything that
 * blocks itself (web/DESIGN.md §7: no focus traps, `Esc` works everywhere except while a request is in
 * flight, and that is announced).
 */
import { t } from "@/i18n/t";   // labels come from the dictionary, so the
                               // tab bar and the help overlay cannot disagree about a name

export type Shortcut = {
  keys: string;
  /** Normalised matcher for the global handler. */
  match: (event: KeyboardEvent) => boolean;
  action: "palette" | "help" | "trade" | "cancel-all" | "close" | "tab-markets" | "tab-tape" | "tab-trade" | "tab-portfolio" | "tab-profile";
  label: string;
  /** Whether the chord is honoured while a text field has focus. */
  inField: boolean;
};

const mod = (event: KeyboardEvent) => event.metaKey || event.ctrlKey;
const plain = (key: string) => (event: KeyboardEvent) => !mod(event) && !event.altKey && event.key === key;

export const SHORTCUTS: Shortcut[] = [
  { keys: "⌘K / Ctrl+K", match: (e) => mod(e) && e.key.toLowerCase() === "k", action: "palette", label: t("shell.shortcut.palette.label"), inField: true },
  { keys: "?", match: plain("?"), action: "help", label: t("shell.shortcut.help.label"), inField: false },
  { keys: "T", match: plain("t"), action: "trade", label: t("shell.shortcut.trade.label"), inField: false },
  { keys: "C", match: plain("c"), action: "cancel-all", label: t("shell.shortcut.cancelAll.label"), inField: false },
  { keys: "Esc", match: plain("Escape"), action: "close", label: t("shell.shortcut.close.label"), inField: true },
  { keys: "1", match: plain("1"), action: "tab-markets", label: t("shell.shortcut.tab1.label"), inField: false },
  { keys: "2", match: plain("2"), action: "tab-tape", label: t("shell.shortcut.tab2.label"), inField: false },
  { keys: "3", match: plain("3"), action: "tab-trade", label: t("shell.shortcut.tab3.label"), inField: false },
  { keys: "4", match: plain("4"), action: "tab-portfolio", label: t("shell.shortcut.tab4.label"), inField: false },
  { keys: "5", match: plain("5"), action: "tab-profile", label: t("shell.shortcut.tab5.label"), inField: false },
];

export function dispatchFor(event: KeyboardEvent, focusInField: boolean): Shortcut | null {
  for (const shortcut of SHORTCUTS) {
    if (focusInField && !shortcut.inField) continue;
    if (event.altKey || (event.shiftKey && event.key !== "?")) continue;
    if (shortcut.match(event)) return shortcut;
  }
  return null;
}

export const TAB_HREFS: Record<string, string> = {
  "tab-markets": "/markets",
  "tab-tape": "/tape",
  "tab-trade": "/terminal",
  "tab-portfolio": "/portfolio",
  "tab-profile": "/profile",
};

/** The five tabs, and the argument for them: Markets (find), Tape (see), Trade (act), Portfolio (what I
 *  hold), Profile (what I control). The obvious set survives because each has a distinct verb; "Watchlist"
 *  and "Leaderboard" lose because they are *content inside* Markets, not a destination — a fifth tab that
 *  duplicates a first tab is how a five-tab bar becomes a six-tab bar next quarter. */
export const MOBILE_TABS = [
  { key: "tab-markets", label: t("shell.nav.markets"), href: "/markets" },
  { key: "tab-tape", label: t("shell.nav.tape"), href: "/tape" },
  { key: "tab-trade", label: t("shell.nav.trade"), href: "/terminal" },
  { key: "tab-portfolio", label: t("shell.nav.portfolio"), href: "/portfolio" },
  { key: "tab-profile", label: t("shell.nav.profile"), href: "/profile" },
] as const;
