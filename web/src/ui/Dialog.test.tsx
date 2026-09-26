/**
 * The dialog's two-step close, and the case that makes it non-optional.
 *
 * The motion itself is asserted in `motion.test.ts` against the stylesheet; what is tested here is the lifecycle the
 * motion depends on, because the interesting failure is not "the panel did not animate" — it is **the dialog that
 * cannot be closed**. `onAnimationEnd` never fires when `prefers-reduced-motion: reduce` has removed the animations,
 * so an exit that waits for it hangs for exactly the users the media query exists to protect. Same for a browser
 * without `matchMedia` at all. Both are cases below, not hypotheticals.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Dialog } from "./Dialog";
import { t } from "@/i18n/t";

function renderDialog(props: { busy?: boolean } = {}) {
  const onClose = vi.fn();
  const view = render(
    // The title comes from the dictionary, not a literal: P08's c9 refuses copy outside the i18n layer, and a test
    // is not exempt from that — the check caught this line, which is the check doing its job.
    <Dialog open onClose={onClose} title={t("common.button.close")} busy={props.busy}>
      <p>body</p>
    </Dialog>,
  );
  return { onClose, ...view };
}

describe("the dialog enters, and closes in two steps", () => {
  beforeEach(() => vi.stubGlobal("matchMedia", (q: string) => ({ matches: false, media: q,
                                                                addEventListener() {}, removeEventListener() {} })));
  afterEach(() => vi.unstubAllGlobals());

  it("renders the entrance classes on both layers", () => {
    const { container } = renderDialog();
    expect(container.querySelector(".overlay")?.className).toContain("pgm-fade-in");
    expect(container.querySelector(".overlay__panel")?.className).toContain("pgm-panel-in");
  });

  it("Escape starts the exit and only its end calls onClose", () => {
    const { onClose, container } = renderDialog();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
    expect(container.querySelector(".overlay__panel")?.className).toContain("pgm-panel-out");
    expect(container.querySelector(".overlay")?.className).toContain("pgm-fade-out");

    fireEvent.animationEnd(container.querySelector(".overlay__panel")!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("ignores an animationend bubbling from a child of the panel", () => {
    const { onClose, container } = renderDialog();
    fireEvent.keyDown(document, { key: "Escape" });
    fireEvent.animationEnd(container.querySelector("p")!);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes immediately, with no exit to wait for, under reduced motion", () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduced-motion"), media: q,
                                                  addEventListener() {}, removeEventListener() {} }));
    const { onClose } = renderDialog();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("still plays the exit when the browser has no matchMedia, because its CSS animations do run", () => {
    // The first draft of this test asserted the opposite, and writing it down is the point: the guard is not
    // "no matchMedia ⇒ skip the animation". A browser old enough to lack `matchMedia` still runs CSS animations,
    // so waiting for `animationend` there is correct; waiting is only wrong when something has *removed* the
    // animation, and the only thing that does that is the reduced-motion media query.
    vi.stubGlobal("matchMedia", undefined);
    const { onClose, container } = renderDialog();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.animationEnd(container.querySelector(".overlay__panel")!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("still refuses the close while a request is in flight, and says why", () => {
    const { onClose, container } = renderDialog({ busy: true });
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
    expect(container.querySelector(".overlay__panel")?.className).not.toContain("pgm-panel-out");
    expect(document.getElementById("pgm-announcer")?.textContent).toContain("in flight");
  });
});
