"""
Dependency-free QR Code encoder (byte mode, ECC level M, versions 1-6).

Enough to encode a verify URL onto the invoice so a human can scan straight
to the /verify page. Version 6 holds ~106 bytes — far more than any verify
URL — so the range is capped at 6, which is exactly the range validated
byte-for-byte against the `segno` reference library in the test suite (used
as an oracle, per version and per mask). `segno` is a TEST-ONLY dependency
and is never imported at runtime. (Versions 7+, whose alignment patterns
cross the timing lines, are deliberately out of scope; data that would need
them raises rather than emitting an unverified symbol.)

Public API:
    encode(data: str) -> list[list[int]]   # square matrix of 0/1 (1 = dark)
"""
from __future__ import annotations

# --- GF(256) arithmetic (primitive polynomial 0x11d) ----------------------
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11d
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(n):
    g = [1]
    for i in range(n):
        g2 = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            g2[j] ^= c
            g2[j + 1] ^= _gf_mul(c, _EXP[i])
        g = g2
    return g


def _rs_encode(data, n):
    gen = _rs_generator(n)
    res = list(data) + [0] * n
    for i in range(len(data)):
        coef = res[i]
        if coef != 0:
            for j in range(len(gen)):
                res[i + j] ^= _gf_mul(gen[j], coef)
    return res[len(data):]


# --- ECC block structure, level M, versions 1..10 -------------------------
# version: (ec_per_block, [(num_blocks, data_codewords_per_block), ...])
_ECC_M = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
}
_MAX_VERSION = 6
# alignment pattern centre coordinates per version
_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
}
# format-info bit strings (level M) for masks 0..7, 15 bits each
_FORMAT_M = {
    0: "101010000010010", 1: "101000100100101", 2: "101111001111100",
    3: "101101101001011", 4: "100010111111001", 5: "100000011001110",
    6: "100111110010111", 7: "100101010100000",
}


def _data_capacity(version):
    ec, groups = _ECC_M[version]
    return sum(n * d for n, d in groups)


def _choose_version(nbytes):
    for v in range(1, _MAX_VERSION + 1):
        overhead = (4 + 8 + 7) // 8  # mode + 8-bit count (versions 1-9)
        if nbytes + overhead <= _data_capacity(v):
            return v
    raise ValueError(f"data too long for QR versions <= {_MAX_VERSION} "
                     "(level M); shorten the URL")


def _encode_data_codewords(data: bytes, version: int):
    count_bits = 8 if version <= 9 else 16
    bits = "0100"  # byte mode
    bits += format(len(data), f"0{count_bits}b")
    for b in data:
        bits += format(b, "08b")
    cap = _data_capacity(version) * 8
    bits += "0" * min(4, cap - len(bits))           # terminator (<=4 bits)
    # Padding bits to the next codeword boundary. Per ISO/IEC 18004 §7.4.10
    # (and matching the segno reference), this advances to a full byte even
    # when already aligned.
    bits += "0" * (8 - len(bits) % 8)
    pads = ["11101100", "00010001"]
    i = 0
    while len(bits) < cap:
        bits += pads[i % 2]
        i += 1
    return [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]


def _interleave(data_codewords, version):
    ec_per, groups = _ECC_M[version]
    blocks = []
    idx = 0
    for num, dper in groups:
        for _ in range(num):
            d = data_codewords[idx:idx + dper]
            idx += dper
            blocks.append((d, _rs_encode(d, ec_per)))
    out = []
    maxd = max(len(d) for d, _ in blocks)
    for i in range(maxd):
        for d, _ in blocks:
            if i < len(d):
                out.append(d[i])
    for i in range(ec_per):
        for _, e in blocks:
            out.append(e[i])
    return out


# --- matrix construction --------------------------------------------------
def _new_matrix(size):
    return [[None] * size for _ in range(size)]


def _place_finder(m, r, c):
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            rr, cc = r + dr, c + dc
            if 0 <= rr < len(m) and 0 <= cc < len(m):
                if 0 <= dr <= 6 and 0 <= dc <= 6:
                    edge = dr in (0, 6) or dc in (0, 6)
                    inner = 2 <= dr <= 4 and 2 <= dc <= 4
                    m[rr][cc] = 1 if (edge or inner) else 0
                else:
                    m[rr][cc] = 0  # separator


def _place_alignment(m, version):
    centres = _ALIGN[version]
    size = len(m)
    for r in centres:
        for c in centres:
            # skip if overlapping a finder pattern
            if (r < 8 and c < 8) or (r < 8 and c > size - 9) or \
               (r > size - 9 and c < 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    ring = max(abs(dr), abs(dc))
                    m[r + dr][c + dc] = 1 if ring in (0, 2) else 0


def _reserved(m, version):
    """A boolean grid: True where a function pattern lives (never data)."""
    size = len(m)
    res = [[False] * size for _ in range(size)]
    for r in range(size):
        for c in range(size):
            if m[r][c] is not None:
                res[r][c] = True
    # format-info areas
    for i in range(9):
        res[8][i] = True
        res[i][8] = True
    for i in range(8):
        res[8][size - 1 - i] = True
        res[size - 1 - i][8] = True
    return res


def _place_function_patterns(m, version):
    size = len(m)
    _place_finder(m, 0, 0)
    _place_finder(m, 0, size - 7)
    _place_finder(m, size - 7, 0)
    # timing patterns
    for i in range(size):
        if m[6][i] is None:
            m[6][i] = 1 if i % 2 == 0 else 0
        if m[i][6] is None:
            m[i][6] = 1 if i % 2 == 0 else 0
    _place_alignment(m, version)
    m[size - 8][8] = 1  # dark module


def _mask_fn(k):
    return [
        lambda r, c: (r + c) % 2 == 0,
        lambda r, c: r % 2 == 0,
        lambda r, c: c % 3 == 0,
        lambda r, c: (r + c) % 3 == 0,
        lambda r, c: (r // 2 + c // 3) % 2 == 0,
        lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
        lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
        lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
    ][k]


def _place_data(m, res, bits):
    """Zig-zag module placement per ISO/IEC 18004 §7.7.3 (two-module-wide
    columns, right to left, alternating up/down; the direction formula
    matches the spec's alternative method)."""
    size = len(m)
    idx = 0
    for right in range(size - 1, 0, -2):
        if right <= 6:
            right -= 1
        for vertical in range(size):
            for z in range(2):
                j = right - z
                upwards = ((right & 2) == 0) ^ (j < 6)
                i = (size - 1 - vertical) if upwards else vertical
                if not res[i][j]:
                    m[i][j] = bits[idx] if idx < len(bits) else 0
                    idx += 1


def _apply_mask(m, res, k):
    fn = _mask_fn(k)
    out = [row[:] for row in m]
    for r in range(len(m)):
        for c in range(len(m)):
            if not res[r][c] and fn(r, c):
                out[r][c] ^= 1
    return out


def _place_format(m, k):
    size = len(m)
    bitsf = _FORMAT_M[k]
    coords1 = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
               (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    for (r, c), b in zip(coords1, bitsf):
        m[r][c] = int(b)
    coords2 = [(size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
               (size - 5, 8), (size - 6, 8), (size - 7, 8),
               (8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5),
               (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1)]
    for (r, c), b in zip(coords2, bitsf):
        m[r][c] = int(b)


def _penalty(m):
    size = len(m)
    score = 0
    # rule 1: runs of 5+ same colour in rows and columns
    for line in list(m) + [list(col) for col in zip(*m)]:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    # rule 2: 2x2 blocks
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    # rule 3: finder-like patterns
    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for line in list(m) + [list(col) for col in zip(*m)]:
        for i in range(size - 10):
            seg = list(line[i:i + 11])
            if seg == pat1 or seg == pat2:
                score += 40
    # rule 4: dark-module balance
    dark = sum(sum(row) for row in m)
    ratio = dark * 100 // (size * size)
    score += 10 * min(abs(ratio - 50) // 5, abs((ratio) - 50) // 5)
    return score


def encode(data: str) -> list[list[int]]:
    raw = data.encode("utf-8")
    version = _choose_version(len(raw))
    codewords = _interleave(_encode_data_codewords(raw, version), version)
    bits = []
    for cw in codewords:
        bits.extend(int(b) for b in format(cw, "08b"))

    size = 17 + 4 * version
    base = _new_matrix(size)
    _place_function_patterns(base, version)
    res = _reserved(base, version)
    _place_data(base, res, bits)

    best = None
    for k in range(8):
        cand = _apply_mask(base, res, k)
        _place_format(cand, k)
        pen = _penalty(cand)
        if best is None or pen < best[0]:
            best = (pen, cand)
    return [[0 if v is None else v for v in row] for row in best[1]]
