"use client";
import { useState } from "react";
import { invalidate, useAction, useResource } from "@/api/data";
import { request } from "@/api/client";
import { Button } from "@/ui/Button";
import { Field, MoneyField } from "@/ui/Field";
import { Number as NumberView } from "@/num/Number";
import { centsFromDecimal } from "@/money/cents";
import { pushRefusal } from "@/ui/Toast";
import { t } from "@/i18n/t";

/**
 * The one money-adjacent surface P08 can wire end to end, because the API has it. Three behaviours that are
 * easy to get wrong and are therefore the point of the screen:
 *   - the list shows a prefix and a label, never the whole address, and *says why* (a reveal is a separate,
 *     deliberate act — P07's D1 threat of clipboard substitution);
 *   - a removal needs a second factor and the screen asks *before* sending, because the API's 403 carries a
 *     reason we would rather the user read before they type;
 *   - the cooldown is the API's number, in hours, not a string we invented.
 */
/**
 * Milliseconds arrive from the API as a number or a numeric string; a duration rendered through `Number`
 * must be a whole integer of minutes. `Number(undefined)` is NaN and NaN renders as the word "NaN", which is
 * the one output this component may not produce, so the coercion is checked instead of assumed.
 */
function wholeMinutes(ms: unknown): number | null {
  const n = typeof ms === "number" ? ms : typeof ms === "string" && /^\d+$/.test(ms) ? Number(ms) : NaN;
  if (!Number.isFinite(n) || n <= 0) return null;
  return Math.floor(n / 60_000);
}

function minutesUntil(untilMs: unknown, nowMs: number): number | null {
  const left = wholeMinutes(untilMs);
  if (left === null) return null;
  const minutes = Math.floor((left * 60_000 - nowMs) / 60_000);
  return minutes > 0 ? minutes : null;
}

export function WalletAddresses() {
  const [code, setCode] = useState("");
  const [address, setAddress] = useState("");
  const [pendingRemoval, setPendingRemoval] = useState<string | null>(null);
  const read = useResource(["wallet", "addresses"], async () => {
    const out = await request<{ items?: Record<string, unknown>[]; cooldownMs?: number; reveal?: string }>({ key: "addressList" });
    if (!out.ok) throw new Error(`${out.error.code}: ${out.error.message}`);
    return out;
  });

  // Same three behaviours the mutation had — the refusal pushed with its code, the cooldown prompt cleared on a
  // TOTP answer, the list invalidated on success — expressed as one function, because that is what they are: the
  // order of the steps is the point, and two callbacks that can each throw hide it.
  const remove = useAction(async (id: string) => {
    try {
      const out = await request({ key: "addressRemove", body: { address_id: id, totp_code: code } });
      if (!out.ok) throw new Error(`${out.error.code}|${out.error.message}`);
      setPendingRemoval(null);
      setCode("");
      invalidate(["wallet", "addresses"]);
      return out;
    } catch (error) {
      const [code_, message] = (error as Error).message.split("|");
      pushRefusal(code_ ?? "INTERNAL", message ?? (error as Error).message, "");
      if (code_ === "TOTP_REQUIRED" || code_ === "TOTP_INVALID") setPendingRemoval(null);
      throw error;                        // kept on `remove.error` too: the screen shows what it can see
    }
  });

  const data = read.data?.data ?? {};
  const cooldownHours = wholeMinutes(data.cooldownMs);

  return (
    <section style={{ display: "grid", gap: "var(--pgm-space-2)" }} aria-label={t("wallet.addresses.title")}>
      <h2>{t("wallet.addresses.title")}</h2>
      <p className="refusal">{data.reveal ?? t("wallet.addresses.noReveal")}</p>
      {read.isPending ? <p role="status">{t("common.state.loading")}</p> : null}
      {read.isError ? <p role="alert" className="refusal">{String(read.error.message)}</p> : null}
      <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: "var(--pgm-space-1)" }}>
        {(data.items ?? []).map((row) => (
          <li key={String(row.id)}>
            <span>{String(row.label ?? "address")} </span>
            <span>{String(row.address_prefix ?? row.masked ?? "—")}</span>{" "}
            {minutesUntil(row.cooldown_until_ms, Date.now()) !== null ? (
              <NumberView kind="count" value={minutesUntil(row.cooldown_until_ms, Date.now()) ?? 0} label={t("wallet.addresses.cooldownMinutes")} />
            ) : null}
            <Button onClick={() => setPendingRemoval(String(row.id))}>{t("wallet.addresses.remove")}</Button>
            {pendingRemoval === String(row.id) ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  remove.mutate(String(row.id));
                }}
                style={{ display: "grid", gap: "var(--pgm-space-1)" }}
              >
                <Field label={t("auth.factor.label")} name="code" value={code} onChange={setCode} inputMode="numeric" required help={t("auth.factor.required")} />
                <Button type="submit" variant="danger" pending={remove.isPending}>
                  {t("wallet.addresses.remove")}
                </Button>
              </form>
            ) : null}
          </li>
        ))}
      </ul>
      {cooldownHours !== null ? <p className="refusal">{t("wallet.withdraw.cooldown", { hours: cooldownHours })}</p> : null}
    </section>
  );
}
