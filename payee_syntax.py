"""
payee_syntax.py -- is the address the agent is about to pay a possible address?

FOUND IN THE WILD, not imagined. `asset_coverage` probing the live x402 corpus
on 2026-08-30 turned up a seller advertising this as its Solana `payTo`:

    2DgEL95L8DtaRb4ubYqrrnMbX7Zxgjxq7k8Ed9XAWYcpFACILITATOR_URL=https://x402.org/facilitator

A real address with an environment variable concatenated onto it -- almost
certainly a missing newline in a `.env`. A payment there cannot arrive. The same
seller also advertised a 39-character EVM asset address.

SINCE FIXED BY THE SELLER, on 2026-09-05 -- and the three-step road to knowing
that is worth more than the finding. (1) The first re-probe found 0 malformed
among the 175 hosts that answered, with 20 silent, and this docstring concluded
"fixed or gone quiet, unknown which". (2) A later probe found the host,
`apiwitchcraft.duckdns.org`, back up and STILL advertising it, so the docstring
was corrected to "it went quiet; it was never fixed". (3) The monthly run found
0 again -- and this time the host is ANSWERING and its `payTo` reads
`2DgEL95L8DtaRb4ubYqrrnMbX7Zxgjxq7k8Ed9XAWYcp`, clean. Verified by reading the
host's own challenge, not inferred from the count reaching zero.

WHAT DISTINGUISHES FIXED FROM SILENT, concretely: that seller had TWO defects,
and only one is repaired. Its BSC asset is still truncated to 39 hex characters,
still reported by `asset_coverage`, on the same host in the same run. A finding
that survives is proof the host answered; without it, "0 malformed" is once again
just 0 SEEN. That is why every run leads with how many hosts answered, and why
this module states the date it last checked rather than a standing claim.

The gate is not weakened by its motivating case being repaired -- one seller
fixing a `.env` does nothing about the next one, and the corpus still carries a
malformed identifier today. Re-derivable from `data/asset_coverage.json`.

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
               0 of 292 real payees exhibit it, its only real instance was an
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
**0 of 292 DISTINCT** real payees flagged -- the union of `data/directory.json`
(266), the seed manifest (292) and `data/liveness.json` (195), which overlap
heavily -- plus 0 of 7 hand-picked good shapes including Stellar's colon form
(`USDC:GA5Z...`), raw base58, and EIP-55 checksummed hex. AUDIT CORRECTION: this
first claimed "0 of 558" by ADDING two sets that are nearly the same set. The
number is re-derivable from committed artifacts by `test_payee_syntax.py`, which
now computes the union rather than restating it -- a prevalence claim another
session cannot reproduce is worth nothing.

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
_IMPOSSIBLE = ("://", "=", ",", ";", '"', "'", "<", ">")


def _impossible_char(ch):
    """Whitespace or a non-printable, ANYWHERE in the identifier.

    AUDIT FINDING (fixed): this was a literal tuple of ASCII spaces, so the
    check was ASCII-only and the evasions were trivial -- a non-breaking space
    (a Windows `.env`, a copy-paste out of a web page) or a zero-width space
    glued a URL onto an address and graded `unknown`, which does not gate. The
    same shape as the case found in the wild, and it walked straight through.
    `isspace()` covers NBSP and the Unicode spaces; `not isprintable()` covers
    NUL, vertical tab, form feed, the zero-width characters and the
    bidirectional overrides -- a lookalike-address trick in its own right.
    Measured on the corpus: 0 additional flags, so the widening is free.
    """
    return ch.isspace() or not ch.isprintable()


def _safe_hint(text):
    """A short, LOG-SAFE rendering of a merchant-controlled string.

    AUDIT FINDING (fixed): the hint went into `reasons[]` raw, so a payee
    carrying a newline forged a line in any plain-text log or CLI report that
    prints a reason. JSON escapes it; a terminal does not. `repr` without the
    surrounding quotes escapes control characters and leaves ordinary
    addresses completely readable.
    """
    short = text if len(text) <= 24 else text[:16] + "..." + text[-4:]
    return repr(short)[1:-1]

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
    out["hint"] = _safe_hint(text)

    bad = [token for token in _IMPOSSIBLE if token in text]
    bad += sorted({ch for ch in text if _impossible_char(ch)})
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
            # wild. This one does not: 0 of 292 distinct real payees are
            # 0x-prefixed but
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
    # `list(...)` on a string would splay it into characters, in the verdict and
    # in the signal alike. Neither can happen through `assess_payee`, but this
    # fold is exported and the cost of being sure is one isinstance.
    existing = v.get("reasons")
    reasons = list(existing) if isinstance(existing, (list, tuple)) else []
    found = signal.get("reasons")
    found = list(found) if isinstance(found, (list, tuple)) else []
    if grade == INVALID_HEX:
        reasons.extend(found)
    if grade == MALFORMED:
        reasons.extend(found)
        if gate and v.get("verdict") == "GO":
            v["verdict"] = "HOLD"
            reasons.append("escalated GO->HOLD: the payee is not a possible "
                           "on-chain address")
    v["reasons"] = reasons
    return v
