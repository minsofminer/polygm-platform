"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Dialog, announce } from "@/ui/Dialog";
import { request } from "@/api/client";
import { ROUTES } from "@/api/routes";
import { MOBILE_TABS } from "./shortcuts";
import { t } from "@/i18n/t";

export type PaletteEntry = { group: "markets" | "traders" | "actions"; label: string; href?: string; run?: () => void; note?: string };

const DEBOUNCE_MS = 180;
/** A wallet is 0x + 40 hex. Only then do we offer the trader search: an address-typed query that hits the
 *  market index returns nothing useful and teaches the user that search is broken. */
const ADDRESS = /^0x[0-9a-fA-F]{40}$/;

export function filterEntries(query: string, markets: { id: string; question: string }[]): PaletteEntry[] {
  const q = query.trim().toLowerCase();
  const groups: PaletteEntry[] = [];
  if (ADDRESS.test(q)) {
    groups.push({ group: "traders", label: q, href: `/traders/${q}` });
  }
  for (const m of markets) {
    if (!q || m.question.toLowerCase().includes(q)) groups.push({ group: "markets", label: m.question, href: `/markets/${m.id}` });
  }
  for (const tab of MOBILE_TABS) groups.push({ group: "actions", label: t("shell.palette.goTo", { label: tab.label }), href: tab.href });
  return groups;
}

/**
 * The palette: search markets, jump to traders, run actions (P08 D4's "highest-leverage UX feature"). The
 * server-side search route is not built (launch item P08-L17) and the palette *says so* rather than
 * silently searching only its local list — a power user must know the ceiling they are under.
 */
export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [markets, setMarkets] = useState<{ id: string; question: string }[]>([]);
  const [searchNote, setSearchNote] = useState<string | null>(null);
  const input = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const id = window.setTimeout(() => setDebounced(query.trim()), DEBOUNCE_MS);
    return () => window.clearTimeout(id);
  }, [query, open]);

  useEffect(() => {
    if (!open || markets.length) return;
    void request<{ items?: { id: string; question: string }[] }>({ key: "markets", query: { limit: 50 } }).then((out) => {
      if (out.ok) setMarkets((out.data.items ?? []).map((m) => ({ id: String(m.id), question: String(m.question ?? m.id) })));
      else setSearchNote(out.error.message);
    });
  }, [open, markets.length]);

  useEffect(() => {
    if (ROUTES.globalSearch.built || !searchNote) return;
    setSearchNote(null);
  }, [searchNote]);

  const entries = useMemo(() => filterEntries(debounced, markets).slice(0, 12), [debounced, markets]);
  const searchAvailable = ROUTES.globalSearch.built;

  const run = (entry: PaletteEntry) => {
    if (entry.run) entry.run();
    else if (entry.href) router.push(entry.href);
    onClose();
  };

  return (
    <Dialog open={open} onClose={onClose} title={t("shell.palette.placeholder")}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const first = entries[0];
          if (first) run(first);
          else announce(t("shell.palette.empty"));
        }}
        role="search"
      >
        <input
          ref={input}
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("shell.palette.placeholder")}
          aria-label={t("shell.palette.placeholder")}
          className="field__input"
        />
      </form>
      {!searchAvailable ? <p className="refusal">{t("shell.palette.searchUnavailable")}</p> : null}
      {searchNote ? <p className="refusal">{searchNote}</p> : null}
      {entries.length === 0 ? (
        <p>{t("shell.palette.empty")}</p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {(["markets", "traders", "actions"] as const).map((group) => {
            const rows = entries.filter((e) => e.group === group);
            if (!rows.length) return null;
            return (
              <li key={group}>
                <h2 style={{ fontSize: "var(--pgm-font-dense)" }}>
                  {{ markets: t("shell.palette.group.markets"), traders: t("shell.palette.group.traders"), actions: t("shell.palette.group.actions") }[group]}
                </h2>
                <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
                  {rows.map((entry) => (
                    <li key={entry.href ?? entry.label}>
                      <button type="button" className="button" onClick={() => run(entry)}>
                        {entry.label}
                      </button>
                    </li>
                  ))}
                </ul>
              </li>
            );
          })}
        </ul>
      )}
    </Dialog>
  );
}
