"use client";
import { useState } from "react";
import { Button } from "@/ui/Button";
import { Field } from "@/ui/Field";
import { request } from "@/api/client";
import { RefusalNotice } from "@/ui/RefusalNotice";
import { t } from "@/i18n/t";

export default function ResetPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    const out = await request({ key: "passwordReset", body: { email } });
    setBusy(false);
    // Whether the route refused because it does not exist or because the address is unknown, the *answer to
    // the user* is the same sentence. A reset form that distinguishes them is an enumeration API with a
    // friendly UI.
    setSent(true);
    void out;
  };
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
      style={{ display: "grid", gap: "var(--pgm-space-3)" }}
    >
      <h1>{t("auth.reset.title")}</h1>
      <Field label={t("auth.reset.label.email")} name="email" type="email" value={email} onChange={setEmail} autoComplete="email" required />
      <Button type="submit" variant="primary" pending={busy}>
        {t("auth.reset.submit")}
      </Button>
      {sent ? <p role="status">{t("auth.reset.sent")}</p> : null}
      <p className="refusal">{t("auth.reset.note")}</p>
      <RefusalNotice route="passwordReset" extra={t("auth.reset.notServed")} />
    </form>
  );
}
