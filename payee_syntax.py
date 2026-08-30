"""
payee_syntax.py -- is the address the agent is about to pay a possible address?

FOUND IN THE WILD, not imagined. `asset_coverage` probing the live x402 corpus
turned up a seller advertising this as its Solana `payTo`:

    2DgEL95L8DtaRb4ubYqrrnMbX7Zxgjxq7k8Ed9XAWYcpFACILITATOR_URL=https://x402.org/facilitator

A real address with an environment variable concatenated onto it -- almost
certainly a missing newline in a `.env`. A payment there cannot arrive. The same
seller also advertised a 39-character EVM asset address.

WHAT THE ENGINE DID ABOUT IT: nothing. Measured before writing this, and again in
the tests below -- that payee and a clean Solana payee returned BYTE-IDENTICAL
verdicts. Both HOLD, because the counterparty is UNKNOWN, not because one of them
is impossible. `normalize_address` is applied to `payer` only; the counterparty
gets `is_evm_address` purely to decide whether to lowercase, and False there means
"leave it alone", not "reject". So a malformed payee has always been invisible
here, and looked covered because the cold-start HOLD happened to catch it.

That distinction matters: a cold-start HOLD clears the moment the payee has
history, and nothing about a broken address gets better with settlements.

TWO GRADES, AND ONLY ONE OF THEM GATES -- the split is evidence, not taste:

  malformed    content that cannot appear in an identifier on ANY chain (`://`,
               `=`, whitespace). This is the case found in the wild. GATES.
  invalid_hex  `0x`-prefixed but not a valid EVM address. RECORDED, NOT GATED:
               0 of 558 real payees exhibit it, its only real instance was an
               ASSET field rather than a payee, and turning it on as a gate
               failed 15 tests across 8 modules -- every one a synthetic
               placeholder like `0xKNOWNGOOD00...`. A rule whose only hits are
               fixtures is not ready to refuse a payment.

THE RULE, and why it can be chain-agnostic. We cannot validate every chain's
address format, and guessing would condemn real payees. But some content cannot
appear in ANY chain's identifier: a bare address is not a URL and not a key=value
pair. `://`, whitespace and `=` are therefore safe to reject without knowing the
chain, and that is the only rule that gates. Everything else is UNKNOWN and
passes -- we decline to judge formats we cannot check.

MEASURED FALSE-FLAG RATE BEFORE SHIPPING IT ON, the way sybil_ring graduated:
**0 of 558** real payees flagged -- 266 in `data/directory.json`, 292 in the seed
manifest -- plus 0 of 7 hand-picked good shapes including Stellar's colon form
(`USDC:GA5Z...`), raw base58, and EIP-55 checksummed hex. The one flagged string
in the entire corpus is the broken one above.

HOLD, NOT STOP, and the argument for STOP is deliberately declined for now. A
syntactically impossible payee is a fact rather than an inference, so a STOP would
be defensible -- but every gate in this codebase except sanctions and payload
mismatch is HOLD-only, a HOLD still costs the agent nothing but a question, and
the format rules are the kind of thing a new chain could surprise us on. Revisit
with real request traffic, not before.

Pure, stdlib-only, fail-open. Folded into `decide_payment`/`forecast` via
`signals.payee_syntax`.
"""

# Content that cannot appear in an on-chain identifier on ANY chain. An address
# is not a URL and not a key=value pair. `=` is included because the case that
# prompted this module is an env-var assignment glued onto an address; note that
# Algorand uses `=` in its base64 NETWORK id, which is a different field and is
# never passed here.
_IMPOSSIBLE = ("://", "=", " ", "\t", "\n", "\r", ",", ";", '"', "'", "<", ">")

OK = "ok"
MALFORMED = "malformed"          # impossible content -- GATES
INVALID_HEX = "invalid_hex"      # 0x but not an address -- recorded, does NOT gate
UNKNOWN = "unknown"


def assess_payee(counterparty):
    """Is `counterparty` a possible on-chain identifier?

    Returns {"grade": ok|malformed|invalid_hex|unknown, "reasons": [str],
    "hint": str|None}. Only `malformed` gates -- see the module docstring.
    `hint` is a SHORT redacted rendering -- enough to recognise the problem
    without echoing an arbitrary attacker-supplied string into logs at full
    length, matching how `secret_scan` reports findings.

    NEVER raises: this runs on every request, on a field the merchant controls.
    """
    out = {"grade": UNKNOWN, "reasons": [], "hint": None}
    if not isinstance(counterparty, str):
        return out
    text = counterparty.strip()
    if not text:
        return out
    out["hint"] = text if len(text) <= 24 else text[:16] + "..." + text[-4:]

    bad = [token for token in _IMPOSSIBLE if token in text]
    if bad:
        out["grade"] = MALFORMED
        out["reasons"].append(
            "payee %s contains %s, which cannot appear in an on-chain address on "
            "any chain -- this looks like a configuration error (an environment "
            "variable or URL concatenated onto the address), and a payment sent "
            "there cannot arrive"
            % (out["hint"], " and ".join(repr(b) for b in bad)))
        return out

    if text.lower().startswith("0x"):
        from addresses import is_evm_address
        if not is_evm_address(text):
            # RECORDED, NOT GATED, and the reason is evidence rather than taste.
            # The impossible-content rule above comes from a payee found in the
            # wild. This one does not: 0 of 558 real payees (266 in
            # data/directory.json, 292 in the seed manifest) are 0x-prefixed but
            # invalid, and the only real instance of this shape was an ASSET
            # field, which asset_coverage already reports. Gating on it would be
            # reasoning by analogy, which is what the rest of this module
            # declines to do.
            #
            # There is also direct evidence the shape is used as a STAND-IN
            # rather than an address: turning this on as a gate failed 15 tests
            # across 8 modules, every one of them a synthetic placeholder like
            # `0xKNOWNGOOD00...`. A rule whose only hits are fixtures is not
            # ready to refuse a payment. Revisit if a real payee ever exhibits it.
            out["grade"] = INVALID_HEX
            out["reasons"].append(
                "payee %s starts 0x but is not a valid EVM address -- worth "
                "checking, though this is recorded rather than gated"
                % out["hint"])
            return out
        out["grade"] = OK
        return out

    # A non-EVM identifier we cannot cheaply validate. Declining to judge is the
    # honest answer: there is no base58 or base32 check here that would not also
    # condemn real Solana, Stellar and Algorand payees.
    return out


def apply_payee_syntax(verdict, signal, gate=True):
    """PURE fold: record `signals.payee_syntax` and escalate GO->HOLD when the
    payee is impossible.

    CONSERVATIVE-ONLY -- never upgrades a verdict, never produces a STOP, and
    `unknown` never escalates. Non-mutating. Mirrors `apply_concentration`.
    """
    if not signal or not isinstance(signal, dict):
        return verdict
    grade = signal.get("grade")
    if grade not in (OK, MALFORMED, INVALID_HEX, UNKNOWN):
        return verdict
    if not isinstance(verdict, dict):
        return verdict
    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    v["signals"]["payee_syntax"] = {"grade": grade, "payee": signal.get("hint")}
    reasons = list(v.get("reasons") or [])
    if grade == INVALID_HEX:
        reasons.extend(signal.get("reasons") or [])
    if grade == MALFORMED:
        reasons.extend(signal.get("reasons") or [])
        if gate and v.get("verdict") == "GO":
            v["verdict"] = "HOLD"
            reasons.append("escalated GO->HOLD: the payee is not a possible "
                           "on-chain address")
    v["reasons"] = reasons
    return v
