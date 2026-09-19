"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { clampFraction, loadRails, railsFit, saveRails, widthVars, type RailFractions } from "./rails";
import { useConnection } from "./connection";

/**
 * Drag-resize that never renders React: the pointer handler writes a CSS custom property on the frame, and
 * the *fraction* is what lands in state (once, on pointer-up) because that is the only number worth keeping.
 * React state at 60fps during a drag would re-render the tape underneath, and the tape is 20 updates a
 * second already (web/DESIGN.md §4's "per-frame writes go to ref.current.style").
 */
export function useRails(frameRef: React.RefObject<HTMLElement | null>) {
  const [fractions, setFractions] = useState<RailFractions>({ left: 0.18, right: 0.22 });
  const [collapsed, setCollapsed] = useState({ left: false, right: false });
  const dragging = useRef<"left" | "right" | null>(null);
  const pushConnection = useConnection.getState().set;
  void pushConnection;

  useEffect(() => setFractions(loadRails()), []);

  const writeVars = useCallback(
    (next: RailFractions) => {
      const vars = widthVars(next, collapsed);
      const el = frameRef.current;
      if (!el) return;
      el.style.setProperty("--pgm-rail-left", vars["--pgm-rail-left"] ?? "");
      el.style.setProperty("--pgm-rail-right", vars["--pgm-rail-right"] ?? "");
    },
    [collapsed, frameRef],
  );

  useEffect(() => writeVars(fractions), [fractions, collapsed, writeVars]);

  const beginDrag = (side: "left" | "right") => (event: PointerEvent) => {
    event.preventDefault();
    dragging.current = side;
    const move = (e: PointerEvent) => {
      const el = frameRef.current;
      if (!el || !dragging.current) return;
      const rect = el.getBoundingClientRect();
      const fraction = dragging.current === "left" ? (e.clientX - rect.left) / rect.width : (rect.right - e.clientX) / rect.width;
      const provisional: RailFractions = { ...fractions, [dragging.current]: clampFraction(fraction) };
      if (!railsFit(provisional, collapsed)) return;
      writeVars(provisional);
      pending.current = provisional;
    };
    const pending = { current: fractions };
    const up = () => {
      dragging.current = null;
      setFractions(pending.current);
      saveRails(pending.current);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const toggle = (side: "left" | "right") => () =>
    setCollapsed((c) => {
      const next = { ...c, [side]: !c[side] };
      return railsFit(fractions, next) ? next : c;
    });

  return { fractions, collapsed, beginDrag, toggle };
}
