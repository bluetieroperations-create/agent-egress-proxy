# Funded-signer test client (`x402_pay.py`)

A real x402 client that **signs and pays** to drive Blackwall's verdict endpoint
end-to-end: `402 challenge → sign EIP-3009 authorization → resend with
X-PAYMENT → facilitator verify+settle → GO/HOLD/STOP`.

This is **test-only tooling**. The Blackwall service stays stdlib-only; this
client is the one place that needs a dependency (`eth-account`) to produce a real
EIP-712 signature.

## What you need

1. **A throwaway EVM wallet + private key.** Never reuse a real one. Pass the key
   via the `SIGNER_PRIVATE_KEY` env var — it is never written to disk or committed.
2. **That wallet funded on the target network** (use **Base Sepolia** first):
   - **Testnet USDC** — the asset actually transferred to Blackwall's `payTo`.
     Base Sepolia USDC is `0x036CbD…f3dCF7e`; get it from the Circle testnet faucet.
   - **A little Base Sepolia ETH** — from a Base/Coinbase/Alchemy faucet. The
     "exact" scheme is gasless (the facilitator relays), so this is a safety net.
3. **A facilitator** the server points at — `https://facilitator.x402.rs`
   supports `base-sepolia`.
4. **A target endpoint** — your deployed Blackwall, or a local one (below).

## Install

```sh
pip install -r clients/requirements.txt
```

## Run (testnet dry-run)

Server (advertises the testnet network/asset, real facilitator):

```sh
python blackwall.py --pay-to 0xYourFundedPayTo \
    --network base-sepolia \
    --facilitator https://facilitator.x402.rs
```

Client:

```sh
export SIGNER_PRIVATE_KEY=0x<throwaway-key>
python clients/x402_pay.py \
    --url http://localhost:8402/v1/forecast-payment \
    --counterparty 0xCounterpartyToScore \
    --amount 5.00 \
    --network base-sepolia \
    --rpc https://sepolia.base.org
```

`--rpc` lets the client read the token's EIP-712 domain (name/version) **on-chain**
so the signature matches the real contract. Without it, the client uses a
known-values fallback table and prints a warning — fine for a local smoke test,
not for a real settlement.

## Local smoke test (no funds, no facilitator)

To exercise the HTTP/signing/verdict loop without spending anything, run the
server with **no** `--facilitator` (it uses the built-in mock facilitator, which
approves) and the client with `--no-rpc`:

```sh
python blackwall.py --pay-to 0x000000000000000000000000000000000000dEaD --network base-sepolia &
export SIGNER_PRIVATE_KEY=0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d  # well-known test key
python clients/x402_pay.py --url http://localhost:8402/v1/forecast-payment \
    --counterparty 0x1111111111111111111111111111111111111111 --amount 5.00 \
    --network base-sepolia --no-rpc
```

This proves everything except the on-chain settlement (the mock facilitator does
not verify the signature or move funds). The client still self-checks that its
signature recovers to the signer address before sending.

> The test key above is a well-known public Hardhat test key (account #1,
> address `0x7099…79C8`) — for local loopback only. Never use it, or any key
> you've published, with real or testnet funds.
