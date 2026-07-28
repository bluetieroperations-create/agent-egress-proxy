#!/usr/bin/env python3
"""
keccak.py -- Keccak-256 (Ethereum's hash), pure-Python, stdlib-only.

Ethereum uses ORIGINAL Keccak, which differs from FIPS-202 SHA3-256 ONLY in the
domain-separation/padding byte (Keccak pads with 0x01, SHA3 with 0x06). So
`hashlib.sha3_256` is NOT a substitute -- it produces different digests. This is a
faithful Keccak-f[1600] sponge with the Keccak (0x01) pad, needed for EIP-712
hashing and Ethereum address derivation in eip712.py / secp256k1 recovery.

Verified against the canonical vector keccak256("") =
c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470.
"""
from __future__ import annotations

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
# Rotation offsets r[x][y], per the Keccak spec (rho step).
_ROT = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]
_MASK = (1 << 64) - 1


def _rotl(x, n):
    return ((x << n) | (x >> (64 - n))) & _MASK


def _keccak_f(state):
    """In-place Keccak-f[1600] permutation on a 5x5 array of 64-bit lanes."""
    for rnd in range(24):
        # theta
        C = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4]
             for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rotl(C[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x][y] ^= D[x]
        # rho + pi
        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rotl(state[x][y], _ROT[x][y])
        # chi
        for x in range(5):
            for y in range(5):
                state[x][y] = B[x][y] ^ ((~B[(x + 1) % 5][y]) & B[(x + 2) % 5][y])
        # iota
        state[0][0] ^= _RC[rnd]
    return state


def keccak256(data: bytes) -> bytes:
    """Keccak-256 digest (32 bytes) of `data`."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("keccak256 expects bytes")
    rate = 136                      # 1088-bit rate for 256-bit output (200 - 2*32)
    # --- pad (Keccak pad10*1 with the 0x01 domain byte) ---
    msg = bytearray(data)
    msg.append(0x01)
    while len(msg) % rate != 0:
        msg.append(0x00)
    msg[-1] ^= 0x80
    # --- absorb ---
    state = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        block = msg[off:off + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:i * 8 + 8], "little")
            state[i % 5][i // 5] ^= lane
        _keccak_f(state)
    # --- squeeze (one block suffices for 256-bit output) ---
    out = bytearray()
    for i in range(4):              # 4 lanes * 8 bytes = 32 bytes
        out += (state[i % 5][i // 5]).to_bytes(8, "little")
    return bytes(out)
