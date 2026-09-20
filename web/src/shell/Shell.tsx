"use client";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ConnectionDot } from "./ConnectionDot";
import { CommandPalette } from "./CommandPalette";
import { ShortcutsOverlay } from "./ShortcutsOverlay";
import { dispatchFor, TAB_HREFS } from "./shortcuts";
import { useRails } from "./useRails";
import { useLive } from "@/live/useLive";
import { useConnection } from "./connection";
import { Button } from "@/ui/Button";
import { Toasts } from "@/ui/Toast";
import { t } from "@/i18n/t";
import { usesMainButton, hapticConfirm } from "@/telegram/bridge";
import { parseStartapp, startappTarget } from "@/telegram/startapp";

/**
 * The frame. Desktop: three columns with resizable rails. Mobile: five tabs. Both: the connection dot, the
 * palette, the help overlay, and the trade gate — one store, so the dot and the ticket cannot disagree.
 */
export function Shell({ children }: { children: React.ReactNode }) {
  const frame = useRef<HTMLDivElement | null>(null);
  const [palette, setPalette] = useState(false);
  const [help, setHelp] = useState(false);
  const { beginDrag, collapsed, toggle } = useRails(frame);
  // The shell owns the tape feed: every widget's freshness derives from the same stamp the dot shows, which
  // is the only way "trading is disabled while disconnected" and the indicator can be the same fact.
  const feed = useLive("tape");
  const setConn = useConnection((s) => s.set);
  const canTrade = useConnection((s) => s.canTrade());
  void setConn;
  useEffect(() => {
    useConnection.getState().set({
      mode: feed.mode,
      connected: feed.connected,
      freshness: feed.freshness,
      gapCount: feed.gapCount,
      lastGapCount: feed.lastGapCount,
      resyncing: feed.resyncing,
      blockReason: feed.blockReason,
      staleMs: feed.stamp ? Math.max(0, Date.now() - feed.stamp.asOf) : null,
    });
  }, [feed]);

  // `?startapp=` is read on the client, not from the layout's props: the App Router gives `searchParams` to
  // pages, not layouts, and a layout prop that is silently `undefined` is how deep links stop working with no
  // error anywhere. These routes are dynamic anyway (the auth check reads cookies), so this costs nothing.
  const params = useSearchParams();
  const router = useRouter();
  const startapp = params?.get("startapp") ?? null;
  const target = startapp ? startappTarget(parseStartapp(startapp)) : null;
  const notice = target?.notice ?? null;
  const deepLink = target?.href ?? null;
  // A deep link that resolves to a market has to *go* there. Until P12 this layout read the payload, computed the
  // notice, and dropped the href on the floor: `?startapp=` on the main site showed a toast (or nothing) and left the
  // user wherever they already were. `replace` rather than `push` so the back button does not bounce between the
  // landing page and the market the link named.
  const went = useRef(false);
  useEffect(() => {
    if (!deepLink || went.current) return;
    went.current = true;
    router.replace(deepLink);
  }, [deepLink, router]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const inField = !!target && (target.isContentEditable || /^(input|textarea|select)$/i.test(target.tagName));
      const hit = dispatchFor(event, inField);
      if (!hit) return;
      event.preventDefault();
      switch (hit.action) {
        case "palette":
          setPalette(true);
          break;
        case "help":
          setHelp(true);
          break;
        case "close":
          setPalette(false);
          setHelp(false);
          break;
        case "cancel-all":
          if (usesMainButton("trade-confirm")) hapticConfirm();
          window.dispatchEvent(new CustomEvent("openout:cancel-all"));
          break;
        case "trade":
          window.dispatchEvent(new CustomEvent("openout:focus-ticket"));
          break;
        default:
          window.location.assign(TAB_HREFS[hit.action] ?? "/markets");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <>
      <header className="topbar">
        <Link href="/markets" aria-label={t("shell.title.brand")}>
          <strong>{t("shell.title.brand")}</strong>
        </Link>
        <Button onClick={() => setPalette(true)} aria-keyshortcuts="Meta+K Control+K">
          {t("shell.palette.placeholder")}
        </Button>
        <Button onClick={() => setHelp(true)} aria-keyshortcuts="?">
          {t("shell.shortcut.title")}
        </Button>
        <ConnectionDot />
        {!canTrade ? (
          <span className="refusal" role="status">
            {t("shell.connection.cancelStillWorks")}
          </span>
        ) : null}
      </header>
      {notice ? <p className="refusal">{notice}</p> : null}
      <div ref={frame} className="frame" data-can-trade={canTrade ? "yes" : "no"}>
        <aside className={`rail${collapsed.left ? " rail--collapsed" : ""}`} aria-label={t("shell.nav.markets")}>
          <button type="button" className="button" onClick={toggle("left")}>
            {collapsed.left ? t("shell.rail.expand") : t("shell.rail.collapse")}
          </button>
          <nav>{children ? null : null}</nav>
        </aside>
        <main style={{ minWidth: 0 }}>{children}</main>
        <aside className={`rail${collapsed.right ? " rail--collapsed" : ""}`} aria-label={t("shell.nav.profile")} />
        <span
          role="separator"
          aria-orientation="vertical"
          aria-label={t("shell.rail.persisted")}
          tabIndex={0}
          className="handle"
          onPointerDown={beginDrag("left") as unknown as never}
        />
        <span
          role="separator"
          aria-orientation="vertical"
          aria-label={t("shell.rail.persisted")}
          tabIndex={0}
          className="handle"
          onPointerDown={beginDrag("right") as unknown as never}
        />
      </div>
      <nav className="tabs" aria-label={t("shell.nav.markets")}>
        {[
          { href: "/markets", label: t("shell.nav.markets") },
          { href: "/tape", label: t("shell.nav.tape") },
          { href: "/terminal", label: t("shell.nav.trade") },
          { href: "/portfolio", label: t("shell.nav.portfolio") },
          { href: "/profile", label: t("shell.nav.profile") },
        ].map((tab) => (
          <Link key={tab.href} href={tab.href} className="tab">
            {tab.label}
          </Link>
        ))}
      </nav>
      <CommandPalette open={palette} onClose={() => setPalette(false)} />
      <ShortcutsOverlay open={help} onClose={() => setHelp(false)} />
      <Toasts />
    </>
  );
}
