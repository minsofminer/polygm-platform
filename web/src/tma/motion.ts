/**
 * The Mini App's motion, as data — the same durations the stylesheet uses, available to JavaScript.
 *
 * The stylesheet is still the authority (the classes in `brand/tokens.css` do the animating); this module exists for
 * the two cases CSS cannot express: waiting for an animation to finish before doing something else (focusing the
 * amount field, releasing the MainButton) and deciding *not* to animate. A test asserts every number here equals the
 * token in `web/styles/tokens.css`, so the two cannot drift — the same discipline the Python side applies to
 * `telegrambot/motion.py`, because the chat and the Mini App must move at one speed.
 *
 * The rules this module encodes, both of them from P03 and restated in P12 because a confirmation screen is the most
 * tempting place in the product to break them:
 *
 *  1. **Nothing animates a number.** No counting up, no tweened PnL, no digits sliding. A number that moves is a
 *     number the user has to *catch*, and on a trade confirmation that is unacceptable.
 *  2. **One idea, one animation.** A card settles, then the sheet slides; never both at once. `SEQUENCE` is the
 *     order, and the page awaits it rather than firing everything and hoping.
 */

/** Milliseconds, mirroring `--pgm-dur-*` and `--pgm-flash-*`. */
export const DUR = {
  press: 100,
  micro: 80,
  small: 125,
  medium: 150,
  large: 200,
  drawer: 300,
  flashIn: 90,
  flashOut: 200,
} as const;

export const CLASS = {
  card: "pgm-tma-card",
  sheet: "pgm-tma-sheet",
  sheetClosing: "pgm-tma-sheet--closing",
  ack: "pgm-tma-ack",
  reject: "pgm-tma-reject",
  skeleton: "pgm-tma-skeleton",
  scrim: "pgm-tma-scrim",
  grabber: "pgm-tma-grabber",
} as const;

/**
 * The order a confirmation runs in: exactly TWO animation beats, and the request in between them.
 *
 * The request is a phase rather than a beat — it has no animation precisely because it is the moment the user is
 * waiting, and a spinner that moves while a trade is in flight is a spinner the user watches instead of the result.
 * The first draft of this constant listed the request as a third beat, which the module's own budget test failed:
 * "one idea, one animation" is a rule that has to survive being applied to the person writing it.
 */
export const SEQUENCE = [
  { beat: "ack", delayMs: DUR.medium, what: "the confirm row flashes its background once — the digits do not move" },
  { beat: "settle", delayMs: DUR.small, what: "the card settles under the result, after the request resolves" },
] as const;

/** The phase between the two beats, named so it is not mistaken for one. */
export const IN_FLIGHT = { animation: "none", what: "the request is out; nothing moves until it answers" } as const;

export const NEVER = [
  "count a number up",
  "tween a price or a PnL",
  "animate a skeleton that carries a value",
  "buzz on a tap that is not a fill or a refusal",
  "run two animations on one beat",
] as const;

export function durationMs(kind: keyof typeof DUR): number {
  return DUR[kind];
}

/** `withMotion(fn)` runs now, or after the settle when motion is welcome. */
export function withMotion(fn: () => void, opts: { reduced: boolean; kind?: keyof typeof DUR }): void {
  const { reduced, kind = "small" } = opts;
  if (reduced) {
    fn();
    return;
  }
  window.setTimeout(fn, DUR[kind]);
}

export function motionFindings(plan: { beats?: number; animatesNumber?: boolean; haptics?: string[] | boolean }): string[] {
  const out: string[] = [];
  const beats = plan.beats ?? 0;
  if (beats > 2) out.push("a sequence with more than two beats is motion the user cannot follow");
  if (plan.animatesNumber) out.push("the plan animates a number, which P03's number_policy forbids");
  if (Array.isArray(plan.haptics) && plan.haptics.some((h) => h !== "fill" && h !== "reject")) {
    out.push("the plan asks for a haptic that is not a fill or a refusal");
  }
  return out;
}
