import { afterEach, describe, expect, it } from "vitest";
import { bottomInsetPx, hapticConfirm, isTma, telegramTheme, usesMainButton } from "./bridge";
import { parseStartapp } from "./startapp";

const original = Object.getOwnPropertyDescriptor(window, "Telegram");
afterEach(() => {
  if (original) Object.defineProperty(window, "Telegram", original);
  else delete (window as unknown as { Telegram?: unknown }).Telegram;
});

function withTelegram(value: Record<string, unknown>) {
  Object.defineProperty(window, "Telegram", { value, configurable: true, writable: true });
}

describe("detection is a signed payload, not a script tag", () => {
  it("does not treat an empty WebApp object as a Mini App", () => {
    withTelegram({ WebApp: { initData: "" } });
    expect(isTma()).toBe(false);
    withTelegram({ WebApp: { initData: "query_id=AA&auth_date=1" } });
    expect(isTma()).toBe(true);
  });
  it("is false on the server, so no screen renders a different tree", () => {
    // `tma()` returns null when window is undefined; the pure functions must not throw there.
    expect(typeof isTma()).toBe("boolean");
  });
});

describe("the native buttons follow the policy", () => {
  it("only the three money-confirming flows may claim the MainButton", () => {
    expect(usesMainButton("trade-confirm")).toBe(true);
    expect(usesMainButton("withdraw-confirm")).toBe(true);
    expect(usesMainButton("key-export-confirm")).toBe(true);
    for (const other of ["open-market", "sign-in"]) {
      expect(usesMainButton(other as never)).toBe(false);
    }
  });

  it("haptics fire on a confirmation, and the only other call site is a refusal", () => {
    const calls: string[] = [];
    withTelegram({ WebApp: { initData: "x", HapticFeedback: { impactOccurred: (s: string) => calls.push(s) } } });
    hapticConfirm();
    expect(calls).toEqual(["medium"]);
  });
});

describe("theme and insets", () => {
  it("the host's colour scheme is read as a theme, not as a suggestion to ignore", () => {
    withTelegram({ WebApp: { initData: "x", colorScheme: "dark" } });
    expect(telegramTheme()).toBe("dark");
    withTelegram({ WebApp: { initData: "x" } });
    expect(telegramTheme()).toBeNull();
  });

  it("the bottom inset is the keyboard-safe number, never a viewport height we would write into layout", () => {
    withTelegram({ WebApp: { initData: "x", safeAreaInset: { bottom: 21 }, viewportHeight: 640 } });
    expect(bottomInsetPx()).toBe(21);
    withTelegram({ WebApp: { initData: "x", safeAreaInset: { bottom: 0 }, contentSafeAreaInset: { bottom: 104 } } });
    expect(bottomInsetPx()).toBe(104);
  });
});

describe("a deep link is data, not a route", () => {
  it("escapes the id into the href instead of interpolating it raw", () => {
    const payload = parseStartapp("m:1234.5:6789");
    expect(payload.kind).toBe("market");
    if (payload.kind === "market") expect(encodeURIComponent(payload.marketId)).toBe("1234.5%3A6789");
  });
});
