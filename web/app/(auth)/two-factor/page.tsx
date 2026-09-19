"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/ui/Button";
import { Field } from "@/ui/Field";
import { request } from "@/api/client";
import { t } from "@/i18n/t";

export default function TwoFactorPage() {
  const router = useRouter();
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    // Codes are 6 digits, single-use, and the window is the server's: the client does not compute a step,
    // because a clock five seconds off on a phone turns a valid code into a "wrong code" refusal.
    const out = await request<{ ok?: boolean }>({ key: "totpVerify", body: { code }, timeoutMs: 6_000 });
    setBusy(false);
    if (out.ok) {
      router.push("/terminal");
      return;
    }
    const e = out.error;
    if (e.code === "TOTP_LOCKED") setError(t("auth.twofa.locked", { seconds: e.retryAfterS ?? 60 }));
    else if (e.code === "TOTP_INVALID") setError(t("auth.twofa.expired"));
    else setError(e.message);
  };
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
      style={{ display: "grid", gap: "var(--pgm-space-3)", maxWidth: "var(--pgm-space-20)" }}
    >
      <h1>{t("auth.twofa.title")}</h1>
      <Field label={t("auth.twofa.label.code")} name="code" value={code} onChange={setCode} inputMode="numeric" autoComplete="one-time-code" required error={error} />
      <Button type="submit" variant="primary" pending={busy}>
        {t("auth.twofa.submit")}
      </Button>
      <p>
        <a href="/sign-in">{t("common.button.close")}</a>
      </p>
    </form>
  );
}
