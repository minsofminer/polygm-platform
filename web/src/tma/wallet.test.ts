/**
 * The wallet screen's model, against the payloads the API really sends.
 *
 * Three kinds of assertion here, and each has a reason:
 *
 *  1. **Money formatting, against Python's own vectors.** `usdcText` mirrors `fmt_usdc`, so the test vectors are that
 *     function's outputs, copied rather than invented ("5", "1290", "80.645161"). A client that rounds or groups
 *     differently from the chat is a client whose numbers a customer has to re-check before typing them back.
 *  2. **The progress mapping, from the four states the route really produces** — `credited`, an active leg, a
 *     `stopped` deposit, and the initial `detecting` — because the one thing this screen must never do is show five
 *     green legs for a transfer that stopped (that is exactly what the route itself used to do).
 *  3. **The ceremony's own ladder**, refusal code by refusal code, so a refusal lands on the step that caused it.
 */
import { describe, expect, it } from "vitest";
import {
  ceremonyBody, ceremonyFindings, ceremonyKey, ceremonyStart, depositQrPayload, depositSentence, exportFindings,
  exportReceiptText, exportStart, hoursText, mapDepositProgress, mapDestinations, mapEntries, mapWalletCard,
  plainRefusal, stepForRefusal, usdcText, EXPORT_CONFIRM_WORD,
} from "@/tma/wallet";

/**
 * A list element the test has just asserted the length of.
 *
 * The project compiles with `noUncheckedIndexedAccess`, which is the right default and shows up in tests as eleven
 * "possibly undefined" errors on lines whose whole purpose is to read one element. Naming the read once is clearer
 * than eleven non-null assertions, and it fails with the index rather than with `undefined is not an object`.
 */
function at<T>(list: readonly T[], index: number): T {
  const value = list[index];
  if (value === undefined) throw new Error(`the test expected an element at ${index}, and the list has ${list.length}`);
  return value;
}

// The card the API returns, verbatim in shape: `wallet`, `cashMicro`, `reservedMicro`, `locks`, `chains`, `note`.
const cardPayload = {
  wallet: { userId: "u1", provider: "turnkey", custody: "delegated", address: "0x1111111111111111111111111111111111111111",
            proxyAddress: "0x2222222222222222222222222222222222222222", state: "funded", policyHash: "abc", policyGap: 0,
            createdMs: 1_700_000_000_000 },
  cashMicro: "1290000000", reservedMicro: "50000000",
  locks: { password: true, totp: true, custody: "delegated" },
  chains: [{ chain: "polygon", confirmations: 128 }],
  note: "Deposits are credited after the bridge lands; the address below is yours alone.",
};

describe("money formatting matches the chat", () => {
  it("prints the same strings Python's fmt_usdc prints", () => {
    // Copied from `polygm_core.money.cents.fmt_usdc`, not from what looks right.
    const vectors: [number | string, string][] = [
      [0, "0"], [5_000_000, "5"], [1_290_000_000, "1290"], [100_000_000_000, "100000"],
      [80_645_161, "80.645161"], [500_000, "0.5"], [-25_000_000, "-25"], ["1000000", "1"],
    ];
    for (const [micro, text] of vectors) expect(usdcText(micro), String(micro)).toBe(text);
  });

  it("never shows a thousands separator, because a comma next to money is a wrong number", () => {
    expect(usdcText(1_000_000_000_000)).not.toContain(",");
    expect(usdcText(1_000_000_000_000)).toBe("1000000");
  });

  it("answers something safe for a value that is not a number at all", () => {
    expect(usdcText("not-a-number")).toBe("0");
    expect(usdcText("")).toBe("0");
  });
});

describe("the balance card", () => {
  it("maps the API's payload, locks and all", () => {
    const card = mapWalletCard(cardPayload);
    expect(card.present).toBe(true);
    expect(card.address).toBe("0x1111111111111111111111111111111111111111");
    expect(card.availableText).toBe("1290");
    expect(card.reservedText).toBe("50");
    expect(card.locks).toEqual({ password: true, totp: true });
    expect(card.custodyText).toContain("signed for you");
    expect(card.lockText).toContain("password and your authenticator");
  });

  it("names both locks when one is missing, because one of two is the state people misread", () => {
    const card = mapWalletCard({ ...cardPayload, locks: { password: false, totp: true } });
    expect(card.lockText).toContain("a password");
    expect(card.lockText).not.toContain("and an authenticator");
    const neither = mapWalletCard({ ...cardPayload, locks: { password: false, totp: false } });
    expect(neither.lockText).toContain("a password and an authenticator");
  });

  it("says there is no wallet rather than rendering an empty one", () => {
    const card = mapWalletCard({ wallet: null, cashMicro: "0", reservedMicro: "0", locks: {}, note: "No wallet yet" });
    expect(card.present).toBe(false);
    expect(card.address).toBe("");
    expect(card.availableText).toBe("0");
    expect(card.note).toContain("No wallet");
  });
});

describe("the ledger", () => {
  it("signs the deltas and keeps the reason the row was written with", () => {
    const { entries, nextBeforeMs } = mapEntries({
      entries: [
        { kind: "deposit", deltaMicro: "100000000", atMs: 1, reason: "Deposit 100 USDC credited", ref: "deposits:7" },
        { kind: "buy_fill", deltaMicro: "-80000000", atMs: 2, reason: "Buy YES 80 shares", ref: "fills:9" },
      ],
      nextBeforeMs: 2,
      note: "Every row here is money that moved.",
    });
    expect(at(entries, 0).amountText).toBe("+100");
    expect(at(entries, 0).label).toBe("Deposit");
    expect(at(entries, 1).amountText).toBe("−80");
    expect(at(entries, 1).positive).toBe(false);
    expect(at(entries, 1).reason).toBe("Buy YES 80 shares");
    expect(nextBeforeMs).toBe(2);
  });

  it("falls back to the raw kind rather than to an empty label", () => {
    const { entries } = mapEntries({ entries: [{ kind: "merge_receipt", deltaMicro: "1000000", atMs: 1, reason: "" }] });
    expect(at(entries, 0).label).toBe("Merged position");
    const { entries: other } = mapEntries({ entries: [{ kind: "some_new_kind", deltaMicro: "1000000", atMs: 1, reason: "" }] });
    expect(at(other, 0).label).toBe("some new kind");
  });
});

describe("the deposit progress screen", () => {
  const steps = (states: string[]) =>
    ["detecting", "confirming", "bridging", "crediting", "credited"].map((key, i) => ({
      key, label: `leg ${key}`, state: states[i], doneMs: 0,
    }));

  it("reports a credited deposit as done, with the balance line", () => {
    const view = mapDepositProgress({ depositId: 4, chain: "polygon", status: "credited", confirmations: 128,
                                      minConfirmations: 128, steps: steps(["done", "done", "done", "done", "done"]) });
    expect(view.credited).toBe(true);
    expect(view.line).toContain("Credited");
    expect(view.steps.every((s) => s.state === "done")).toBe(true);
  });

  it("says what the active leg is waiting for", () => {
    const view = mapDepositProgress({ depositId: 4, chain: "polygon", status: "confirming", confirmations: 3,
                                      minConfirmations: 128,
                                      steps: steps(["done", "active", "pending", "pending", "pending"]) });
    expect(view.line).toBe("leg confirming");
    expect(view.credited).toBe(false);
  });

  it("never paints a stopped transfer as a finished one", () => {
    // The route used to do exactly this: `stuck` fell through to "all legs done", so the screen showed a completed
    // deposit. Its own fix is what this asserts against — a stopped leg, and the reason, not a green tick.
    const view = mapDepositProgress({ depositId: 4, chain: "polygon", status: "stuck", confirmations: 2,
                                      minConfirmations: 128, problem: "the transfer stopped and a human has the case",
                                      steps: steps(["pending", "pending", "pending", "pending", "stopped"]) });
    expect(view.steps.filter((s) => s.state === "done")).toHaveLength(0);
    expect(at(view.steps, view.steps.length - 1).state).toBe("stopped");
    expect(view.line).toContain("human");
  });

  it("counts confirmations without animating anything", () => {
    const view = mapDepositProgress({ depositId: 4, chain: "polygon", status: "confirming", confirmations: 7,
                                      minConfirmations: 128, steps: steps(["done", "active", "pending", "pending", "pending"]) });
    expect(view.confirmations).toBe(7);
    expect(view.minConfirmations).toBe(128);
  });
});

describe("the deposit QR payload", () => {
  it("is a bare address when no amount is known, because that is what a scanner can always use", () => {
    expect(depositQrPayload("polygon", "0xabc")).toBe("0xabc");
    expect(depositQrPayload("polygon", "0xabc", "0")).toBe("0xabc");
    expect(depositQrPayload("nonsense", "0xabc", "25")).toBe("0xabc");
  });

  it("is EIP-681 with the destination chain's own id once there is an amount", () => {
    // 137 for Polygon, 8453 for Base: a scanner that ignored the chain id could send a Base deposit to an address
    // that only exists on Ethereum.
    expect(depositQrPayload("polygon", "0xabc", "250")).toBe("ethereum:0xabc@137?value=250000000");
    expect(depositQrPayload("base", "0xabc", "1.5")).toBe("ethereum:0xabc@8453?value=1500000");
  });

  it("promises a target, not a requirement", () => {
    const sentence = depositSentence("polygon", 250_000_000);
    expect(sentence).toContain("Polygon");
    expect(sentence).toContain("250");
    expect(sentence).toContain("not a fixed requirement");
  });
});

describe("the withdrawal ceremony", () => {
  const ready = { availableMicro: 129_000_000, locks: { password: true, totp: true } };

  it("walks the server's ladder, in the server's order", () => {
    let ceremony = ceremonyStart();
    expect(ceremony.step).toBe("amount");
    ceremony = { ...ceremony, amountUsdc: "25", step: "destination" };
    // Nothing chosen yet, so the destination step says so rather than letting the user through to type it back.
    expect(ceremonyFindings(ceremony, ready)).toEqual(["Pick a destination from your allowlist."]);
    ceremony = { ...ceremony, addressId: "a1", step: "typed" };
    expect(ceremonyFindings(ceremony, ready)).toEqual(["The amount you typed does not match the amount above.",
                                                       "Type the destination as it was shown."]);
    ceremony = { ...ceremony, typedAmount: "25", typedAddress: "0xabc", step: "password" };
    expect(ceremonyFindings(ceremony, ready)).toEqual(["Enter your password."]);
    ceremony = { ...ceremony, password: "hunter2", step: "code" };
    expect(ceremonyFindings(ceremony, ready)).toEqual(["Enter the 6-digit code from your authenticator app."]);
    ceremony = { ...ceremony, code: "123456" };
    expect(ceremonyFindings(ceremony, ready)).toHaveLength(0);
  });

  it("refuses an amount above what is available, and names the number", () => {
    const ceremony = { ...ceremonyStart(), amountUsdc: "1290", step: "amount" as const };
    const findings = ceremonyFindings(ceremony, ready);
    expect(findings[0]).toContain("129");
    expect(findings[0]).toContain("reserved against open orders");
  });

  it("says the lock is missing rather than asking for a password that cannot exist", () => {
    const ceremony = { ...ceremonyStart(), password: "", step: "password" as const };
    expect(ceremonyFindings(ceremony, { availableMicro: 1_000_000, locks: { password: false, totp: true } })[0])
      .toContain("set one in the bot");
    const code = { ...ceremonyStart(), step: "code" as const, code: "123456" };
    expect(ceremonyFindings(code, { availableMicro: 1_000_000, locks: { password: true, totp: false } })[0])
      .toContain("set it up in the bot");
  });

  it("sends the typed pair as typed, and the amount as a string", () => {
    const ceremony = { ...ceremonyStart(), amountUsdc: " 25 ", addressId: "a1", typedAmount: "25",
                       typedAddress: " 0xabc ", password: "pw", code: " 123456 " };
    const body = ceremonyBody(ceremony);
    expect(body.amountUsdc).toBe("25");
    expect(body.typedAddress).toBe("0xabc");
    expect(body.code).toBe("123456");
    expect(typeof body.amountUsdc).toBe("string");
  });

  it("mints a key the server will accept, once per ceremony", () => {
    const key = ceremonyKey(() => "0123456789abcdef");
    expect(key).toBe("tma-wd-0123456789abcdef");
    // The server's own regex (`_IDEM_RE`): a key outside it is refused 422 before the handler runs.
    expect(key).toMatch(/^[A-Za-z0-9_-]{8,128}$/);
  });

  it("puts each refusal on the step that caused it", () => {
    expect(stepForRefusal("ADDRESS_COOLDOWN")).toBe("destination");
    expect(stepForRefusal("PASSWORD_WRONG")).toBe("password");
    expect(stepForRefusal("TOTP_INVALID")).toBe("code");
    expect(stepForRefusal("INSUFFICIENT_BALANCE")).toBe("amount");
    expect(stepForRefusal("BAD_FIELD")).toBe("typed");
    expect(stepForRefusal("SOMETHING_NEW")).toBeNull();
  });

  it("has a sentence for every code the ceremony can be refused with", () => {
    for (const code of ["ADDRESS_COOLDOWN", "ADDRESS_NOT_ALLOWED", "PASSWORD_REQUIRED", "PASSWORD_WRONG",
                        "TOTP_REQUIRED", "TOTP_INVALID", "TOTP_LOCKED", "INSUFFICIENT_BALANCE"]) {
      const text = plainRefusal(code);
      expect(text, code).not.toContain("did not go through");
      expect(text.length, code).toBeGreaterThan(30);
    }
  });

  it("lets the server's own sentence through when the code is a 422 the table deliberately does not carry", () => {
    // `BAD_FIELD` is the route's "one of the fields you read back does not match", and its detail names which one.
    // A generic table entry would be *worse* than the fallback here: the fallback prints what the server said.
    const text = plainRefusal("BAD_FIELD", "type the amount exactly as shown, digits and all");
    expect(text).toBe("type the amount exactly as shown, digits and all");
  });

  it("reports a destination's hold in hours and days, never as a countdown", () => {
    expect(hoursText(3 * 3_600_000)).toBe("3 hours");
    expect(hoursText(90 * 60_000)).toBe("2 hours");
    expect(hoursText(50 * 3_600_000)).toBe("2 days");
    expect(hoursText(20 * 60_000)).toBe("20 minutes");
  });

  it("maps the allowlist as the route sends it — masked, and never with the full destination", () => {
    const first = at(mapDestinations({ items: [{ id: "a1", label: "Cold wallet", display: "0x1234…abcd", usable: false,
                                                cooldown_remaining_ms: 7_200_000 }], cooldownMs: 86_400_000 }), 0);
    expect(first.display).toBe("0x1234…abcd");
    expect(first.usable).toBe(false);
    expect(first.cooldownText).toBe("usable in 2 hours");
    // The route strips `address` before it answers; a screen that had the full string would offer to copy it.
    expect(JSON.stringify(mapDestinations({ items: [{ id: "a1", display: "0x1234…abcd" }] }))).not.toContain("address\"");
  });
});

describe("the key export", () => {
  it("will not let the form be submitted without the typed word, the password and a code", () => {
    expect(exportFindings(exportStart(), true)).toEqual([
      "Type EXPORT (in capitals) to confirm you understand what this is.",
      "Enter your password.",
      "Enter the 6-digit code from your authenticator app.",
    ]);
    expect(exportFindings({ typedConfirm: EXPORT_CONFIRM_WORD, password: "pw", code: "123456" }, true)).toHaveLength(0);
    expect(exportFindings({ typedConfirm: "export", password: "pw", code: "123456" }, true)[0]).toContain("EXPORT");
  });

  it("says what was used, in the words the receipt needs", () => {
    expect(exportReceiptText({ dekVersion: 3, wrapsUsed: 2 })).toContain("Key version 3");
    expect(exportReceiptText({ dekVersion: 3, wrapsUsed: 1 })).toContain("1 wrap used");
  });
});
