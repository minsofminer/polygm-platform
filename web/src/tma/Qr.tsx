"use client";
/**
 * The QR, drawn from `qr.ts` — an SVG whose path is one square per dark module.
 *
 * Three decisions worth stating, because each is a way a QR "looks fine and does not scan":
 *
 *  - **The quiet zone is four modules, and it is drawn.** Scanners need a light margin the width of four modules; it
 *    is part of the *symbol*, not padding, so it goes in the `viewBox` (`-4 -4 size+8 size+8`) rather than in CSS
 *    margins, where a rounded card corner or a theme's background can quietly eat it.
 *  - **Dark modules on a light plate, whatever the theme.** A QR's contrast is a scanner requirement rather than a
 *    style choice, and this is the one mark in the product that must not follow the theme — a dark-mode QR is an
 *    inverted QR, which some scanners read and some do not. It is rendered inside a light-theme subtree
 *    (`data-theme="light"`) and coloured from the ordinary tokens (`--pgm-bg-base`, `--pgm-text-primary`, which in the
 *    light theme are #ffffff-family and near-black): the theme mechanism does the work, so there is no second colour
 *    system hiding in a component, and no hex in this file for the design-token scan to catch.
 *  - **`crispEdges`, and no animation.** A smoothed or animated QR is a QR that fails on a cheap camera, and this is
 *    the one control on the screen whose failure mode is money going to the wrong address.
 */
import { useMemo } from "react";
import { encodeQr, qrSvgPath, QrTooLongError } from "@/tma/qr";

export type QrCodeProps = {
  /** What to encode — an address, or an EIP-681 URI when the amount is known. */
  text: string;
  /** Rendered width and height in CSS pixels; the symbol scales, the module grid does not. */
  size?: number;
  /** The accessible name; a QR with no label is an image a screen reader cannot describe. */
  label: string;
};

export function QrCode({ text, size = 208, label }: QrCodeProps) {
  const matrix = useMemo(() => {
    try {
      return encodeQr(text);
    } catch (err) {
      // Over-length is a *typed* outcome, not a crash: the address is shown as text instead, which is what the
      // copy button is for. Anything else is a bug in the encoder and is allowed to break the render loudly.
      if (err instanceof QrTooLongError) return null;
      throw err;
    }
  }, [text]);

  if (!matrix) {
    return (
      <p className="pgm-tma-card" role="status">
        That address is longer than this QR can carry, so it is not drawn — copy it instead.
      </p>
    );
  }
  const modules = matrix.length;
  return (
    <span className="pgm-tma-qr" data-theme="light">
      <svg
        viewBox={`-4 -4 ${modules + 8} ${modules + 8}`}
        width={size}
        height={size}
        role="img"
        aria-label={label}
        shapeRendering="crispEdges"
      >
        <rect className="pgm-tma-qr__plate" x={-4} y={-4} width={modules + 8} height={modules + 8} />
        <path className="pgm-tma-qr__ink" d={qrSvgPath(matrix)} />
      </svg>
    </span>
  );
}

export default QrCode;
