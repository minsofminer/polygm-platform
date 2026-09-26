"use client";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useToasts } from "@/ui/Toast";
import { isTma, telegramTheme, bottomInsetPx } from "@/telegram/bridge";
import { silentReauth } from "@/telegram/reauth";
import { useAuth } from "@/auth/session";

export function AppProviders({ children, initialAuth }: { children: ReactNode; initialAuth: "authenticated" | "unauthenticated" | "expired" }) {
  // A stable object, used only as an identity token by `silentReauth` (it de-dupes "one attempt per cold
  // start" on it). It used to be the query client; nothing about the query cache was ever involved.
  const coldStart = useRef({}).current;
  const [inset, setInset] = useState(0);

  useEffect(() => {
    // The server already rendered <html data-theme>/<html data-density> from the device cookie, so there is
    // no flash of the wrong theme and no hydration mismatch. Inside the webview the host's scheme wins.
    const tmaHere = isTma();
    if (tmaHere) {
      const host = telegramTheme();
      if (host) document.documentElement.dataset.theme = host;
      setInset(bottomInsetPx());
      window.addEventListener("resize", () => setInset(bottomInsetPx()), { passive: true });
    }
    if (initialAuth === "expired" && tmaHere) {
      void silentReauth(coldStart).then((result) => {
        if (result.attempted && result.ok) window.location.reload();
        else if (result.attempted)
          useToasts.getState().push({
            key: `tma-reauth:${result.code}`,
            text: "Telegram did not give this device a session. Sign in once and it will stay signed in here.",
            tone: "error",
            ttlMs: 0,
          });
      });
    }
  }, [coldStart, initialAuth]);

  useEffect(() => {
    // The auth store mirrors the server's verdict. It is a *cache of a decision*, never the decision: a
    // protected page is refused by the proxy on the server (see app/api/[...path]) so a client that lies
    // about its state still cannot read or write anything.
    if (initialAuth === "authenticated" && useAuth.getState().state !== "authenticated") {
      useAuth.getState().authenticated({ id: "session" });
    }
  }, [initialAuth]);

  return <div style={{ "--pgm-tab-inset": `${inset}px` } as React.CSSProperties}>{children}</div>;
}
