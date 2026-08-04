# Making the flywheel real — Base Sepolia runbook

Goal: run the Black_Wall → Traceipt flywheel with **real crypto** (real Ed25519
receipts, a real x402 facilitator, real on-chain settlement verification, a real
signed EIP-3009 payment) but on **Base Sepolia testnet**, so **no real money** is
at risk. This is the rehearsal that de-risks the mainnet flip.

What's already real (verified this session, no config needed):
- `settlement.py` `rpc` mode verifies **actual on-chain USDC transfers** on both
  Base mainnet and Base Sepolia — honest claim → ok, tampered amount / spoofed
  payee / fake tx → rejected.
- The two Cloudflare-403 `User-Agent` bugs that would have broken every real
  settlement and every facilitator call are fixed (commit `8b32fbc`).

The only value you must supply is a **receiving wallet address** (`RECEIPTS_PAY_TO`).
Only the address — never a private key.

---

## Side 1 — Traceipt (this repo): flip the live API to real mode

The non-secret real-mode vars now live in `render.yaml` (committed), including
`RECEIPTS_PAY_TO` (a public receiving address). The two SECRETS are declared
`sync: false` there and set in the Render dashboard's **Environment** tab. So on
deploy you only need to set the two secrets, below.

| Env var | Value | Where | Notes |
|---|---|---|---|
| `RECEIPTS_CHAIN` | `base-sepolia` | render.yaml | testnet |
| `RECEIPTS_SETTLEMENT` | `rpc` | render.yaml | verify real transfers, not mock |
| `RECEIPTS_RPC_URL` | `https://sepolia.base.org` | render.yaml | public node; works now that the UA bug is fixed. Swap for a provider endpoint if it rate-limits. |
| `RECEIPTS_MIN_CONFIRMATIONS` | `0` | render.yaml | testnet; set a few for mainnet |
| `X402_GATE` | `facilitator` | render.yaml | actually collect payment |
| `RECEIPTS_FACILITATOR_URL` | `https://facilitator.x402.rs` | render.yaml | supports base-sepolia, **no credentials** |
| `RECEIPTS_BIND_PAYER` | `1` | render.yaml | the wallet that pays must equal the settlement payer (closes payer-fraud gap) |
| `RECEIPTS_PAY_TO` | `0x3ec5…04e1` | render.yaml | public receiving address; receipts only mint for transfers paid TO it |
| `RECEIPTS_SELLER_ID` | `traceipt.xyz` | render.yaml | seller-hosted identity pinned into receipts |
| `RECEIPTS_PRICE_BASE_UNITS` | `2000` | render.yaml | $0.002 per `/attest` / `/receipts` call |
| `RECEIPTS_BASE_URL` | `https://api.traceipt.xyz` | render.yaml | verify links + QR resolve here |
| `RECEIPTS_KEY_PEM` | *(secret — see below)* | **dashboard** | durable signing key; mark **secret** |
| `RECEIPTS_ADMIN_TOKEN` | *(secret — random)* | **dashboard** | gates `/anchor` + `/credits` |

**Generate the durable signing key yourself** (never let it transit a chat or a
log — it is the root of trust for every receipt), then paste it into Render as the
secret `RECEIPTS_KEY_PEM`:

```sh
python3 -c "from traceipt.signing import Signer; print(Signer.generate().private_pem().decode())"
```

Save the matching public JWK for your records:

```sh
python3 -m traceipt.service --print-jwk
```

**Safety coupling (enforced in code, service.py:187):** `rpc` settlement is only
safe with `RECEIPTS_PAY_TO` set — otherwise a caller could claim a stranger's
transfer as "verified". Set the wallet in the same change you flip to `rpc`. Never
one without the other.

**Durability:** the free Render plan has no disk, so the ledger DB resets on
restart/idle (the `RECEIPTS_KEY_PEM` secret still survives, so re-issued receipts
verify). Fine for a **one-window live run**. For a durable, always-on flywheel that
Black_Wall can pull receipts from later, add a paid disk (`plan: starter`, disk at
`/data`, ~$7/mo — the block is already in `render.yaml`'s comments). Do the free run
first; add the disk once it works.

After setting the vars, redeploy and smoke-test:

```sh
curl -s https://api.traceipt.xyz/health          # {"ok":true,"gate":"facilitator","chain":"base-sepolia"}
curl -s https://api.traceipt.xyz/jwks.json        # your public key(s)
curl -i -s -X POST https://api.traceipt.xyz/attest \
  -H 'Content-Type: application/json' \
  -d '{"hash":"sha256:'"$(python3 -c "print('a'*64)")"'","type":"smoke"}'
# expect HTTP 402 with an accepts[] challenge (the gate is now charging)
```

A `402` on that last call is **success** — the paid gate is live.

---

## Side 2 — Black_Wall (branch `claude/blackwall-x402-integration-j3rdab`): the payer

The payer half runs from the Black_Wall session, which already has the client.

1. **A throwaway EVM wallet** — generate one, never reuse a real key. Export its
   key as `SIGNER_PRIVATE_KEY` (env only; never commit).
2. **Fund it on Base Sepolia:**
   - **Testnet USDC** (the asset transferred) from the Circle faucet —
     Base Sepolia USDC is `0x036CbD53842c5426634e7929541eC2318f3dCF7e`.
   - **A little Base Sepolia ETH** (safety net; the `exact` scheme is gasless).
3. **Anchor a real verdict** (integration A — the paid endpoint):

```sh
export SIGNER_PRIVATE_KEY=0x<throwaway-funded-key>
python clients/traceipt_anchor.py \
    --base-url https://api.traceipt.xyz \
    --network base-sepolia --rpc https://sepolia.base.org \
    --verdict '{"verdict":"STOP","score":0.0,"receipt_id":"bw_demo"}'
```

Expected: it POSTs `/attest`, gets `402`, signs an EIP-3009 authorization, the
facilitator settles the testnet-USDC payment, and Traceipt returns
`{ANCHORED: att_..., proof: https://api.traceipt.xyz/attest/att_.../proof}`. The
spend cap (`--max-atomic`, default $1) means a spoofed challenge can't overcharge.

That single command, returning an anchored attestation + a working proof URL, **is
the flywheel running for real.** Black_Wall paid Traceipt real (testnet) USDC and
got back a trustless, time-stamped proof of its verdict.

### Optional — close the loop (integration B, receipt → reputation)

To also exercise receipts feeding reputation, you need a real testnet payment made
*to* `RECEIPTS_PAY_TO` that Traceipt receipts via `POST /receipts`, then:

```sh
python traceipt_pull.py --base-url https://api.traceipt.xyz \
    --store rep.db --receipt-id <the_receipt_id>
```

It fetches the signed envelope, verifies the Ed25519 signature against
`/jwks.json` (fail-closed), and folds the authenticated payment into reputation.

---

## Recorded: FIRST MAINNET run — real money (2026-08-04)

The flywheel run for real, on **Base mainnet**, every link verified
independently against the chain. Real USDC moved; the Coinbase **CDP**
facilitator verified + settled the EIP-3009 payment; the root is on Base
mainnet. This supersedes the Sepolia run as the definitive reference.

| Link | Value |
|---|---|
| Payer | `0x3aec6fb2279D7Dd261482CC8BA9f2830f73A1A77` (dedicated gas+payer wallet) |
| Payment | **0.01 USDC on Base mainnet** (`eip155:8453`) → `RECEIPTS_PAY_TO` `0x3ec5…04e1`, via the **CDP facilitator** |
| Attestation | `att_b91e311aad05a268d8bf` (type `sanctions-verdict`), `status: anchored`, `durable: true` |
| Verdict | `GO` (offline-fixture screen) |
| verdict_digest (leaf) | `sha256:728c4733c730091d606cfc22368e7787249392fec898ad730f8d59a5396dcace` |
| Merkle root | `6d2b25d4f9701dba7d9b6bbe5cfc6a706d7b436a3db09e8c3b8c9cebfbce2985` |
| Sealed by | **immediate seal** (`RECEIPTS_ATTEST_SEAL=immediate`) — the full inclusion proof was returned **in the 201**, self-contained |
| On-chain tx | `0xab1c79b60a3ca3386eabc654bf163711140ac17a969e1fa526be8314da38821f` (base **mainnet**, **block 49517014, status success**) |
| Calldata | `TRACEIPT-ANCHOR\x01` + the exact root (0-value self-send from the gas wallet) |
| Gas | 22,920 gas; the anchor tx paid ~0.00000014 ETH — **CDP paid the USDC-transfer gas** (payer needs USDC, not ETH) |
| Money moved | payTo USDC `0.23205 → 0.24205` (+$0.01); payer USDC `2.88 → 2.87` |
| Proof | inclusion proof verifies OFFLINE (`verify_inclusion(0,1,leaf,[],root) → True`); `proof.root` == the on-chain calldata root |
| Basescan | https://basescan.org/tx/0xab1c79b60a3ca3386eabc654bf163711140ac17a969e1fa526be8314da38821f |

**What it proves:** the complete chain fired with real money — **CDP verified +
settled a real USDC payment → immediate seal → on-chain Merkle anchor on Base
mainnet → self-contained, independently-verifiable proof.** Unlike the Sepolia
run, the audit path was returned in the 201 (immediate seal), so the payer holds
a receipt that survives server amnesia — the paid-but-lost defect is closed on
mainnet without a disk.

**Getting here took three schema fixes** (the first real CDP call surfaced them):
the gate now surfaces the facilitator's real error rather than a generic reject,
and the payload forwarded to CDP carries a complete `accepted` PaymentRequirements
(with `maxTimeoutSeconds`) — the shape CDP's `x402V2PaymentPayload` validates
against. Reproduce with `tools/pay_attest.py` (or the zero-dependency
`tools/pay_attest_standalone.py`) against `https://api.traceipt.xyz`.

## Recorded: canonical on-chain run (2026-08-03)

The full loop, every link verified **independently against Base Sepolia**. This
is the definitive reference — durable signing key, locked admin, hands-free
sealing, and the Merkle root published on-chain.

| Link | Value |
|---|---|
| Payer (burner) | `0x7Fe662e89bFAA7FAc22891199F7128E434569794` |
| Payment | 0.002 USDC on base-sepolia → `RECEIPTS_PAY_TO` `0x3ec5…04e1` |
| Attestation | `att_452739d4d1db99521d23` (type `blackwall-verdict`, ref `bw_sanctions_demo_0005`) |
| Verdict | `STOP` (Black_Wall risk verdict) |
| verdict_digest (leaf) | `sha256:46dbf0f74410520340db947d0c20dfb23d8efd2da3a69e06cd7ec9f1382f673b` |
| Merkle root | `0205e6e2bf9e7961f7b241052f186203fe4db26f1069efcecd7f37b7251d5b85` |
| Sealed by | the **auto-anchor loop** (`RECEIPTS_ANCHOR_INTERVAL=300`) — no manual `/anchor` |
| On-chain tx | `0x1a9b1db1992d157ce1e0da6dc30d854fd0eaa99a524a1862b7838ba960848010` (base-sepolia, **block 44977609, status success**) |
| Calldata | `TRACEIPT-ANCHOR` + the exact root (0-value self-send from gas wallet `0x3aec…1A77`) |
| Gas | 22,920 gas; gas wallet 0.000100 → 0.0000998545 ETH |
| Proof | inclusion proof verifies OFFLINE; `proof.root == the on-chain root` |
| Basescan | https://sepolia.basescan.org/tx/0x1a9b1db1992d157ce1e0da6dc30d854fd0eaa99a524a1862b7838ba960848010 |

**What it proves:** a Black_Wall risk verdict (a *separate* codebase computing the
same `verdict_digest` byte-for-byte) became a **paid → bound → auto-sealed →
on-chain-timestamped → independently-verifiable** record. Existence-by-time is
provable against Base Sepolia, not just Traceipt's DB — so the "screened-before-
paid" ordering is trustless.

**Config live at the time of this run:** durable `RECEIPTS_KEY_PEM`
(kid `6927e09cfebdbb3c`), `RECEIPTS_ADMIN_TOKEN` (admin locked),
`RECEIPTS_ANCHOR_INTERVAL=300` (hands-free sealing), `RECEIPTS_PUBLISHER=onchain`
with a funded gas wallet.

**Remaining durability gap (honest):** the on-chain *root* is permanent, but the
*inclusion proof (audit path)* still lives in the disk-less DB — a redeploy keeps
the root forever but drops the per-leaf proof. Full durability = on-chain roots
**+ a persistent disk** (DEPLOY.md §2.A).

### Earlier run (pre-config, local-only anchor)

The first end-to-end run (`att_6d7f255926c6db8fdcc3`, ref `bw_sanctions_demo_0001`,
verdict `STOP` on the Tornado Cash OFAC address `0x8589…FDA16`, root
`02271b53…82a9`) proved the payment→bind→anchor→VC path *before* the production
config was in place. Everything the canonical run above resolves was a caveat
here: it used the pre-config signing key (its VC won't verify against the durable
key), its anchor was **local-only** (`onchain_tx` null), and it was sealed via a
**then-open** admin endpoint. Kept only as the record of the intermediate step.

---

## Then: the mainnet flip (later, real money)

Once the testnet run is green: switch `RECEIPTS_CHAIN=base`,
`RECEIPTS_RPC_URL=<provider mainnet endpoint>`, a mainnet-capable facilitator
(Coinbase CDP), fund the Black_Wall payer with real USDC, and add the paid disk.
Everything else is identical. See `DEPLOY.md` §2.
