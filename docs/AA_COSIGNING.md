# AA co-signing — Blackwall as a mandatory ERC-4337 / ERC-7579 guard

The deliberate **biggest bet** from `docs/STRATEGY_REVIEW.md` #4. Today Blackwall is
verdict-**only**: advisory, never in the money path, no custody, and if it's down
payments still flow. Co-signing trades that away for a *mandatory* guarantee: a
smart account that **cannot broadcast a payment without Blackwall's signature**.

This is not a drop-in upgrade — it changes Blackwall's liability and availability
posture. Read the tradeoff before shipping.

## How it works

```
1. The agent's smart account builds a UserOperation (the payment).
2. It asks Blackwall to co-sign.  Blackwall:
     a. computes the userOpHash (aa_cosigner.user_op_hash — ERC-4337 v0.7),
     b. decodes execute(dest,value,data) and SCREENS the real on-chain call
        (calldata.py Phase 3) + runs the verdict (forecast),
     c. returns an ECDSA signature over the userOpHash ONLY on GO
        (or a human-approved HOLD); on STOP or a drainer/mismatch it REFUSES.
3. The account puts Blackwall's signature in userOp.signature (alongside/instead of
   the owner sig, per the validator design).
4. On-chain, the Blackwall ERC-7579 validator ecrecovers userOp.signature and
   checks it is Blackwall's authorized key. No signature ⇒ validateUserOp reverts
   ⇒ the EntryPoint rejects the op ⇒ no payment.
```

The co-signer verifies the **actual call** (`execute`'s inner `to`/`value`/`data`),
not a self-reported claim — so "score paying 0.09 to X" but "callData approves ∞ to
Y" is refused *even when the verdict is GO*.

## The load-bearing decision: fail-open vs fail-closed

This is about Blackwall being **unreachable** (down/timeout), NOT about a STOP
verdict (a STOP always withholds the signature — that's the point).

| Policy | Blackwall down ⇒ | Pro | Con |
|---|---|---|---|
| `FAIL_CLOSED` | withhold signature → **payments halt** | strongest guarantee | hard availability SLA; an outage freezes the wallet |
| `FAIL_OPEN` | sign anyway → **degrade to advisory** | preserves liveness | defeats the "mandatory" guarantee during the outage |

`aa_cosigner.cosign_user_op(..., policy=FAIL_CLOSED|FAIL_OPEN)` makes this explicit.
Pick per customer; high-value treasuries lean fail-closed with an HA co-signer +
break-glass, agent spending wallets may prefer fail-open.

## Posture change (do not skip)

- **Custody/liability.** You are now in the transaction path holding a signing key.
  Money-transmitter and liability analysis differ from verdict-only. Verdict-only
  Blackwall drops in front of *any* rail with none of this — keep offering it.
- **Availability.** Fail-closed makes Blackwall a hard dependency of the wallet.
- **Key management.** The co-signer key is new critical infra (HSM/MPC, rotation,
  the `revoke`-equivalent on-chain: swap the authorized key in the validator).

## Reference on-chain validator (ERC-7579) — *specification, not deployed*

Prototype on a **testnet** account first. The off-chain half is in `aa_cosigner.py`;
the on-chain half is roughly:

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.23;
// A minimal ERC-7579 validator: an op is valid only if it carries a signature from
// Blackwall's authorized key over the EntryPoint userOpHash.
contract BlackwallValidator {
    mapping(address account => address) public blackwallKey;   // per-account authorized signer

    function onInstall(bytes calldata data) external { blackwallKey[msg.sender] = address(bytes20(data)); }
    function onUninstall(bytes calldata) external { delete blackwallKey[msg.sender]; }

    // ERC-4337 v0.7: userOpHash is the EntryPoint-provided hash.
    function validateUserOp(PackedUserOperation calldata userOp, bytes32 userOpHash)
        external view returns (uint256 validationData)
    {
        address signer = ECDSA.recover(
            MessageHashUtils.toEthSignedMessageHash(userOpHash),  // OR raw userOpHash — MATCH the off-chain signer
            userOp.signature
        );
        // 0 == valid, 1 == SIG_VALIDATION_FAILED
        return signer == blackwallKey[userOp.sender] ? 0 : 1;
    }
}
```

**Hash-domain caveat:** the on-chain `recover` MUST hash the same bytes the
off-chain signer signed. `aa_cosigner._sign_hash` signs the **raw** userOpHash; if
your validator wraps it with `toEthSignedMessageHash` (the EIP-191 prefix), prefix
it off-chain too, or drop the wrapper on-chain. The tests cross-check the raw form
against `eth_abi`/`eth-account`; **re-verify against your exact EntryPoint +
validator before mainnet.**

## What is and isn't built here

- ✅ Off-chain: exact v0.7 `userOpHash` (cross-checked vs `eth_abi`), `execute`
  decoding + Phase-3 calldata screening, the GO/HOLD/STOP sign-or-refuse decision,
  fail-open/closed, and `recover_cosigner` (what the validator does), all tested and
  cross-checked against `eth-account`.
- ⛔ Not built: the deployed Solidity validator, HSM/MPC key infra, and a bundler
  path. Those are the on-chain/ops half — the real bet — to prototype on testnet.
- **v0.6 EntryPoint** uses a different packing; add a `user_op_hash_v06` if you
  target it.
