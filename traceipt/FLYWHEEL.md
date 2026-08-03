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

## Recorded: first live run (2026-08-03)

The loop turned end-to-end on Base Sepolia with a real x402 payment. Every link
below was verified independently (the digest recomputed, the Merkle proof checked
offline). Kept as a concrete reference.

| Link | Value |
|---|---|
| Payer (burner) | `0x7Fe662e89bFAA7FAc22891199F7128E434569794` |
| Payment | 0.002 USDC on base-sepolia (burner 1.000000 → 0.998000; received at `RECEIPTS_PAY_TO` `0x3ec5…04e1`) |
| Attestation | `att_6d7f255926c6db8fdcc3` (type `blackwall-verdict`, ref `bw_sanctions_demo_0001`) |
| Verdict | `STOP` on counterparty `0x8589427373D6D84E98730D7795D8f6f8731FDA16` (Tornado Cash, OFAC-listed) |
| verdict_digest | `sha256:1f3e3a2f47a0146364409b02c19307c6cf584b62bf6efc50485ccdc6ed5f7141` |
| Merkle anchor | `anchor_id 1`, root `02271b5399b092e5ff4cfe20e9594e24394f9af628af795311f00f0e0f4682a9` |
| Proof | inclusion proof verifies OFFLINE; leaf == verdict_digest |
| Credential | `AnchoredAttestationCredential` (W3C VC) at `/attest/att_6d7f…/vc` |

**The point it proves:** Black_Wall (a separate codebase) and Traceipt computed
the *same* `verdict_digest` byte-for-byte, and the server stored exactly that
hash — so a risk verdict was bound, anchored, and made independently verifiable
on a real payment.

**Honest caveats (do not overclaim this run):**
- It was issued under the **pre-configuration signing key**. The durable
  `RECEIPTS_KEY_PEM` was set afterward (kid `6927e09cfebdbb3c`), so this run's VC
  does **not** verify against the current issuer key. Runs after that use the
  stable key.
- `onchain_tx` is **null** — the Merkle root was recorded locally, not published
  to a chain. The inclusion proof is valid *relative to that root*, but trustless
  existence-by-time needs the on-chain publisher (DEPLOY.md §2.F).
- The batch was sealed via an **open** admin endpoint (the admin token was not
  yet set). Admin is now locked (`RECEIPTS_ADMIN_TOKEN`) and sealing is hands-free
  (`RECEIPTS_ANCHOR_INTERVAL=300`), so later runs need no manual `POST /anchor`.

---

## Then: the mainnet flip (later, real money)

Once the testnet run is green: switch `RECEIPTS_CHAIN=base`,
`RECEIPTS_RPC_URL=<provider mainnet endpoint>`, a mainnet-capable facilitator
(Coinbase CDP), fund the Black_Wall payer with real USDC, and add the paid disk.
Everything else is identical. See `DEPLOY.md` §2.
