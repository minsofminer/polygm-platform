/**
 * The withdrawal ceremony's rules, stated where they can be checked without a browser.
 *
 * The browser suite (`e2e/wallet-ceremony.spec.ts`) proves the *order* — that the primary control is
 * unreachable until the step above it is satisfied, and that a typed value which differs from what was shown is
 * refused. These are the same rules as arithmetic, so a failure here points at the rule rather than at a
 * selector.
 */
import { describe, expect, it } from "vitest";
import { amountRule, balanceCard, codeRule, entriesFrom, matchesShown, usable, withdrawBody } from "./withdraw-logic";

const card = balanceCard({ cashMicro: "25000000", reservedMicro: "5000000", locks: { password: true, totp: true, custody: "managed" } });

describe("what the ledger says is available", () => {
  it("is cash minus what is reserved, and never negative", () => {
    expect(card.usableMicro).toBe("20000000");
    expect(balanceCard({ cashMicro: "100", reservedMicro: "900" }).usableMicro).toBe("0");
  });

  it("reads a missing or malformed balance as zero rather than as NaN", () => {
    // NaN renders as the word "NaN" on a screen about money, which is the one output this layer may not produce.
    expect(balanceCard(null).usableMicro).toBe("0");
    expect(balanceCard({ cashMicro: "not a number" }).usableMicro).toBe("0");
  });

  it("carries the two locks the ceremony needs", () => {
    expect(card.locks).toEqual({ password: true, totp: true, custody: "managed" });
    expect(balanceCard({}).locks).toEqual({ password: false, totp: false, custody: "unknown" });
  });
});

describe("step one: the amount", () => {
  it("accepts an amount inside the ledger and refuses one above it, in cents", () => {
    expect(amountRule("12.50", card).ok).toBe(true);
    expect(amountRule("200.00", card)).toEqual({ ok: false, why: "amount.aboveLedger" });
    // Exactly the ceiling is allowed: "up to what is available" is what the sentence says.
    // 10^6 micro to the dollar, so $200.00 is 200000000 micro — the unit is the whole reason this rule is
    // arithmetic in one module instead of comparisons spread over a component.
    expect(amountRule("200.00", balanceCard({ cashMicro: "200000000", reservedMicro: "0" })).ok).toBe(true);
  });

  it("refuses zero, a negative and an unparsable amount without calling any of them zero", () => {
    expect(amountRule("0", card)).toEqual({ ok: false, why: "amount.zero" });
    expect(amountRule("-1.00", card).ok).toBe(false);
    const shape = amountRule("12.5.0", card);
    expect(shape.ok).toBe(false);
    if (!shape.ok) expect(shape.why).not.toBe("amount.zero");
  });

  it("refuses everything while the balance has not been read", () => {
    // A ceiling that is not known is not a ceiling: the screen says so rather than comparing against zero.
    expect(amountRule("1.00", null)).toEqual({ ok: false, why: "amount.unknownBalance" });
  });
});

describe("the typed confirmation", () => {
  it("treats 12.5 and 12.50 as the same amount, and 12.51 as a different one", () => {
    expect(matchesShown("amount", "12.5", "12.50")).toBe(true);
    expect(matchesShown("amount", "12.51", "12.50")).toBe(false);
  });

  it("is character-exact for an address: a near-miss is a different destination", () => {
    const shown = "0x1111111111111111111111111111111111111111";
    expect(matchesShown("address", shown, shown)).toBe(true);
    expect(matchesShown("address", `${shown.slice(0, -1)}2`, shown)).toBe(false);
    expect(matchesShown("address", ` ${shown} `, shown)).toBe(true);   // whitespace is trimmed, not significant
    expect(matchesShown("address", "0x1", "")).toBe(false);            // nothing shown means nothing to match
  });
});

describe("the allowlist, as the API serves it", () => {
  it("takes an address in full where the owner is typing it, and the short form otherwise", () => {
    const [first] = entriesFrom({
      items: [{ id: 3, address: "0xabc", label: "cold", cooldownRemainingMs: 0 }, { id: 4, prefix: "0xdef…123", label: "hot" }],
    });
    expect(first).toEqual({ id: "3", shown: "0xabc", label: "cold", cooldownRemainingMs: 0 });
    expect(entriesFrom({ items: [{ id: 4, prefix: "0xdef…123" }] })[0]?.shown).toBe("0xdef…123");
  });

  it("refuses a destination that has not finished its hold, and one with nothing to type back", () => {
    expect(usable({ id: "1", shown: "0xabc", label: "", cooldownRemainingMs: 0 })).toBe(true);
    expect(usable({ id: "1", shown: "0xabc", label: "", cooldownRemainingMs: 60_000 })).toBe(false);
    expect(usable({ id: "1", shown: "", label: "", cooldownRemainingMs: 0 })).toBe(false);
  });

  it("survives a payload with no list at all", () => {
    expect(entriesFrom({})).toEqual([]);
    expect(entriesFrom(null)).toEqual([]);
  });
});

describe("the authenticator code", () => {
  it("takes six to ten digits — the contract's own bounds — and nothing else", () => {
    expect(codeRule("123456").ok).toBe(true);
    expect(codeRule(" 1234567 ").ok).toBe(true);
    expect(codeRule("12345")).toEqual({ ok: false, why: "code.shape" });
    expect(codeRule("12345a")).toEqual({ ok: false, why: "code.shape" });
    expect(codeRule("")).toEqual({ ok: false, why: "code.required" });
  });
});

describe("the body that goes on the wire", () => {
  it("is the contract's six fields, with the amount re-emitted from the parsed integer", () => {
    const body = withdrawBody({
      amount: "12.5",
      addressId: "3",
      typedAmount: "12.50",
      typedAddress: "0xabc",
      password: "hunter2",
      code: "123456",
    });
    // `12.5` in the field must not become a second string with the same meaning: the contract's pattern is
    // `^[0-9]{1,9}(\.[0-9]{1,2})?$` and both spellings name the same amount.
    expect(body.amountUsdc).toBe("12.50");
    expect(Object.keys(body).sort()).toEqual(["addressId", "amountUsdc", "code", "password", "typedAddress", "typedAmount"]);
  });
});
