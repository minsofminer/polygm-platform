#!/usr/bin/env python3
"""Rebuild `web/src/tma/qr.fixtures.json` — the reference matrices `qr.test.ts` compares against.

`web/src/tma/qr.ts` is a QR encoder with no dependency, and the only honest way to test one is against another
implementation. This script is that other implementation: **segno**, at error-correction level M, byte mode, no
border, symbol boost disabled.

Four details, each of which changed the file:

**Byte mode is forced.** Placing data with a library that optimises picks the smallest mode, and for a hex deposit
address that is *alphanumeric* — a real 29-module QR, and not the one our encoder builds. The address a wallet
renders can be mixed case, so byte mode is the mode the Mini App must get right.

**The version is not pinned.** Each case carries the version the reference chose, and a fixture whose size changed
would be caught by `--check` rather than quietly testing a different QR.

**segno's terminator padding is patched out.** `segno.encoder.write_padding_bits` appends `8 - (length % 8)` zero
bits, which is a whole extra codeword when the bit stream already ends on a codeword boundary; ISO/IEC 18004
§7.4.10 pads only as far as the boundary, and only if it is not there already. It is invisible to a decoder — pad
codewords are ignored — but it moved 24 codewords and the whole error-correction tail on two of these three cases,
so a fixture built from unpatched segno pins a symbol no other encoder produces. `spec_write_padding_bits` below is
the correction, and `python-qrcode` is used as a third opinion: every fixture's codeword stream must equal
`qrcode.util.create_data`'s, byte for byte.

**segno is still the mask authority, and `qrcode` is not.** The two disagree about *which* of the eight legal masks
to pick, because they score the candidates in different states: `qrcode` draws the candidate's format information
before scoring, while segno evaluates the data region with the format and version areas still unwritten — which is
what ISO/IEC 18004 §7.8 asks for ("the data mask pattern which results in the lowest penalty score shall be
selected"), and what `qr.ts` does. A fixture generated from the other convention would pin us to a mask the
standard's own scoring rule does not choose.

Every generated matrix is then **read back** — format information, codeword walk, de-interleave, Reed-Solomon
syndromes, payload — because a fixture is only worth comparing against if it is a real QR. The read-back is
deliberately written from the standard rather than from either library; it is what caught the padding deviation.

    python3 tools/build-qr-fixtures.py            # write the file
    python3 tools/build-qr-fixtures.py --check    # exit 1 if the file on disk differs (the gate runs this)

Requires `segno` and `qrcode` (`pip install segno qrcode`), *test* dependencies of the web suite and never runtime
ones. The Mini App ships `qr.ts` and nothing else.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "src" / "tma" / "qr.fixtures.json"

#: name -> the text to encode. Each case is chosen for what it exercises in the encoder:
#: version 3 / one full block with no pad codewords, version 4 / two blocks, version 8 / four unequal blocks with
#: the version header. The first two are the two that unpatched segno got wrong.
CASES: dict[str, str] = {
    "address": "0x1111111111111111111111111111111111111111",
    "eip681": "ethereum:0x1234567890abcdef1234567890abcdef12345678@137",
    "long": ("polygm:deposit?chain=polygon&asset=USDC&amount=250.00&address="
             "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01&memo=dep-8f2c1a4e-77b1-4c9e-9a2d-0f5e6d7c8b91"),
}

# ----------------------------------------------------------------------------------------------- the reader
# Everything below is written from ISO/IEC 18004 rather than from segno or qrcode: it is the check on both.

_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38],
          8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
#: At level M: (total codewords, data codewords) per block, versions 1-10 — the same table `qr.ts` ships.
_BLOCKS = {1: [(26, 16)], 2: [(44, 28)], 3: [(70, 44)], 4: [(50, 32), (50, 32)], 5: [(67, 43), (67, 43)],
           6: [(43, 27)] * 4, 7: [(49, 31)] * 4, 8: [(60, 38), (60, 38), (61, 39), (61, 39)],
           9: [(58, 36), (58, 36), (58, 36), (59, 37), (59, 37)], 10: [(69, 43)] * 4 + [(70, 44)]}

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[(_LOG[a] + _LOG[b]) % 255]


def _syndromes(words: list[int], ec_len: int) -> list[int]:
    """R(alpha^i) for i = 0..ec_len-1 must all be zero: the generator's roots, per ISO/IEC 18004 annex A."""
    out = []
    for i in range(ec_len):
        a = _EXP[i]
        acc = 0
        for w in words:
            acc = _mul(acc, a) ^ w
        out.append(acc)
    return out


def _mask_bit(mask: int, row: int, col: int) -> bool:
    return [(row + col) % 2 == 0, row % 2 == 0, col % 3 == 0, (row + col) % 3 == 0,
            (row // 2 + col // 3) % 2 == 0, (row * col) % 2 + (row * col) % 3 == 0,
            ((row * col) % 2 + (row * col) % 3) % 2 == 0,
            ((row * col) % 3 + (row + col) % 2) % 2 == 0][mask]


def _read_format(rows: list[str], size: int) -> tuple[int, int]:
    """The level and mask recorded in the format information, read LSB-first in the order it is written."""
    import qrcode.util as qu
    pos = [((i, 8) if i < 6 else ((i + 1, 8) if i < 8 else (size - 15 + i, 8))) for i in range(15)]
    word = sum(int(rows[r][c]) << i for i, (r, c) in enumerate(pos))
    for dec in range(32):
        if qu.BCH_type_info(dec) == word:
            return dec >> 3, dec & 7
    raise SystemExit("the format information in a fixture decoded to no legal word")


def _free(version: int, size: int) -> list[list[bool]]:
    """Which modules the data walk skips: finders and separators, timing, alignment, dark module, headers."""
    fn = [[False] * size for _ in range(size)]

    def mark(row: int, col: int) -> None:
        if 0 <= row < size and 0 <= col < size:
            fn[row][col] = True

    for orow, ocol in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                mark(orow + dr, ocol + dc)
    for i in range(8, size - 8):
        mark(6, i)
        mark(i, 6)
    for row in _ALIGN[version]:
        for col in _ALIGN[version]:
            if (row <= 8 and col <= 8) or (row <= 8 and col >= size - 9) or (row >= size - 9 and col <= 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    mark(row + dr, col + dc)
    mark(size - 8, 8)
    for i in range(9):
        mark(8, i)
        mark(i, 8)
    for i in range(8):
        mark(8, size - 1 - i)
        mark(size - 1 - i, 8)
    if version >= 7:
        for i in range(6):
            for j in range(3):
                mark(size - 11 + j, i)
                mark(i, size - 11 + j)
    return fn


def read_back(rows: list[str], version: int) -> dict:
    """Decode a matrix: the level and mask it records, its codeword blocks, and the text they carry."""
    size = len(rows)
    fn = _free(version, size)
    level, mask = _read_format(rows, size)
    bits: list[int] = []
    right, upward = size - 1, True
    while right > 0:
        if right == 6:
            right = 5
        for v in range(size):
            row = size - 1 - v if upward else v
            for col in (right, right - 1):
                if col >= 0 and not fn[row][col]:
                    bits.append(int(rows[row][col]) ^ (1 if _mask_bit(mask, row, col) else 0))
        upward = not upward
        right -= 2
    words = [int("".join(str(b) for b in bits[i * 8:(i + 1) * 8]), 2) for i in range(len(bits) // 8)]
    structure = _BLOCKS[version]
    blocks = [[0] * total for total, _ in structure]
    counts = [data for _, data in structure]
    at = 0
    for i in range(max(counts)):
        for b, data in enumerate(counts):
            if i < data:
                blocks[b][i] = words[at]
                at += 1
    for i in range(max(total - data for total, data in structure)):
        for b, (total, data) in enumerate(structure):
            if i < total - data:
                blocks[b][data + i] = words[at]
                at += 1
    clean = all(not any(_syndromes(blocks[b], total - data)) for b, (total, data) in enumerate(structure))
    stream: list[int] = []
    for b, (_, data) in enumerate(structure):
        stream.extend(blocks[b][:data])
    bitstr = "".join(format(w, "08b") for w in stream)
    count_bits = 8 if version <= 9 else 16
    length = int(bitstr[4:4 + count_bits], 2)
    payload = bytes(int(bitstr[4 + count_bits + i * 8:12 + count_bits + i * 8], 2) for i in range(length))
    # The two-bit level field is not an index: 00 is level M and 01 is L (ISO/IEC 18004 table 25).
    return {"ecc": {0: "M", 1: "L", 2: "H", 3: "Q"}[level], "mask": mask, "rs_clean": clean,
            "mode": int(bitstr[:4], 2), "text": payload.decode("utf-8")}


# ----------------------------------------------------------------------------------------------- the reference
def _patch_padding(encoder, consts) -> None:
    """segno's one deviation from §7.4.10, replaced with the rule: pad to the boundary, not past it."""

    def spec_write_padding_bits(buff, version, length):
        if version not in (consts.VERSION_M1, consts.VERSION_M3):
            remainder = (-length) % 8
            if remainder:
                buff.extend([0] * remainder)

    encoder.write_padding_bits = spec_write_padding_bits


def build() -> dict:
    try:
        import qrcode
        import segno
        from qrcode.util import MODE_8BIT_BYTE, QRData, create_data
        from segno import consts, encoder
    except ImportError as exc:                                     # pragma: no cover - environment, not logic
        raise SystemExit("this script needs the reference encoders: pip install segno qrcode (%s)" % exc)
    _patch_padding(encoder, consts)
    out: dict[str, dict] = {}
    for name, text in CASES.items():
        # Capture the bit stream segno is about to place, so the codewords themselves can be checked against a
        # third implementation rather than only the cells they end up in.
        captured: list[int] = []
        real_place = encoder.add_codewords

        def spy(matrix, codewords, version, _real=real_place, _into=captured):
            _into.extend(int(b) for b in codewords)
            return _real(matrix, codewords, version)

        encoder.add_codewords = spy
        try:
            qr = segno.make(text, error="m", mode="byte", boost_error=False, micro=False)
        finally:
            encoder.add_codewords = real_place
        if qr.mask is None:                                        # pragma: no cover - segno always reports one
            raise SystemExit("segno did not report a mask for %s, so the fixture could not be pinned" % name)
        rows = ["".join(str(int(cell)) for cell in row) for row in qr.matrix]
        case = {"text": text, "version": qr.version, "mask": qr.mask,
                "size": qr.symbol_size(border=0)[0], "rows": rows}
        back = read_back(rows, qr.version)
        if back["text"] != text or not back["rs_clean"] or back["mask"] != qr.mask or back["ecc"] != "M":
            raise SystemExit("the reference produced a matrix that does not read back as itself: %s (%s)"
                             % (name, {k: v for k, v in back.items() if k != "text"}))
        ours = [int("".join(str(b) for b in captured[i * 8:(i + 1) * 8]), 2) for i in range(len(captured) // 8)]
        theirs = create_data(qr.version, qrcode.constants.ERROR_CORRECT_M, [QRData(text.encode(), mode=MODE_8BIT_BYTE)])
        if ours != list(theirs):
            raise SystemExit("segno and qrcode disagree about %s's codewords, so neither can be the fixture" % name)
        out[name] = case
    return out


def main(argv: list[str]) -> int:
    fresh = json.dumps(build(), indent=1) + "\n"
    if "--check" in argv:
        on_disk = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if on_disk != fresh:
            print("FAIL qr.fixtures.json is stale — run tools/build-qr-fixtures.py")
            return 1
        print("PASS qr fixtures are current (%d cases)" % len(CASES))
        return 0
    OUT.write_text(fresh, encoding="utf-8")
    sizes = {name: "%dx%d v%d mask %d" % (case["size"], case["size"], case["version"], case["mask"])
             for name, case in build().items()}
    print("wrote %s — %s" % (OUT.relative_to(ROOT), sizes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
