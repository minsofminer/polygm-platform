/**
 * The QR encoder, against a reference encoder.
 *
 * A QR code that is *almost* right is the worst kind of wrong: it scans into something, and the something is an
 * address nobody owns. So this file does not check "a matrix came out" — it compares every cell of three matrices
 * with the ones `tools/build-qr-fixtures.py` produced for the same text (`qr.fixtures.json`, level M, no border) and
 * the mask that tool chose. Passing means the byte-mode encoding, the character count, the padding, the
 * Reed-Solomon codewords, the block interleaving, the function patterns, the data walk, the format and version
 * headers, all eight masks and the penalty rules that pick between them are the ones the spec describes.
 *
 * The three inputs are chosen for what they exercise, not for looks:
 *
 *   `address` — 42 bytes, version 3: one block, and its data fills that block exactly, so the symbol carries no pad
 *               codewords at all. This is the case the wallet screen actually uses.
 *   `eip681`  — 55 bytes, version 4: two blocks, so interleaving and pad codewords are both exercised.
 *   `long`    — 150 bytes, version 8: four unequal blocks *and* the version header, which only exists from version 7.
 *
 * The fixtures were first generated with an unpatched reference and *two of the three disagreed*, in the error
 * correction tail: the reference inserts a spare zero codeword when the bit stream already ends on a codeword
 * boundary, which ISO/IEC 18004 §7.4.10 does not do. See the tool's docstring — the padding correction and the
 * cross-check against a third implementation are why this file's matrices are worth comparing against.
 *
 * `readFileSync` resolves from the process cwd on purpose: `new URL(..., import.meta.url)` is a `file:` URL in vitest
 * and this suite runs from `web/` (the same gotcha `route-notes.test.ts` records).
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { encodeQr, qrSvgPath, qrVersionFor, QrTooLongError } from "@/tma/qr";

type Fixture = { text: string; version: number; mask: number; size: number; rows: string[] };
const fixtures = JSON.parse(
  readFileSync(resolve(process.cwd(), "src/tma/qr.fixtures.json"), "utf8"),
) as Record<string, Fixture>;

const asRows = (matrix: readonly (readonly boolean[])[]): string[] =>
  matrix.map((row) => row.map((dark) => (dark ? "1" : "0")).join(""));

/** A fixture by name; the file on disk is generated, so a missing case is a stale-fixture bug, not a test bug. */
const caseOf = (name: string): Fixture => {
  const found = fixtures[name];
  if (found === undefined) throw new Error(`qr.fixtures.json has no \`${name}\` case — run tools/build-qr-fixtures.py`);
  return found;
};

describe("the QR encoder matches the reference encoder", () => {
  for (const [name, fixture] of Object.entries(fixtures)) {
    it(`${name}: every cell, including the mask it chose`, () => {
      const matrix = encodeQr(fixture.text);
      expect(matrix.length).toBe(fixture.size);
      expect(asRows(matrix)).toEqual(fixture.rows);
      // The fixture records what the reference decided, so a reader can see which mask this is asserting about.
      expect(fixture.mask).toBeGreaterThanOrEqual(0);
      expect(fixture.version).toBeGreaterThan(0);
    });
  }

  it("the fixture file is not empty, so the loop above cannot pass vacuously", () => {
    expect(Object.keys(fixtures).length).toBe(3);
    for (const fixture of Object.values(fixtures)) expect(fixture.rows.length).toBe(fixture.size);
  });
});

describe("what the encoder refuses, and how", () => {
  it("picks the smallest version that fits, and says which", () => {
    expect(qrVersionFor(14)).toBe(1);   // byte mode at level M: 16 data codewords, minus mode and length headers
    expect(qrVersionFor(15)).toBe(2);
    expect(qrVersionFor(42)).toBe(3);   // the fixture's address, and the reason the fixture is version 3
    expect(qrVersionFor(213)).toBe(10); // the top of the range this encoder claims
  });

  it("throws a typed error past its own ceiling rather than truncating a destination", () => {
    const tooLong = "x".repeat(214);
    expect(qrVersionFor(214)).toBeNull();
    expect(() => encodeQr(tooLong)).toThrow(QrTooLongError);
    try {
      encodeQr(tooLong);
    } catch (err) {
      // The screen renders this sentence, so the error has to carry the numbers it is about.
      expect((err as QrTooLongError).bytes).toBe(214);
      expect((err as QrTooLongError).capacity).toBeGreaterThan(200);
    }
  });

  it("handles a byte string that is not ASCII, because a memo field can be anything", () => {
    const matrix = encodeQr("memo: café ✓");
    const rows = asRows(matrix);
    expect(rows.length).toBeGreaterThan(20);
    expect(rows.every((row) => row.length === rows.length)).toBe(true);
  });
});

describe("the SVG path", () => {
  it("has one square per dark module, and none for the light ones", () => {
    const matrix = encodeQr(caseOf("address").text);
    const path = qrSvgPath(matrix);
    const dark = matrix.flat().filter(Boolean).length;
    expect(path.split("M").length - 1).toBe(dark);
    expect(path.startsWith("M")).toBe(true);
  });

  it("draws inside the matrix, never outside it", () => {
    const matrix = encodeQr(caseOf("eip681").text);
    const size = matrix.length;
    const coords = [...qrSvgPath(matrix).matchAll(/M(\d+) (\d+)h1v1h-1z/g)].map((m) => [Number(m[1]), Number(m[2])]);
    expect(coords.length).toBeGreaterThan(0);
    for (const [x, y] of coords) {
      expect(x).toBeLessThan(size);
      expect(y).toBeLessThan(size);
    }
  });
});
