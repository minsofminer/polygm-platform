/**
 * Haptics, and the shortest possible whitelist.
 *
 * A phone that buzzes is a phone the user checks, so the vocabulary is *closed* and it has two entries:
 *
 *  - **fill** → `notificationOccurred("success")`, exactly once, when an order is confirmed as filled.
 *  - **reject** → `notificationOccurred("error")`, when an order is refused or a trade is blocked by the risk gate.
 *
 * Everything else in the product is silent. Not "most things" — everything: a button that buzzes on tap makes the
 * fill buzz meaningless, and a haptic on a menu makes the phone feel broken rather than responsive. `impactOccurred`
 * is used for nothing at all, which is why `haptic()` takes a *semantic* event rather than a style: the call sites
 * are not allowed to make taste decisions about the hardware.
 *
 * Three properties that matter more than the buzz:
 *
 *  - **It never throws.** The bridge may be absent (a browser tab), the method may be missing (older Telegram), and
 *    the call may be refused. Haptics are decoration; a decoration that breaks a trade is a bug.
 *  - **It respects reduced motion** (the OS setting is about the *device* not vibrating at you, too).
 *  - **It is not called twice for one event.** Duplicate buzzes are how a user learns to ignore them.
 */
import { tma } from "@/telegram/bridge";

export type HapticEvent = "fill" | "reject";

const EVENTS: Record<HapticEvent, "success" | "error"> = { fill: "success", reject: "error" };

/** The two events that may buzz, exported so a test — and the P12 gate — can assert the list has not grown. */
export const HAPTIC_EVENTS: HapticEvent[] = ["fill", "reject"];

export type HapticHost = {
  notificationOccurred?: (type: "error" | "success" | "warning") => void;
  impactOccurred?: (style: "light" | "medium" | "heavy" | "rigid" | "soft") => void;
};

export function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function haptic(event: HapticEvent, host?: HapticHost | null): boolean {
  if (!HAPTIC_EVENTS.includes(event)) return false;
  if (prefersReducedMotion()) return false;
  const target = host ?? (tma() as HapticHost | null);
  try {
    target?.notificationOccurred?.(EVENTS[event]);
    return !!target?.notificationOccurred;
  } catch {
    return false;
  }
}

/**
 * A one-per-event gate, for the paths that can fire more than once for one logical thing: the fill notification
 * arrives over a poll that may answer twice, and a refused order can be re-submitted by an impatient tap. The key is
 * the caller's (an intent id, an update id) so the dedupe is about *the order*, not about time.
 */
export function onceHaptic(): (event: HapticEvent, key: string) => boolean {
  const seen = new Set<string>();
  return (event: HapticEvent, key: string) => {
    const k = `${event}:${key}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return haptic(event);
  };
}
