import type { Metadata } from "next";
import type { ReactNode } from "react";
import { isMiniAppSurface } from "@/tma/surface.server";

/**
 * The Mini App entry. Same shell, same components, same routes — one codebase (P08 D2). What differs is the
 * document: the Telegram bridge script is loaded here and nowhere else, and the CSP for this subtree allows
 * Telegram to frame it (see next.config.mjs) while every other path has `frame-ancestors 'none'`.
 */
export const metadata: Metadata = {
  title: "Openout",
  robots: { index: false },
};

export default function TmaLayout({ children }: { children: ReactNode }) {
  // One injection, never two: on the Mini App's own deployment the root layout already owns the bridge (every page
  // there is the Mini App), and a second `script` tag with the same src is a second fetch and a second `WebApp`
  // assignment for no reason.
  return (
    <>
      {isMiniAppSurface() ? null : <script src="https://telegram.org/js/telegram-web-app.js" async />}
      {children}
    </>
  );
}
