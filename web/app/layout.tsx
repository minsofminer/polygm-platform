import type { Metadata, Viewport } from "next";
import { cookies } from "next/headers";
import "./globals.css";
import { AppProviders } from "@/app-providers";
import { Toasts } from "@/ui/Toast";
import { serverAuth } from "@/auth/server";
import { isMiniAppSurface } from "@/tma/surface.server";
import themeColors from "../styles/theme-colors.json";

export const metadata: Metadata = {
  title: { default: "Openout", template: "%s · Openout" },
  description:
    "Read the Polymarket tape, the books and the whales behind them. Openout is a keyboard-first terminal for prediction markets.",
  metadataBase: new URL(process.env.NEXT_PUBLIC_SITE_ORIGIN ?? "https://openout.app"),
  robots: isMiniAppSurface() ? { index: false, follow: false } : { index: true, follow: true },
  other: { "og:site_name": "Openout" },
};

/** The webview needs `viewport-fit=cover` or the safe-area insets are all zero and the tab bar sits under
 *  the home indicator. `userScalable` stays true: pinch-zoom is an accessibility feature, not a bug. */
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: themeColors.dark },
    { media: "(prefers-color-scheme: light)", color: themeColors.light },
  ],
};

/**
 * The theme and density are decided on the server, from the device's own stored preference, and written as
 * attributes on <html>. That is the only way to satisfy two rules at once: "dark theme as default" (P08 D1)
 * and "no flash" — a client effect that sets data-theme paints a light frame first on every load. The
 * fallback is dark, so a first-time visitor sees the theme the product is designed in.
 */
export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const store = await cookies();
  const theme = store.get("pgm_theme")?.value === "light" ? "light" : "dark";
  const density = store.get("pgm_density")?.value ?? "compact";
  const auth = await serverAuth();
  return (
    <html lang="en" data-theme={theme} data-density={density}>
      <body className="font-body">
        {/* On the Mini App's own deployment every page is the Mini App, so the bridge belongs to the document. On the
            main site it belongs to the `/tma` subtree alone (see app/tma/layout.tsx) — loading it everywhere there
            would hand `initDataUnsafe` and HapticFeedback to pages that have no business calling them. */}
        {isMiniAppSurface() ? <script src="https://telegram.org/js/telegram-web-app.js" async /> : null}
        <AppProviders initialAuth={auth.state}>
          {children}
          <Toasts />
        </AppProviders>
      </body>
    </html>
  );
}
