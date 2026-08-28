# Audit / eval / verify — everything built this session

Adversarial pass over the session's new code, per the repo's standing practice.
Green tests were not treated as sufficient; each finding below was reproduced
before it was fixed.

## Findings

| # | Component | Finding | Severity | Status |
|---|---|---|---|---|
| 1 | `mcp_trust.build_index` | Grade precedence used `_GRADES.index()`, ranking by how much we KNOW rather than how much it MATTERS. `UNREACHABLE` outranked `MISMATCH`, so **a stale dead registry row on the same host silently masked a lying one** and the gate did not fire. | **HIGH** | Fixed |
| 2 | `x402_challenge.to_accepts` | The challenge is written by an untrusted third-party server. A `recipient` of `{"evil": 1}` propagated a **dict** into the `payTo` slot, and `"NOT_AN_ADDRESS"` propagated as a payee. `discovery_crawl` validated downstream; `traceipt_attest` and `clients/x402_pay` did **not**. | **MEDIUM-HIGH** | Fixed |
| 3 | `token_decimals.decode_decimals` | A contract returning 32 zero bytes reads as `0` decimals. Legitimate for a 0-decimal token; wrong for a non-token that returns zeros. | LOW | Documented |
| 4 | `x402_challenge.parse_params` | Checked for regex blowup on a hostile header. 200 KB parsed in <1 ms. | — | No issue |
| 5 | `wallet_guard` → `forecast` | Checked whether the new `amount_unverified` key breaks request validation. It does not. | — | No issue |

## 1 — the masking bug, reproduced

```
host serving:  one MISMATCH entry  +  one UNREACHABLE entry
  grade  -> unreachable
  gates? -> False        <-- the lie is invisible
```

`_GRADES` is ordered `(READY, MISMATCH, EMPTY, UNREACHABLE, UNKNOWN)` — an
ordering that reads naturally but encodes the wrong priority. Replaced with an
explicit `_PRECEDENCE` map in which **every gating grade outranks every
non-gating one**, guarded by a test that asserts exactly that property so a
future reordering cannot reintroduce it.

Between the non-gating grades, a host demonstrably serving real tools is `READY`;
a dead sibling entry is a stale registry row, not evidence the host is down.

**Measured effect on the real corpus** (9,968 hosts):

| Grade | Before | After |
|---|---|---|
| ready | 8,089 | **8,122** |
| unreachable | 1,810 | **1,776** |
| empty | 45 | **46** |
| mismatch | 24 | 24 |

34 hosts were being reported unreachable while demonstrably serving tools, and
one `empty` host was masked. The mismatch set is unchanged — the fix corrects
masking without inventing new flags.

## 2 — untrusted input, reproduced

```
recipient "NOT_AN_ADDRESS" -> payTo='NOT_AN_ADDRESS'
recipient {"evil": 1}      -> payTo={'evil': 1}
```

Validation now happens at the parse boundary rather than relying on each consumer
to re-check: a non-42-char, non-hex, or non-string recipient rejects the whole
challenge; a non-scalar amount rejects it; a non-integer `chainId` no longer
forges an `eip155:` network string; a non-integer `decimals` is dropped rather
than reaching `10**n`.

## 3 — documented, not fixed

`decimals()` returning 32 zero bytes is indistinguishable from a genuine
0-decimal token. Rejecting `0` would break legitimate tokens; accepting it risks
treating atomic units as human for a contract that merely returns zeros. A second
`symbol()` call would disambiguate at double the query cost and double the leak.
Left as a documented residual, bounded by the fact that a non-contract address
returns `0x` (already handled) and a contract without `decimals()` reverts.

## Verification

**Live, against real endpoints** — all 5 previously-recovered v2 hosts still
parse after the hardening, each returning a valid address:

```
api.onesource.io                hdr_accepts  payTo=0x19B8e99079A5
api.webbersites.com             hdr_accepts  payTo=0xdd5fcEa81CA6
stable-financial.vercel.app     hdr_accepts  payTo=0xBD17480bf6ff
stable-travel...vercel.app      hdr_accepts  payTo=0xDd257723b86B
x402.donnyautomation.com        hdr_accepts  payTo=0x2740651e046c
```

No false rejections from the new validation.

**Full suite:** 945 tests in the root suites, 44 in `integrations/wallets`, 58 in
`mcp_history`, 30 in `ecosystem_history`. All green.

## Still open, honestly

- `settlement_sim._base_units` keeps a 6-decimal default. LOW: that gate is
  HOLD-only, never STOPs, and its underfunded result is explicitly non-gating.
- `KNOWN_DECIMALS` plus the on-chain reader cover EVM only. A Solana mint is
  never queried (`eth_call` cannot answer), so non-EVM assets still resolve to
  unknown — safe, not verified.
- Multiple `WWW-Authenticate` headers collapse to one via `dict(headers.items())`.
  Not observed in the wild across 195 probed hosts, but it is an assumption.
- The MCP trust index is built from a committed reading, so it ages. Reading #2
  (2026-09-27) refreshes it; between readings a payee's grade can be stale.
