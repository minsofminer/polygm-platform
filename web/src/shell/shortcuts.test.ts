import { describe, expect, it } from "vitest";
import { dispatchFor, MOBILE_TABS, SHORTCUTS } from "./shortcuts";

const ev = (init: KeyboardEventInit) => ({ key: "", metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...init }) as KeyboardEvent;

describe("the map is the list and the list is the map", () => {
  it("every shortcut has a label to show and a matcher to run", () => {
    expect(SHORTCUTS.length).toBeGreaterThanOrEqual(8);
    for (const s of SHORTCUTS) {
      expect(s.label.length).toBeGreaterThan(3);
      expect(s.keys.length).toBeGreaterThan(0);
      expect(typeof s.match(ev({ key: "nonsense" }))).toBe("boolean");
    }
  });

  it("the five tabs are five, and each one is a destination the others do not contain", () => {
    expect(MOBILE_TABS.length).toBe(5);
    expect(new Set(MOBILE_TABS.map((t) => t.href)).size).toBe(5);
    expect(new Set(MOBILE_TABS.map((t) => t.label)).size).toBe(5);
  });
});

describe("focus-aware dispatch", () => {
  it("⌘K works from inside a field, because searching is how you leave the field", () => {
    expect(dispatchFor(ev({ key: "k", metaKey: true }), true)?.action).toBe("palette");
    expect(dispatchFor(ev({ key: "k", ctrlKey: true }), true)?.action).toBe("palette");
  });

  it("bare letters do nothing while typing, so 'T' can still be part of a market name", () => {
    expect(dispatchFor(ev({ key: "t" }), true)).toBeNull();
    expect(dispatchFor(ev({ key: "t" }), false)?.action).toBe("trade");
  });

  it("the cancel chord is one key and is never a modified accident", () => {
    expect(dispatchFor(ev({ key: "c" }), false)?.action).toBe("cancel-all");
    expect(dispatchFor(ev({ key: "c", metaKey: true }), false)).toBeNull();
  });

  it("Esc keeps its binding in a field: closing a panel is not a text-editing key", () => {
    expect(dispatchFor(ev({ key: "Escape" }), true)?.action).toBe("close");
  });
});
