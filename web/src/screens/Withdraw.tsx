"use client";
import { useState } from "react";
import { useAction, useResource } from "@/api/data";
import { request } from "@/api/client";
import { Button } from "@/ui/Button";
import { Field, MoneyField } from "@/ui/Field";
import { formatCents, microToCents } from "@/money/cents";
import { pushRefusal } from "@/ui/toast-store";
import { t } from "@/i18n/t";
import { ROUTES } from "@/api/routes";
import {
  amountRule,
  balanceCard,
  codeRule,
  entriesFrom,
  matchesShown,
  usable,
  withdrawBody,
  type BalanceCard,
  type Destination,
} from "./withdraw-logic";

/**
 * The withdrawal ceremony.
 *
 * The contract calls it "seven locks, seven refusals": an allowlisted destination, a hold that has run out, a
 * typed amount, a typed address, a password and an authenticator code — and the field that is *not* here, a raw
 * destination address, because only the allowlist may name where money goes. This screen is the client half of
 * that: it asks for the six things, and it refuses to move a step when the one above it is not satisfied.
 *
 * Where the refusals come from is split on purpose. The rules a person can see (amount above the ledger, a
 * destination still cooling, a typed value that does not match what is shown) are answered *here*, in words,
 * before a request is made — `withdraw-logic.ts` holds the arithmetic and its tests. The rules only the server
 * can know (the password, the code, the allowlist row) come back as the API's own codes, which is why every
 * failure ends in the refusal renderer rather than in a message this screen invented.
 *
 * What it does NOT do is claim the money moved. The last thing the route does is record the request; signing is
 * the custody plane's job. The success line says exactly that, because a screen that says "sent" about an
 * unsigned transfer is the lie this product is built against.
 */
type Step = 1 | 2 | 3 | 4 | 5;

export function Withdraw() {
  const [step, setStep] = useState<Step>(1);
  const [amount, setAmount] = useState("25.00");
  const [typedAmount, setTypedAmount] = useState("");
  const [typedAddress, setTypedAddress] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [destinationId, setDestinationId] = useState<string | null>(null);
  const [recorded, setRecorded] = useState<{ id: string; destination: string } | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);

  const balance = useResource(["wallet", "balance"], async () => {
    const out = await request<Record<string, unknown>>({ key: "balance" });
    if (!out.ok) throw new Error(`${out.error.code}: ${out.error.message}`);
    return balanceCard(out.data);
  });
  const addresses = useResource(["wallet", "addresses"], async () => {
    const out = await request<Record<string, unknown>>({ key: "addressList" });
    if (!out.ok) throw new Error(`${out.error.code}: ${out.error.message}`);
    return entriesFrom(out.data);
  });

  const send = useAction(async () => {
    // The body is built from the integers the parser accepted, never from the field's text: the value that goes
    // on the wire is the value the screen checked.
    const out = await request<{ withdrawalId?: number | string; status?: string; destination?: string }>({
      key: "withdraw",
      body: withdrawBody({ amount, addressId: destinationId ?? "", typedAmount, typedAddress, password, code }),
    });
    if (!out.ok) {
      // The refusal is the API's own sentence. The code goes to the toast (an operator can read it, support can
      // ask for it) and the message goes next to the button, in the place the person is already looking.
      pushRefusal(out.error.code, out.error.message, out.error.requestId);
      setRefusal(out.error.message);
      return;
    }
    setRefusal(null);
    setRecorded({ id: String(out.data.withdrawalId ?? "—"), destination: out.data.destination ?? "" });
    // The two secrets do not stay in component state after a success, and the code is single-use by construction.
    setPassword("");
    setCode("");
  });

  const card: BalanceCard | null = balance.data ?? null;
  const destinations: Destination[] = addresses.data ?? [];
  const chosen = destinations.find((d) => d.id === destinationId) ?? null;

  const amountVerdict = amountRule(amount, card);
  const availableText = card ? formatCents(microToCents(Number(card.usableMicro))) : "—";
  const amountWhy = amountVerdict.ok ? null : amountMessage(amountVerdict.why, availableText);
  const typedAmountOk = matchesShown("amount", typedAmount, amount);
  const typedAmountWrong = typedAmount.trim().length > 0 && !typedAmountOk;
  const typedAddressOk = chosen !== null && matchesShown("address", typedAddress, chosen.shown);
  const typedAddressWrong = typedAddress.trim().length > 0 && !typedAddressOk;
  const codeVerdict = codeRule(code);

  const finish = () => {
    setRefusal(null);
    send.mutate(undefined);
  };

  if (recorded) {
    return (
      <section aria-label={t("wallet.withdraw.title")} style={{ display: "grid", gap: "var(--pgm-space-2)" }}>
        <h2>{t("wallet.withdraw.title")}</h2>
        <p role="status">{t("wallet.withdraw.recorded", { id: recorded.id })}</p>
        <p className="refusal">{t("wallet.withdraw.notSigned")}</p>
      </section>
    );
  }

  return (
    <section aria-label={t("wallet.withdraw.title")} style={{ display: "grid", gap: "var(--pgm-space-2)" }}>
      <h2>{t("wallet.withdraw.title")}</h2>
      <p>
        {t("wallet.withdraw.available")}: {card ? formatCents(microToCents(Number(card.usableMicro))) : t("common.state.loading")}
      </p>
      {card && !(card.locks.password && card.locks.totp) ? (
        <p className="refusal" role="note">{t("wallet.withdraw.needLocks")}</p>
      ) : null}
      {balance.isError ? <p className="refusal" role="alert">{String(balance.error.message)}</p> : null}
      {addresses.isError ? <p className="refusal" role="alert">{String(addresses.error.message)}</p> : null}

      {/* Step 1 · the amount. The ledger's own number is the ceiling, and the refusal is a sentence, not a red box. */}
      <MoneyField
        label={t("wallet.withdraw.amountLabel")}
        name="amount"
        value={amount}
        onChange={(v) => { setAmount(v); setRefusal(null); }}
        error={amountWhy}
        help={t("wallet.withdraw.amountHelp")}
      />
      <p className="refusal">{t("wallet.withdraw.allowlistOnly")}</p>
      {step === 1 ? (
        <Button variant="primary" disabled={!amountVerdict.ok} onClick={() => setStep(2)}>
          {t("common.button.continue")}
        </Button>
      ) : null}

      {/* Step 2 · the destination, from the allowlist only, and the amount typed back. */}
      {step >= 2 ? (
        <fieldset style={{ display: "grid", gap: "var(--pgm-space-1)", border: 0, padding: 0 }}>
          <legend>{t("wallet.withdraw.choose")}</legend>
          {destinations.length === 0 ? <p role="note">{t("wallet.withdraw.noDestinations")}</p> : null}
          {destinations.map((d) => (
            <label key={d.id} className="field" style={{ display: "flex", gap: "var(--pgm-space-1)" }}>
              <input
                type="radio"
                name="destination"
                value={d.id}
                checked={destinationId === d.id}
                disabled={!usable(d)}
                onChange={() => { setDestinationId(d.id); setRefusal(null); }}
              />
              <span>
                {d.label ? `${d.label} · ` : ""}{d.shown}
                {usable(d) ? null : ` — ${t("wallet.withdraw.cooling", { minutes: String(Math.ceil(d.cooldownRemainingMs / 60_000)) })}`}
              </span>
            </label>
          ))}
          <Field
            label={t("wallet.withdraw.typeAmount")}
            name="typedAmount"
            value={typedAmount}
            onChange={(v) => setTypedAmount(v)}
            inputMode="decimal"
            help={t("wallet.withdraw.typeAmountHelp")}
          />
          {/* The mismatch is stated where it is typed, in words: the value is refused, not corrected. */}
          {typedAmountWrong ? <p role="alert" className="refusal">{t("wallet.withdraw.typeAmountMismatch")}</p> : null}
          {step === 2 ? (
            <Button
              variant="primary"
              disabled={!(typedAmountOk && chosen !== null && usable(chosen))}
              onClick={() => setStep(3)}
            >
              {t("common.button.continue")}
            </Button>
          ) : null}
        </fieldset>
      ) : null}

      {/* Step 3 · the destination typed back, character-exact, against what the screen showed. */}
      {step >= 3 ? (
        <div style={{ display: "grid", gap: "var(--pgm-space-1)" }}>
          <p>{t("wallet.withdraw.destinationShown", { address: chosen?.shown ?? "" })}</p>
          <Field
            label={t("wallet.withdraw.typeAddress")}
            name="typedAddress"
            value={typedAddress}
            onChange={(v) => setTypedAddress(v)}
            help={t("wallet.withdraw.typeAddressHelp")}
          />
          {typedAddressWrong ? <p role="alert" className="refusal">{t("wallet.withdraw.typeAddressMismatch")}</p> : null}
          {step === 3 ? (
            <Button variant="primary" disabled={!typedAddressOk} onClick={() => setStep(4)}>
              {t("common.button.continue")}
            </Button>
          ) : null}
        </div>
      ) : null}

      {/* Step 4 · the password, then step 5 · the authenticator. Two locks, asked in order, neither skippable. */}
      {step >= 4 ? (
        <div style={{ display: "grid", gap: "var(--pgm-space-1)" }}>
          <Field
            label={t("wallet.withdraw.password")}
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={setPassword}
            help={t("wallet.withdraw.passwordHelp")}
          />
          {step === 4 ? (
            <Button variant="primary" disabled={password.length === 0} onClick={() => setStep(5)}>
              {t("common.button.continue")}
            </Button>
          ) : null}
        </div>
      ) : null}

      {step >= 5 ? (
        <div style={{ display: "grid", gap: "var(--pgm-space-1)" }}>
          <Field
            label={t("wallet.withdraw.code")}
            name="code"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={code}
            onChange={setCode}
            error={code.length > 0 && !codeVerdict.ok ? t("wallet.withdraw.codeShape") : undefined}
            help={t("wallet.withdraw.codeHelp")}
          />
          <Button
            variant="primary"
            pending={send.isPending}
            disabled={!codeVerdict.ok}
            onClick={finish}
          >
            {t("wallet.withdraw.submit")}
          </Button>
          {refusal ? <p role="alert" className="refusal">{refusal}</p> : null}
        </div>
      ) : null}

      {/* What this screen cannot do, said on the screen: the route is declared, and the signing is not live. */}
      <p className="refusal" data-route={ROUTES.withdraw.path}>{t("wallet.withdraw.notSigned")}</p>
    </section>
  );
}

/**
 * The refusal catalogue, as a switch of literal keys rather than a lookup table.
 *
 * `scripts/i18n-check.mjs` fails the build on `t(<anything that is not a string literal>)`, and it is right to:
 * a key computed at runtime is a key no check can verify and a route whose dictionary may not even be in the
 * graph. A switch keeps the mapping total and readable at the same time.
 */
function amountMessage(why: string, available: string): string {
  switch (why) {
    case "amount.zero":
      return t("wallet.withdraw.amountZero");
    case "amount.unknownBalance":
      return t("wallet.withdraw.amountUnknown");
    case "amount.aboveLedger":
      return t("wallet.withdraw.amountAbove", { available });
    default:
      return t("wallet.withdraw.amountShape", { reason: why });
  }
}
