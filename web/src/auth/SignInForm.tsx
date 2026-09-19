"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/ui/Button";
import { Field } from "@/ui/Field";
import { t } from "@/i18n/t";
import { useAuth } from "./session";
import { isTma, tma } from "@/telegram/bridge";
import { pushRefusal } from "@/ui/Toast";

/**
 * Sign in. Three things this form must not do, and each is a line of code rather than a promise:
 *  - it never says "no such account": LOGIN_FAILED is the same sentence and the same timing for a wrong
 *    identifier and a wrong password, because the API already returns the same body for both;
 *  - it never shows the token it got back (there is none in the response — the proxy took it);
 *  - it never claims success while the server is still answering: the pending state is the button's, and
 *    the auth store only moves on the response.
 */
export function SignInForm() {
  const router = useRouter();
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lockSeconds, setLockSeconds] = useState<number | null>(null);
  const [needFactor, setNeedFactor] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    useAuth.getState().beginProbe(null);
    const response = await fetch("/api/session/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ identifier, password }),
    });
    const body = (await response.json().catch(() => null)) as { state?: string; error?: { code?: string; message?: string; requestId?: string; retryAfterS?: number } } | null;
    setBusy(false);
    if (!response.ok) {
      const code = body?.error?.code ?? "INTERNAL";
      // The server's own lock window is what the copy quotes. A client-side countdown invented from a
      // generic "too many attempts" would be a number we cannot explain to the person locked out.
      if (code === "ACCOUNT_LOCKED") setLockSeconds(body?.error?.retryAfterS ?? 900);
      if (code === "TOTP_REQUIRED") setNeedFactor(true);
      if (code === "SECURITY_ENV_MISSING") {
        pushRefusal(code, "this environment is not configured for auth and refuses to half-work", body?.error?.requestId ?? "");
        setError(t("auth.signin.failed"));
        return;
      }
      setError(code === "LOGIN_FAILED" ? t("auth.signin.failed") : String(body?.error?.message ?? code));
      useAuth.getState().expired({ reason: String(body?.error?.message ?? code) });
      return;
    }
    useAuth.getState().authenticated({ id: "session" });
    router.push("/terminal");
  };

  const telegram = isTma();

  return (
    <form onSubmit={submit} style={{ display: "grid", gap: "var(--pgm-space-3)" }}>
      <h1>{t("auth.signin.title")}</h1>
      <Field label={t("auth.signin.label.identifier")} name="identifier" value={identifier} onChange={setIdentifier} autoComplete="username" required />
      <Field
        label={t("auth.signin.label.password")}
        name="password"
        type="password"
        value={password}
        onChange={setPassword}
        autoComplete="current-password"
        required
        error={error}
      />
      {lockSeconds !== null ? <p className="refusal" role="alert">{t("auth.signin.locked", { seconds: lockSeconds })}</p> : null}
      {needFactor ? (
        <p className="refusal">
          {t("auth.twofa.title")} — <a href="/two-factor">{t("auth.factor.label")}</a>
        </p>
      ) : null}
      <Button type="submit" variant="primary" pending={busy}>
        {busy ? t("auth.signin.busy") : t("auth.signin.submit")}
      </Button>
      {telegram ? (
        <p className="refusal">
          {t("auth.signin.telegram")} — this device is inside Telegram, where the session is re-established from
          the signed launch data on the next request.
        </p>
      ) : null}
      <p>
        <a href="/sign-up">{t("auth.signin.noAccount")}</a> · <a href="/reset-password">{t("auth.signin.resetLink")}</a>
      </p>
      <p className="refusal">{t("auth.device.notify")}</p>
    </form>
  );
}

export function TmaSignIn() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const go = async () => {
    setBusy(true);
    const initData = tma()?.initData ?? "";
    const response = await fetch("/api/session/telegram", {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ initData }),
    });
    setBusy(false);
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as { error?: { message?: string; code?: string } } | null;
      setNote(body?.error?.message ?? "Telegram did not vouch for this account");
      return;
    }
    router.push("/terminal");
  };
  return (
    <Button onClick={go} pending={busy} why={note ?? undefined}>
      {t("auth.signin.telegram")}
    </Button>
  );
}
