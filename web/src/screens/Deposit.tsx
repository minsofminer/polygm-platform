"use client";
import { useState } from "react";
import { Button } from "@/ui/Button";
import { RefusalNotice } from "@/ui/RefusalNotice";
import { t } from "@/i18n/t";

const NETWORKS = [
  { id: "polygon", label: t("wallet.deposit.network.polygon"), minutes: 2 },
  { id: "base", label: t("wallet.deposit.network.base"), minutes: 1 },
];

export function Deposit() {
  const [network, setNetwork] = useState(NETWORKS[0]!.id);
  const [copied, setCopied] = useState<string | null>(null);
  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(value.slice(-4));
    } catch {
      setCopied(null);
      window.dispatchEvent(new CustomEvent("openout:copy-failed"));
    }
  };
  return (
    <section style={{ display: "grid", gap: "var(--pgm-space-2)" }} aria-label={t("wallet.deposit.title")}>
      <h2>{t("wallet.deposit.title")}</h2>
      <fieldset style={{ border: "var(--pgm-border-w-hair) solid var(--pgm-border-default)", borderRadius: "var(--pgm-radius-md)" }}>
        <legend>{t("wallet.deposit.network")}</legend>
        {NETWORKS.map((n) => (
          <label key={n.id} style={{ display: "block", minWidth: "var(--pgm-min-touch-target)" }}>
            <input type="radio" name="network" value={n.id} checked={network === n.id} onChange={() => setNetwork(n.id)} /> {n.label} ·{" "}
            <span title={t("wallet.deposit.confirmations")}>{t("wallet.deposit.confirmationsMinutes", { minutes: n.minutes })}</span>
          </label>
        ))}
        <p className="refusal">{t("wallet.deposit.networkWarning")}</p>
      </fieldset>
      <p>
        {t("wallet.deposit.address")}: <code>0x…</code>{" "}
        <Button onClick={() => copy("0x0000000000000000000000000000000000000000")}>{t("wallet.deposit.copy")}</Button>
        {copied ? <span role="status">{t("wallet.deposit.copied", { tail: copied })}</span> : null}
      </p>
      <p>{t("wallet.deposit.minimum")}: 1.00 · {t("wallet.deposit.confirmations")}: 12</p>
      <p role="status">{t("wallet.deposit.waiting")}</p>
      <RefusalNotice route="deposit" extra={t("wallet.deposit.unwatched")} />
    </section>
  );
}
