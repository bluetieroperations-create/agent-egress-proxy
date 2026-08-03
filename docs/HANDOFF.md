# HANDOFF — session & branch coordination (read before you push)

**Purpose:** stop two parallel sessions from duplicating work, cross-merging two
separate projects, or overwriting each other's pushes. A same-branch overwrite has
already happened here once — this doc is how we don't repeat it.

_Last updated at commit `806db8d` on `claude/blackwall-x402-integration-j3rdab`._

---

## TL;DR — the rules

1. **Two projects live in this ONE repo as TWO branches. Do not merge them.**
   - `claude/blackwall-x402-integration-j3rdab` → **Blackwall** (the payment-verdict
     engine + integrations). Lives at the **repo root**. ← this session.
   - `claude/x402-product-ideas-6adgah` → **Traceipt** (signed receipts). Lives in
     the **`traceipt/` directory**.
   - `main` is (currently) bare of both — neither project is merged to it yet.
2. **One branch, one active writer.** If another session must touch a branch that a
   session is already working, `git fetch` + **rebase onto the remote tip before you
   push**, and verify `local HEAD == remote HEAD` **after** pushing. (The earlier
   collision was a blind push that got rejected non-fast-forward.)
3. **Don't rebuild what's already built.** Check the inventory below first.
4. **The `traceipt_*.py` files are a shared SEAM** — they live on the Blackwall
   branch but depend on Traceipt's schema/endpoints. Coordinate changes (see §4).
5. **Core stays stdlib-only.** `eth-account`, `langchain-core`, `cryptography`,
   `eth_abi` are **test/adapter-only** and always `skipUnless`-gated — never import
   them from a core module.
6. **Run the full suite before pushing** (command in `CLAUDE.md`).

---

## 1. Who owns what

| Branch | Project | Where | Owns |
|---|---|---|---|
| `claude/blackwall-x402-integration-j3rdab` | **Blackwall** | repo root + `integrations/`, `clients/`, `docs/` | the verdict engine and everything in the inventory below |
| `claude/x402-product-ideas-6adgah` | **Traceipt** | `traceipt/` | signed-receipt service: `traceipt/traceipt/{signing,schema,canonical,service,ledger}.py` |

Blackwall does **not** edit `traceipt/`. Traceipt does **not** edit the repo root.
Keep them on separate branches; do not open a PR that merges one into the other.

## 2. Already built on the Blackwall branch — DO NOT rebuild

Grouped; each has a `test_*.py` and passes (632 core + integration tests green).

**Verdict engine & signals**
- `blackwall.py` — the GO/HOLD/STOP engine (`decide_payment`, `forecast`, HTTP server).
- `sanctions.py`, `readiness.py`, `reputation_store.py`, `reputation_onchain.py`,
  `ledger.py`, `settlement_watch.py`, `addresses.py`, `discovery.py`.
- `x402.py` — Blackwall's own x402 billing (v2, CDP facilitator), `cdp_auth.py`.
- `ap_gate.py` — treasury/AP payout gate.
- `seller_audit.py` — earned "verified merchant" tier (`SellerRegistry`, bounded
  trust floor, folds into `decide_payment` via `verified_floor`).

**Payload simulation (all 3 phases) + the pure-Python crypto behind it**
- `payload_sim.py` — cross-check the signed payment vs the claim (Phase 1 fields,
  Phase 2 signer recovery). `calldata.py` — Phase 3 contract-call drainer screen.
- `keccak.py`, `secp256k1.py`, `eip712.py` — stdlib crypto (cross-checked vs
  `eth_abi`/`eth-account`). **If you need Ethereum hashing/recovery, use these — do
  not add a new crypto module or a hard dependency.**

**AA co-signing** — `aa_cosigner.py` (off-chain half: v0.7 `userOpHash`, `execute`
decode + screen, sign-on-GO/withhold-on-STOP, fail-open/closed). On-chain validator
is NOT built (see `docs/AA_COSIGNING.md`).

**Traceipt bridge (the seam — see §4)** — `traceipt_attest.py`, `traceipt_ingest.py`,
`traceipt_verify.py`, `traceipt_pull.py`.

**Integrations** (self-contained, own tests run from their dir)
- `integrations/langchain/` — LangChain guard (tool + guardrail callback).
- `integrations/wallets/` — wallet signing-guard: `wallet_guard.py` core +
  `turnkey_signer.py` / `privy_signer.py`. Availability toggle
  (`FAIL_CLOSED`/`FAIL_OPEN`, runtime-flippable), `describe_policy()` +
  `customer_message()` customer-facing copy.
- `clients/` — `x402_pay.py`, `traceipt_anchor.py` (dep: `eth-account`, test-only).

**Customer-facing docs** — `docs/TRANSPARENCY.md`, `docs/AA_COSIGNING.md`,
`docs/STRATEGY_REVIEW.md` (ranks & tracks the roadmap: payload-sim ✅, seller-audit
✅, LangChain ✅, AA co-sign off-chain ✅).

## 3. What is NOT built (safe to pick up — but claim it here first)

- The **on-chain** ERC-7579 validator + key infra for AA co-signing (testnet first).
- A **live end-to-end run** against real Traceipt receipt volume / real x402 traffic.
- More framework/wallet adapters (CrewAI, Vercel, Fireblocks, Dynamic) — same
  pattern as the shipped ones.
- RLP-decoding in `wallet_guard.claim_from_tx` (today it expects a tx object).

## 4. The shared seam — coordinate these

`traceipt_attest.py` / `traceipt_ingest.py` / `traceipt_verify.py` /
`traceipt_pull.py` live on the **Blackwall** branch but consume **Traceipt's**
contract:
- receipt **envelope** `{protected, payload, signature}` and the receipt payload
  (`kind`, `settlement{payee,payer,tx_hash,amount_base_units,verified,...}`,
  `issued_at`);
- **JWKS** at `GET /jwks.json` (Ed25519, `OKP`/`kid`);
- endpoints `POST /attest`, `GET /receipts/{id}`.

**If the Traceipt session changes the receipt schema, the JWKS shape, or those
endpoints, it breaks these four files.** Ping the Blackwall side before/after such a
change so the bridge is updated in lockstep. (`traceipt_verify.py` mirrors
`traceipt/traceipt/signing.py::verify_envelope` byte-for-byte — the canonical JSON
uses `ensure_ascii=False`; keep them identical.)

**Open proposal for Traceipt → tag receipts with service category.** `categories.py`
(Blackwall branch, pure/stdlib/tested) classifies an x402 resource URL into a coarse
service category. Proposal: Traceipt tags each receipt with
`categories.classify_resource(resource_url)` as an OPTIONAL advisory field (enables
spend-by-category + sector analytics). Advisory only, derived-not-asserted, keep the
field optional so old receipts stay valid. Full write-up + constraints:
`docs/CATEGORY.md` §1. Import `categories.py` rather than re-implementing so the two
sides can't drift.

## 5. Push protocol (both sessions)

```sh
git fetch origin <branch>
git rebase origin/<branch>          # or: git pull --rebase origin <branch>
# ... run the full test suite (see CLAUDE.md) ...
git push -u origin <branch>
git rev-parse HEAD                  # must EQUAL:
git rev-parse origin/<branch>       # ...this. If not, someone pushed under you.
```

Never force-push a shared branch unless it contains only already-merged history and
you've told the other session.

---

**If you're the other session and unsure whether your task overlaps:** it's in §2 →
don't redo it; it's in §3 → note here that you're taking it; it touches §4 → sync
first. When in doubt, ask before pushing.
