"use client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useToasts } from "@/ui/Toast";
import { isTma, telegramTheme, bottomInsetPx } from "@/telegram/bridge";
import { silentReauth } from "@/telegram/reauth";
import { useAuth } from "@/auth/session";

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // The feed is the authority on anything live; a query that re-fetched on focus would show a second,
        // differently-aged copy of the same price. Stale time is therefore the stamp's own ttl, applied per
        // call site, and the global default is "do not refetch by yourself".
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        retry: false,
      },
      mutations: { retry: false },
    },
  });
}

export function AppProviders({ children, initialAuth }: { children: ReactNode; initialAuth: "authenticated" | "unauthenticated" | "expired" }) {
  const client = useMemo(makeClient, []);
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
      void silentReauth(client).then((result) => {
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
  }, [client, initialAuth]);

  useEffect(() => {
    // The auth store mirrors the server's verdict. It is a *cache of a decision*, never the decision: a
    // protected page is refused by the proxy on the server (see app/api/[...path]) so a client that lies
    // about its state still cannot read or write anything.
    if (initialAuth === "authenticated" && useAuth.getState().state !== "authenticated") {
      useAuth.getState().authenticated({ id: "session" });
    }
  }, [initialAuth]);

  return (
    <QueryClientProvider client={client}>
      <div style={{ "--pgm-tab-inset": `${inset}px` } as React.CSSProperties}>{children}</div>
    </QueryClientProvider>
  );
}
