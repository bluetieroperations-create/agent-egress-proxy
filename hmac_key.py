"""
hmac_key.py -- the ONE owner of the HMAC capability secret.

Three separate modules each grew their own committed fallback for this secret,
and each was found as a separate audit finding:

    blackwall._DEV_RECEIPT_KEY   a "dev receipt key" placeholder constant
    approvals._key()             a "dev insecure key" placeholder constant
    seller_audit._DEV_AUDIT_KEY  a "dev audit key" placeholder constant

(Deliberately DESCRIBED and not quoted: `test_hmac_key` scans this tree for the
literals, and a grep cannot tell a mention from a use -- the blunt scan is the
stronger guard, so the convention is to describe them.)

A COMMITTED SECRET IS NOT A SECRET. Every capability token in this engine is an
HMAC under it: `sign_report_token` (authorizes writing an OUTCOME, which feeds
the reputation ledger the whole product is built on), `approvals`
`decide`/`redeem` tokens (mark a HOLD as human-approved), and
`seller_audit.sign_revoke_token` (withdraw a merchant's badge). With the
fallback in force, anyone who could read the public repo could mint all three.
Measured on the live deploy before fixing: `BLACKWALL_RECEIPT_KEY` IS set there,
so this was latent rather than breached -- which is why the fix could be made
properly instead of as an emergency.

WHY A RANDOM PER-PROCESS KEY rather than refusing to boot. `receipt_signer` can
simply turn signing OFF when unset, because a verdict without a receipt is still
a valid verdict. That option does not exist here: `receipt_id` is emitted on
EVERY verdict and is the ledger join key, so the capability is mandatory. Between
the two remaining choices:

  * REFUSE TO BOOT -- safest, but it breaks every existing deploy on upgrade,
    including the free public smoke-test configuration whose own blueprint says
    to leave the secret blank.
  * RANDOM PER PROCESS -- unforgeable, no deploy breaks, and the one real cost is
    that tokens do not survive a restart.

The cost fails SAFE: after a restart an in-flight outcome report is REJECTED,
never accepted as a forgery. It is also confusing if unexplained -- intermittent
"invalid report_token" with no cause -- so `load_key` reports whether the key is
ephemeral and the boot banner says so in those words.

A SHORT secret is accepted and reported WEAK rather than refused: an operator's
existing short secret must not stop a deploy, and turning that into a boot
failure would be a breaking change dressed as a security fix.
"""
from __future__ import annotations

import os
import secrets

#: The one environment variable. Named here so every consumer agrees, and so an
#: operator rotating it rotates every capability rather than some of them.
ENV = "BLACKWALL_RECEIPT_KEY"

#: HMAC-SHA256's block size is 64 bytes; below this a secret has less entropy
#: than the construction assumes. Advisory only -- see the module docstring.
MIN_BYTES = 16

EPHEMERAL_BYTES = 32

_ephemeral = None


def load_key(environ=None):
    """Return (key_bytes, is_ephemeral).

    An explicitly-set secret ALWAYS wins, even if an ephemeral key was already
    generated in this process -- otherwise a call-order accident would silently
    keep the random one after the operator configured a real secret.
    """
    global _ephemeral
    env = os.environ if environ is None else environ
    raw = (env.get(ENV) or "").strip()
    if raw:
        return raw.encode("utf-8"), False
    if _ephemeral is None:
        _ephemeral = secrets.token_bytes(EPHEMERAL_BYTES)
    return _ephemeral, True


def is_weak(key):
    """True for a set-but-short secret. Advisory; never blocks."""
    return bool(key) and len(key) < MIN_BYTES


def describe(environ=None):
    """One operator-facing line about the capability secret's state."""
    key, ephemeral = load_key(environ)
    if ephemeral:
        return ("capability secret EPHEMERAL -- %s is unset, so a random "
                "per-process key was generated. Report/approval/revoke tokens "
                "are unforgeable but DO NOT SURVIVE A RESTART (an in-flight "
                "outcome report will be rejected after a redeploy). Set %s to "
                "make them durable." % (ENV, ENV))
    if is_weak(key):
        return ("capability secret set but SHORT (%d bytes, want >= %d) -- "
                "report/approval/revoke tokens are only as strong as it is"
                % (len(key), MIN_BYTES))
    return "capability secret configured (%s, %d bytes)" % (ENV, len(key))


def reset_ephemeral_for_tests():
    """Drop the cached ephemeral key. Tests only."""
    global _ephemeral
    _ephemeral = None
