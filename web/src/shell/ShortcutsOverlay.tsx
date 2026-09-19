"use client";
import { Dialog } from "@/ui/Dialog";
import { SHORTCUTS } from "./shortcuts";
import { t } from "@/i18n/t";

/** The discoverable help overlay, generated from the same table the handler reads (see shortcuts.ts). */
export function ShortcutsOverlay({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog open={open} onClose={onClose} title={t("shell.shortcut.title")}>
      <table>
        <tbody>
          {SHORTCUTS.map((s) => (
            <tr key={s.keys}>
              <th scope="row" style={{ textAlign: "left" }}>
                <kbd>{s.keys}</kbd>
              </th>
              <td>{s.label}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <button type="button" className="button" onClick={onClose}>
        {t("common.button.close")}
      </button>
    </Dialog>
  );
}
