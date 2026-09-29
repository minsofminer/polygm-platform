/**
 * The withdrawal ceremony's arithmetic, in its own module.
 *
 * Two reasons it is not inline in `Withdraw.tsx`. The first is this repository's rule about the client
 * boundary (`src/client-boundary.test.ts`): a plain function exported from a `"use client"` module is a client
 * reference, and a server importer of one is a 500. The second is better — these are the rules the ceremony
 * exists for. "Is this amount above the ledger?", "does what was typed equal what was shown?", "which
 * destinations may receive funds at all?" are decisions with a right answer, and a decision with a right answer
 * belongs somewhere a test can state it without a browser.
 *
 * The screen is then only wiring: which field is enabled, and which sentence is rendered when a rule says no.
 */
import { centsFromDecimal, microToCents, type Cents } from "@/money/cents";

/** A destination as the allowlist serves it: short-form, with the hold that is still running on it. */
export type Destination = {
  id: string;
  /** What the owner must type back. The API serves short-form here and full-form where it is typed (contract). */
  shown: string;
  label: string;
  /** Milliseconds of the 24-hour hold left; 0 or absent means usable now. */
  cooldownRemainingMs: number;
};

/** The balance card's money, from `/v1/wallet/balance`. Micro integers as strings, which is what the wire says. */
export type BalanceCard = { cashMicro: string; reservedMicro: string; usableMicro: string; locks: Locks };
export type Locks = { password: boolean; totp: boolean; custody: string };

export type Rule = { ok: true } | { ok: false; why: string };

const microOf = (value: unknown): number => {
  if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value);
  if (typeof value === "string" && /^\d+$/.test(value)) return Number(value);
  return 0;
};

/**
 * Micro is the ledger's unit and cents is the screen's, so the comparison happens in cents (the money layer's
 * own type) and never in floats: `NaN` from an unparsable field is a refusal, not a zero.
 */
export function balanceCard(card: Record<string, unknown> | null | undefined): BalanceCard {
  const cash = microOf(card?.cashMicro);
  const reserved = microOf(card?.reservedMicro);
  const locks = (card?.locks ?? {}) as Record<string, unknown>;
  return {
    cashMicro: String(cash),
    reservedMicro: String(reserved),
    usableMicro: String(Math.max(0, cash - reserved)),
    locks: { password: locks.password === true, totp: locks.totp === true, custody: String(locks.custody ?? "unknown") },
  };
}

export function entriesFrom(payload: Record<string, unknown> | null | undefined): Destination[] {
  const items = Array.isArray(payload?.items) ? (payload.items as Record<string, unknown>[]) : [];
  return items.map((row, index) => {
    const id = String(row.id ?? row.addressId ?? index);
    return {
      id,
      // The full value where the owner is typing it back, the short form otherwise — that is the contract's rule,
      // and the ladder is the only screen that may see the full one.
      shown: String(row.address ?? row.short ?? row.prefix ?? row.addressShort ?? ""),
      label: String(row.label ?? ""),
      cooldownRemainingMs: microOf(row.cooldownRemainingMs),
    };
  });
}

export function usable(destination: Destination): boolean {
  return destination.cooldownRemainingMs <= 0 && destination.shown.length > 0;
}

/** Step one: an amount that is positive, parsable, and not larger than what the ledger says is free. */
export function amountRule(input: string, card: BalanceCard | null): Rule {
  let cents: Cents;
  try {
    cents = centsFromDecimal(input);
  } catch (cause) {
    return { ok: false, why: cause instanceof Error ? cause.message : String(cause) };
  }
  if (cents <= 0) return { ok: false, why: "amount.zero" };
  if (card === null) return { ok: false, why: "amount.unknownBalance" };
  if (cents > microToCents(Number(card.usableMicro))) return { ok: false, why: "amount.aboveLedger" };
  return { ok: true };
}

/**
 * The typed confirmation. Compared as money where the field is an amount (so `12.5` and `12.50` are the same
 * number and `12.51` is not), and character-exact where it is an address — a near-miss in an address is not a
 * rounding difference, it is a different destination.
 */
export function matchesShown(what: "amount" | "address", typed: string, shown: string): boolean {
  if (what === "address") return typed.trim() === shown.trim() && shown.trim().length > 0;
  try {
    return centsFromDecimal(typed) === centsFromDecimal(shown);
  } catch {
    return false;
  }
}

/** The code field's own bound, from the contract: `minLength: 6, maxLength: 10`. */
export function codeRule(code: string): Rule {
  const trimmed = code.trim();
  if (trimmed.length === 0) return { ok: false, why: "code.required" };
  if (!/^\d{6,10}$/.test(trimmed)) return { ok: false, why: "code.shape" };
  return { ok: true };
}

/**
 * The bodies the API takes. `amountUsdc` is a decimal string of dollars (the contract's pattern), built from the
 * integer the parser accepted rather than from the field's text — `"10.0"` and `"10.00"` must not become two
 * different payloads with the same meaning.
 */
export function withdrawBody(args: {
  amount: string;
  addressId: string;
  typedAmount: string;
  typedAddress: string;
  password: string;
  code: string;
}): Record<string, string> {
  const cents = centsFromDecimal(args.amount);
  return {
    amountUsdc: (cents / 100).toFixed(2),
    addressId: args.addressId,
    typedAmount: args.typedAmount,
    typedAddress: args.typedAddress,
    password: args.password,
    code: args.code.trim(),
  };
}
