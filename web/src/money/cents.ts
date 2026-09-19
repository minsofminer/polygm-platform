/**
 * The money path. web/DESIGN.md §5 makes this file the only parser/formatter in the app: money is integer
 * cents at every boundary, no money value is ever rendered from an unrounded float, prices always use `.`
 * as the decimal separator in every locale, grouping is a thin space, and precision comes from the market's
 * `minimum_tick_size` rather than an assumption of two decimals.
 *
 * The type is branded so `number` cannot be passed by accident, and the constructors refuse non-integers.
 * The refusal is behavioural, not documentary: `cents(42.5)` throws. A float in the money path fails the
 * build because `tools/p08-gate-check.py` c4 forbids `parseFloat`/`toFixed`/`Intl.NumberFormat` anywhere
 * else under `web/src`.
 */

export type Cents = number & { readonly __cents: unique symbol };
export type TickSize = string;

export class MoneyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MoneyError";
  }
}

const THIN_SPACE = "\u2009";

/** Wrap an integer that is already cents. Non-integers throw: this is where a float dies. */
export function cents(value: number): Cents {
  if (typeof value !== "number" || !Number.isFinite(value) || !Number.isInteger(value)) {
    throw new MoneyError(`cents must be an integer, got ${typeof value} ${String(value)}`);
  }
  if (Math.abs(value) > Number.MAX_SAFE_INTEGER / 100) {
    throw new MoneyError("cents overflow: the value is beyond safe integer range");
  }
  return value as Cents;
}

/**
 * Parse a decimal *string* — "1.5", "0.001", "-12.75" — into cents. Accepting a string is the point: JSON
 * from the API is parsed as a string, and a JS number has already lost the information about how many
 * decimals the caller meant. Anything past two decimals is a price, not money, and goes through
 * `priceToUnits` with the market's tick.
 */
export function centsFromDecimal(input: string): Cents {
  const text = input.trim().replace(new RegExp(THIN_SPACE, "g"), "");
  const m = /^(-?)(\d+)(?:\.(\d+))?$/.exec(text);
  if (!m) throw new MoneyError(`not a decimal string: ${JSON.stringify(input)}`);
  const [, sign, whole, fracRaw = ""] = m;
  if (fracRaw.length > 2) {
    // A price at three decimals is legal, money at three decimals is not: it means someone multiplied a
    // price by a size and called the result money. Round nothing here — that is the caller's decision.
    throw new MoneyError(`${text} has more than 2 decimals; size x price is not money, use priceToUnits`);
  }
  const frac = (fracRaw + "00").slice(0, 2);
  const value = Number(whole) * 100 + Number(frac);
  return cents(sign === "-" ? -value : value);
}

export function addCents(...values: Cents[]): Cents {
  return values.reduce<number>((acc, v) => acc + v, 0) as Cents;
}

/** How many digits a tick implies: 0.001 -> 3, 0.01 -> 2. Both occur inside one event today. */
export function tickToDecimals(tick: TickSize | number): number {
  const text = typeof tick === "number" ? tick.toFixed(6) : tick;
  if (!/^\d*\.?\d+$/.test(text)) throw new MoneyError(`unusable tick ${JSON.stringify(String(tick))}`);
  const frac = text.split(".")[1] ?? "";
  const digits = frac.replace(/0+$/, "").length;
  if (digits > 6) throw new MoneyError(`tick ${text} needs more than 6 decimals`);
  return digits;
}

/**
 * A price is an integer count of tick units, not a float. `priceToUnits("0.425", "0.001")` is 425, and the
 * inverse renders exactly 0.425 — no float ever participates.
 */
export function priceToUnits(price: string, tick: TickSize): number {
  const dec = tickToDecimals(tick);
  const m = /^(-?)(\d+)(?:\.(\d+))?$/.exec(price.trim());
  if (!m) throw new MoneyError(`not a price string: ${JSON.stringify(price)}`);
  const [, sign, whole, fracRaw = ""] = m;
  if (fracRaw.length > dec) {
    throw new MoneyError(`${price} is finer than the tick (${dec}dp): the order would be off-tick`);
  }
  const frac = (fracRaw + "0".repeat(dec)).slice(0, dec);
  const units = Number(whole) * 10 ** dec + Number(frac);
  return sign === "-" ? -units : units;
}

export function unitsToPrice(units: number, tick: TickSize): string {
  const dec = tickToDecimals(tick);
  const v = integerUnits("price units", units);
  const sign = v < 0 ? "-" : "";
  const abs = Math.abs(v);
  const scale = 10 ** dec;
  const whole = Math.floor(abs / scale);
  const frac = String(abs % scale).padStart(dec, "0");
  return dec === 0 ? `${sign}${whole}` : `${sign}${whole}.${frac}`;
}

/** Group with U+2009. A comma is a locale's opinion; a thin space is a typographic decision. */
export function groupThousands(intText: string): string {
  const negative = intText.startsWith("-");
  const digits = negative ? intText.slice(1) : intText;
  const grouped = digits.replace(/\B(?=(\d{3})+(?!\d))/g, THIN_SPACE);
  return (negative ? "-" : "") + grouped;
}

export type FormatOptions = {
  /** Decimals for the rendered number. Required for prices, defaulted to 2 for money. */
  decimals?: number;
  /** Prefixes the value with an explicit sign even when positive. */
  signed?: boolean;
  /** Render dollars as whole units (default true for `formatCents`). */
  asDollars?: boolean;
  currency?: string | null;
};

/**
 * Every unit the number layer renders is an integer of something: cents, tick units, whole sizes, per-mille.
 * `Math.trunc` was the previous answer here and it is not one: truncating `NaN` yields `NaN`, which then
 * rendered as the string "NaN" in a table cell — a value that looks like data. This throws, `Number` renders
 * "—" with the reason in its `title`, and the caller has to fix the scaling.
 */
export function integerUnits(what: string, value: number): number {
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new MoneyError(`${what} must be an integer of display units, got ${typeof value} ${String(value)}`);
  }
  return value;
}

/** Integer cents -> "1 234.56". The only place rounding of money happens, and it rounds half-up on the
 *  integer remainder, which for cent-scale integers is exact. */
export function formatCents(value: Cents, opts: FormatOptions = {}): string {
  const asDollars = opts.asDollars ?? true;
  const decimals = opts.decimals ?? (asDollars ? 2 : 0);
  if (decimals < 0 || decimals > 6) throw new MoneyError("decimals must be 0..6");
  const scaled = asDollars ? value : value * 10 ** decimals;
  const negative = scaled < 0;
  const abs = Math.abs(scaled);
  let whole: number;
  let fracText = "";
  if (decimals === 0) {
    whole = abs;
  } else {
    const scale = 10 ** decimals;
    // No `| 0`, no `<<`, no bitwise anything here. This arithmetic is safe-integer arithmetic, and a single
    // int32 coercion in the formatter is enough to turn a $30M balance into a negative number — which is
    // what this line used to do (see docs/P08-frontend-shell.md §2.8). `cents()` already refused values above
    // MAX_SAFE_INTEGER/100; the formatter is where the number actually met a bit-width, and it does not any
    // more.
    const rounded = Math.round(abs);
    whole = Math.floor(rounded / scale);
    fracText = String(rounded % scale).padStart(decimals, "0");
  }
  const sign = negative ? "-" : opts.signed ? "+" : "";
  const body = fracText ? `${groupThousands(String(whole))}.${fracText}` : groupThousands(String(whole));
  return `${sign}${opts.currency ? opts.currency + " " : ""}${body}`;
}

export type NumberInput = {
  value: Cents | number;
  kind: "money" | "price" | "size" | "pnl" | "percent" | "count";
  tick?: TickSize;
  priceUnits?: number;
  compact?: boolean;
  signed?: boolean;
};

/** SI suffixes for sizes and counts only. Never for money: a truncated dollar amount is a lie about a
 *  balance, and balances are the one number a user checks before sending funds. */
export function withSiSuffix(units: number, decimals: number): string {
  const abs = Math.abs(units);
  const steps: [number, string][] = [
    [1_000_000_000, "B"],
    [1_000_000, "M"],
    [1_000, "k"],
  ];
  const sign = units < 0 ? "-" : "";
  for (const [scale, suffix] of steps) {
    if (abs >= scale) {
      const scaled = abs / scale;
      const digits = 10 ** decimals;
      const rounded = Math.round(scaled * digits) / digits;
      const [whole, frac = ""] = rounded.toFixed(decimals).split(".");
      return `${sign}${whole}${frac && frac.replace(/0+$/, "") ? "." + frac.replace(/0+$/, "") : ""}${suffix}`;
    }
  }
  return `${sign}${groupThousands(String(Math.round(abs)))}`;
}

/** The single entry point the number layer calls. */
export function formatNumber(input: NumberInput): string {
  switch (input.kind) {
    case "money":
      return formatCents(cents(input.value), { signed: input.signed });
    case "price": {
      if (typeof input.priceUnits !== "number") throw new MoneyError("price needs priceUnits");
      const text = unitsToPrice(input.priceUnits, input.tick ?? "0.01");
      return input.compact ? text.replace(/^0\./, ".") : text;
    }
    case "size":
      return withSiSuffix(integerUnits("size", input.value), 1);
    case "count":
      return groupThousands(String(integerUnits("count", input.value)));
    case "pnl":
      // The sign is not a prop here: a PnL that is positive *is* signed, and `formatCents` owns that, so
      // there is no `signed === false` branch to write. (There used was one, and both of its arms returned
      // "" — a conditional that decides nothing is worse than none, because it reads like a rule.)
      return formatCents(cents(input.value), { signed: true });
    case "percent": {
      // Per-mille integers: 12_345 means 12.345%. No float multiplication, and no truncation of a float that
      // arrived mis-scaled — that is the same "the number looks fine" failure the rest of this file refuses.
      const v = integerUnits("percent", input.value);
      const sign = v < 0 ? "-" : "";
      const abs = Math.abs(v);
      const whole = Math.floor(abs / 1000);
      const frac = String(abs % 1000).padStart(3, "0").replace(/0+$/, "");
      return `${sign}${whole}${frac ? "." + frac : ""}%`;
    }
  }
}
