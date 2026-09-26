"use client";
import { useEffect, useState } from "react";
import { useResource } from "@/api/data";
import { request } from "@/api/client";
import { Button } from "@/ui/Button";
import { Field } from "@/ui/Field";
import { RefusalNotice } from "@/ui/RefusalNotice";
import { t } from "@/i18n/t";

/**
 * Profile and settings (D5), and the display section is the one part that works completely today: theme and
 * density are device preferences, so they write a cookie the root layout reads on the next request (no
 * flash, no hydration mismatch, and no dependence on the settings route that P08-L14 will own). Everything
 * that needs the server to remember says so.
 */
export function ProfileClient() {
  const [theme, setTheme] = useState("dark");
  const [density, setDensity] = useState("compact");
  const [oneClick, setOneClick] = useState(false);
  const [confirmAbove, setConfirmAbove] = useState("250.00");

  useEffect(() => {
    setTheme(document.documentElement.dataset.theme ?? "dark");
    setDensity(document.documentElement.dataset.density ?? "compact");
  }, []);

  const save = (patch: Record<string, string>) => {
    for (const [name, value] of Object.entries(patch)) {
      document.cookie = `${name}=${value}; path=/; max-age=31536000; samesite=lax${location.protocol === "https:" ? "; secure" : ""}`;
    }
    if (patch.pgm_theme) document.documentElement.dataset.theme = patch.pgm_theme;
    if (patch.pgm_density) document.documentElement.dataset.density = patch.pgm_density;
  };

  const sessions = useResource(["sessions"], async () => {
    const out = await request<{ items?: Record<string, unknown>[] }>({ key: "sessions" });
    if (!out.ok) throw new Error(`${out.error.code}: ${out.error.message}`);
    return out.data.items ?? [];
  });

  const revoke = async (id: string) => {
    const out = await request({ key: "revokeSessions", body: { sessionId: id } });
    if (!out.ok) throw new Error(`${out.error.code}: ${out.error.message}`);
    void sessions.refetch();
  };

  return (
    <div style={{ display: "grid", gap: "var(--pgm-space-5)" }}>
      <section aria-label={t("profile.section.display")}>
        <h2>{t("profile.section.display")}</h2>
        <div style={{ display: "flex", gap: "var(--pgm-space-2)", flexWrap: "wrap" }}>
          {["dark", "light"].map((option) => (
            <Button key={option} aria-pressed={theme === option} onClick={() => { setTheme(option); save({ pgm_theme: option }); }}>
              {option === "dark" ? t("profile.display.theme.dark") : t("profile.display.theme.light")}
            </Button>
          ))}
          {["dense", "compact", "comfortable"].map((option) => (
            <Button key={option} aria-pressed={density === option} onClick={() => { setDensity(option); save({ pgm_density: option }); }}>
              {{ dense: t("profile.display.density.dense"), compact: t("profile.display.density.compact"), comfortable: t("profile.display.density.comfortable") }[option]}
            </Button>
          ))}
        </div>
        <p className="refusal">{t("profile.display.colourBlindNote")}</p>
      </section>

      <section aria-label={t("profile.section.trading")}>
        <h2>{t("profile.section.trading")}</h2>
        <Field label={t("profile.trading.confirmAbove")} name="confirmAbove" value={confirmAbove} onChange={setConfirmAbove} inputMode="decimal" />
        <label style={{ display: "block", minWidth: "var(--pgm-min-touch-target)" }}>
          <input type="checkbox" checked={oneClick} onChange={(e) => setOneClick(e.target.checked)} /> {t("profile.trading.oneClick")}
        </label>
        {oneClick ? <p className="refusal" role="alert">{t("profile.trading.oneClickWarning")}</p> : null}
        <p>{t("profile.trading.builderFee")}: the rate is ours and is queryable on-chain; the display arrives with P10.</p>
        <RefusalNotice route="tradingDefaults" extra={t("profile.section.unsaved")} />
      </section>

      <section aria-label={t("profile.section.security")}>
        <h2>{t("profile.section.security")}</h2>
        {sessions.isPending ? <p role="status">{t("common.state.loading")}</p> : null}
        {sessions.isError ? <p role="alert" className="refusal">{String(sessions.error.message)}</p> : null}
        <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: "var(--pgm-space-1)" }}>
          {(sessions.data ?? []).map((row) => (
            <li key={String(row.id)} style={{ display: "flex", gap: "var(--pgm-space-2)", alignItems: "center" }}>
              <span>{String(row.device_label ?? "device")}</span>
              <span className="refusal">{row.kind === "telegram" ? t("auth.session.kind.telegram") : t("auth.session.kind.web")}</span>
              <span>{row.active ? "" : row.expired ? t("auth.session.expired") : String(row.revoked_reason ?? "")}</span>
              {row.active ? (
                <Button onClick={() => void revoke(String(row.id))} variant="danger">
                  {t("auth.session.revoke")}
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
        <form
          action="/api/session/logout"
          method="post"
          onSubmit={async (e) => {
            e.preventDefault();
            await fetch("/api/session/logout", { method: "POST", credentials: "same-origin" });
            location.assign("/sign-in");
          }}
        >
          <Button type="submit">{t("auth.session.logout")}</Button>{" "}
          <Button
            type="button"
            variant="danger"
            onClick={async () => {
              const out = await request({ key: "revokeSessions", body: { sessionId: "all", everywhere: true } });
              if (!out.ok) throw new Error(out.error.code);
              location.assign("/sign-in");
            }}
          >
            {t("auth.session.logoutEverywhere")}
          </Button>
        </form>
        <RefusalNotice route="recoveryCodes" extra={t("profile.recovery.extra")} />
        <h3>{t("profile.section.danger")}</h3>
        <p>{t("profile.danger.detail")}</p>
        <RefusalNotice route="deleteAccount" />
      </section>

      <section aria-label={t("profile.section.notifications")}>
        <h2>{t("profile.section.notifications")}</h2>
        <p>{["telegram", "email", "push"].map((c) => ({ telegram: t("profile.notifications.channel.telegram"), email: t("profile.notifications.channel.email"), push: t("profile.notifications.channel.push") }[c]!)).join(" · ")}</p>
        <p>{t("profile.notifications.quiet")} · {t("profile.notifications.digest")}</p>
        <RefusalNotice route="notificationPrefs" extra={t("profile.section.unsaved")} />
      </section>

      <section aria-label={t("profile.section.apiKeys")}>
        <h2>{t("profile.section.apiKeys")}</h2>
        <RefusalNotice route="apiKeys" extra={t("profile.apiKeys.extra")} />
      </section>
    </div>
  );
}
