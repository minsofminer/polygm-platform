"use client";
import { useEffect, useState } from "react";
import { useConnection } from "@/shell/connection";
import { Button } from "@/ui/Button";
import { MoneyField } from "@/ui/Field";
import { centsFromDecimal } from "@/money/cents";
import { newIdempotencyKey, request } from "@/api/client";
import { pushRefusal } from "@/ui/Toast";
import { t } from "@/i18n/t";
import { hapticConfirm, showMainButton, usesMainButton } from "@/telegram/bridge";
import { announce } from "@/ui/Dialog";

/**
 * The ticket is in P08 because the *gate* is in P08: the shell is what decides whether a trade may be sent,
 * and the decision has to be made in one place. The rules it enforces, in order:
 *   1. no submission while the feed is down or too old, with the reason in words, and cancel-all still live;
 *   2. the amount never becomes a float: the field keeps a decimal string and the cents constructor does the
 *      arithmetic (a `0.07` in the money path fails the build, not the trade);
 *   3. the idempotency key is generated once per ticket-and-amount, so a double-click and a retry inside the
 *      client collapse into one intent;
 *   4. haptics fire on the confirmation only (webview) — nowhere else in the app.
 */

/** Cents (an integer) as the wire's decimal string: `1000` → `"10.00"`. */
function usdcFromCents(value: number): string {
  const whole = Math.trunc(value / 100);
  const part = Math.abs(value % 100);
  return `${whole}.${String(part).padStart(2, "0")}`;
}

export function TradeTicket({ slug }: { slug?: string }) {
  const canTrade = useConnection((s) => s.canTrade());
  const whyNot = useConnection((s) => s.whyNot());
  const [amount, setAmount] = useState("10.00");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [intent, setIntent] = useState<string | null>(null);

  const key = newIdempotencyKey(`order:${slug ?? ""}:${side}:${amount}`);

  const send = async () => {
    if (!canTrade) {
      announce(whyNot ?? t("shell.connection.tradingDisabled", { reason: "disconnected" }));
      return;
    }
    if (!slug) {
      // A ticket with no market is not a ticket. It used to default to the string `"demo"` and post that as a market
      // id, so the only thing standing between a placeholder and an order was the venue's own 404 — a check that
      // belongs on this side of the wire.
      setRefusal(t("trade.ticket.noMarket"));
      return;
    }
    let cents;
    try {
      // Parsed by the money module and then re-emitted as a decimal string from the integer, so the value that goes
      // on the wire is the value the parser accepted: no `parseFloat` on the way out, and `"10.0"` and `"10.00"`
      // become the same amount instead of two different strings with the same meaning.
      cents = centsFromDecimal(amount);
    } catch (cause) {
      setRefusal(cause instanceof Error ? cause.message : String(cause));
      return;
    }
    setBusy(true);
    setRefusal(null);
    // `orderByAmount`, not `createOrder`: this route takes what a person actually types — a market, a side and a
    // budget — and re-reads the price server-side. The old body (`{market_id, side, amount_cents}` against
    // `/v1/orders`) needed a token id and a limit price the browser does not have and should not invent.
    const out = await request<{ intentId?: string; status?: string }>({
      key: "orderByAmount",
      body: { slug, side: side === "BUY" ? "yes" : "no", amountUsdc: usdcFromCents(cents) },
      idempotencyKey: key,
    });
    setBusy(false);
    if (!out.ok) {
      // The toast carries the code (an operator can read it, and support can ask for it); what the *user* reads here
      // is the sentence the API wrote. The line used to render `CODE: message`, which put a machine token in front of
      // the one thing the person needed — the same rule `_tg_plain_refusal` enforces on the chat side.
      pushRefusal(out.error.code, out.error.message, out.error.requestId);
      setRefusal(out.error.message);
      return;
    }
    if (usesMainButton("trade-confirm")) hapticConfirm();
    setIntent(String(out.data.intentId ?? out.data.status ?? "accepted"));
  };

  useEffect(() => {
    if (!canTrade) return;
    return showMainButton(t("tma.mainButton.trade"), () => void send());
  }, [canTrade, amount, side]);

  useEffect(() => {
    const onFocus = () => document.getElementById("pgm-ticket-amount")?.focus();
    window.addEventListener("openout:focus-ticket", onFocus);
    return () => window.removeEventListener("openout:focus-ticket", onFocus);
  }, []);

  return (
    <section
      className="unavailable"
      aria-label={t("shell.nav.trade")}
      data-can-trade={canTrade ? "yes" : "no"}
      style={{ display: "grid", gap: "var(--pgm-space-2)" }}
    >
      <strong>{t("shell.nav.trade")}</strong>
      {!canTrade ? (
        <p className="refusal" role="status">
          {t("shell.connection.tradingDisabled", { reason: whyNot ?? "unknown" })} · {t("shell.connection.cancelStillWorks")}
        </p>
      ) : null}
      <MoneyField label={t("trade.ticket.sizeLabel")} name="amount" value={amount} onChange={setAmount} help={t("profile.trading.size")} />
      <div style={{ display: "flex", gap: "var(--pgm-space-2)" }}>
        {(["BUY", "SELL"] as const).map((option) => (
          <Button key={option} aria-pressed={side === option} onClick={() => setSide(option)} variant={side === option ? "primary" : "default"}>
            {option}
          </Button>
        ))}
      </div>
      <Button variant="primary" onClick={() => void send()} pending={busy} why={canTrade ? undefined : (whyNot ?? undefined)}>
        {t("tma.mainButton.trade")}
      </Button>
      {refusal ? <p className="refusal" role="alert">{refusal}</p> : null}
      {intent ? <p role="status">intent {intent}</p> : null}
    </section>
  );
}
