"use client";
import { useState } from "react";
import { Button } from "@/ui/Button";
import { Field } from "@/ui/Field";
import { RefusalNotice } from "@/ui/RefusalNotice";
import { showMainButton, usesMainButton, hapticConfirm } from "@/telegram/bridge";
import { useEffect } from "react";
import { t } from "@/i18n/t";

/**
 * Key export, multi-step, with the warning that matters in step one rather than in a modal nobody reads, and
 * a typed confirmation that is *checked* (not merely present). The final step is refused by the client
 * because the route does not exist: an "export" button that produced a fake key would be the single worst
 * thing this screen could do, so it produces nothing and says which launch item will make it real.
 */
export function KeyExport() {
  const [typed, setTyped] = useState("");
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const armed = typed.trim() === "EXPORT";
  useEffect(() => {
    if (step !== 2 || !armed) return;
    return showMainButton(t("wallet.keys.title"), () => {
      if (usesMainButton("key-export-confirm")) hapticConfirm();
      setStep(3);
    });
  }, [step, armed]);
  return (
    <section style={{ display: "grid", gap: "var(--pgm-space-2)" }} aria-label={t("wallet.keys.title")}>
      <h2>{t("wallet.keys.title")}</h2>
      <p className="refusal" role="alert">
        {t("wallet.keys.warning")}
      </p>
      {step === 1 ? <Button variant="danger" onClick={() => setStep(2)}>{t("common.button.continue")}</Button> : null}
      {step === 2 ? (
        <>
          <Field label={t("wallet.keys.typeToConfirm")} name="confirm" value={typed} onChange={setTyped} help={t("wallet.keys.help.typeToConfirm")} />
          <Button variant="danger" disabled={!armed} onClick={() => setStep(3)}>
            {t("wallet.keys.title")}
          </Button>
        </>
      ) : null}
      {step === 3 ? <p role="status">{t("wallet.keys.step3")}</p> : null}
      <RefusalNotice route="keyExport" extra={t("wallet.keys.unavailable")} />
    </section>
  );
}
