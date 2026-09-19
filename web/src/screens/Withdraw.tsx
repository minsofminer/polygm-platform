"use client";
import { useState } from "react";
import { MoneyField, Field } from "@/ui/Field";
import { Button } from "@/ui/Button";
import { RefusalNotice } from "@/ui/RefusalNotice";
import { centsFromDecimal, type Cents } from "@/money/cents";
import { formatCents } from "@/money/cents";
import { t } from "@/i18n/t";

/**
 * The withdrawal form, complete and disabled. It is not a stub: the amount is parsed by the money layer and
 * the fee estimate is a real integer subtraction, so the arithmetic that will matter on day one is already
 * under test. What is missing is the route (launch item P08-L6), and the screen says so where the submit
 * button would have been — a disabled button with a reason is a design decision; a working button that
 * quietly no-ops is a fraud.
 */
export function Withdraw() {
  const [amount, setAmount] = useState("25.00");
  const [address, setAddress] = useState("");
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const feeCents = 100;
  let parsed: Cents | null = null;
  try {
    // `centsFromDecimal` already returns branded integer cents. A `Number(...)` around it type-checks and
    // does nothing except unbrand the value, which is how a float creeps back into the money path.
    parsed = centsFromDecimal(amount);
  } catch (cause) {
    setError(cause instanceof Error ? cause.message : String(cause));
  }
  const net = parsed === null ? null : parsed - feeCents;
  return (
    <form onSubmit={(e) => e.preventDefault()} style={{ display: "grid", gap: "var(--pgm-space-2)" }}>
      <h2>{t("wallet.withdraw.title")}</h2>
      <MoneyField label={t("wallet.withdraw.amountLabel")} name="amount" value={amount} onChange={(v) => { setAmount(v); setError(null); }} error={error} />
      <Field label={t("wallet.withdraw.allowlistOnly")} name="address" value={address} onChange={setAddress} help={t("wallet.withdraw.allowlistOnly")} />
      <Field label={t("wallet.keys.typeToConfirm").replace("EXPORT", "WITHDRAW")} name="typed" value={typed} onChange={setTyped} />
      <p>
        {t("wallet.withdraw.fee")}: {formatCents(feeCents as never)} · net {net === null ? "—" : formatCents(net as never)}
      </p>
      <Button type="submit" disabled>
        {t("wallet.withdraw.submit")}
      </Button>
      <RefusalNotice route="withdraw" />
    </form>
  );
}
