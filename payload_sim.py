#!/usr/bin/env python3
"""
payload_sim.py -- Phase 1 payload simulation: cross-check the agent's ACTUAL signed
x402 payment against the payment it asked Blackwall to score.

Blackwall's verdict is computed from the CLAIMED {counterparty, amount, asset,
chain} in the request body. But a compromised or MITM'd agent can ask us to score
"pay $5 to X" while the authorization it is about to broadcast actually says "pay
$5000 to Y". Blackwall never sees the real signed payment -- so a clean verdict
would be about a payment that ISN'T the one being made. This closes that gap.

Phase 1 (this module, stdlib, no crypto): when the request carries the agent's
signed EIP-3009 `transferWithAuthorization` (the exact X-PAYMENT it is about to
send the counterparty), decode it and assert the authorization MATCHES the payment
being scored:
  * to      == counterparty   (paying who you asked me to score)
  * value   == amount         (atomic units; the sum you asked me to score)
  * asset   == asset          (same token, when the claim names a contract address)
  * network == chain          (same chain, CAIP-2 aware)
  * a nonce is present        (a real EIP-3009 auth always carries one)
Any mismatch is a NON-NEGOTIABLE hard STOP -- "the signed payment does not match
the payment you asked me to score" -- folded into decide_payment's `hard_stop`.
Time-validity (expired / not-yet-valid) is advisory (a facilitator rejects those
anyway), reported as a warning, not a hard stop.

IMPORTANT -- CHANNEL: the payment-being-scored travels in the request BODY field
`payment_authorization`, NOT the transport `X-PAYMENT` header. That header is
Blackwall's OWN fee payment (Blackwall is itself x402-paid, see x402.py); the two
must never be conflated.

LIMITATIONS (audited & accepted):
  * Known-asset gate. Phase 2's signer recovery (and thus the cryptographic
    chain/asset binding) runs only for assets with a trusted EIP-712 domain in
    x402.EIP712_DOMAINS (Base/Base-Sepolia USDC today). For an UNKNOWN asset there
    is no trusted domain, so signer verification degrades to a warning and the
    Phase-1 asset/chain checks fall back to best-effort on self-declared metadata
    (a payment omitting them isn't cross-checked on those fields). Recipient +
    amount always bind; add domains to the table to extend the crypto binding.
  * Fundamental limit: we verify the SHOWN authorization; we cannot prove it is the
    one actually broadcast. A fully malicious agent showing one auth and sending
    another is out of scope for any phase.
  * Decimals. The amount is compared in atomic units assuming USDC (6dp). A
    non-USDC asset with different decimals would FALSE-mismatch on amount -- errs
    SAFE (STOP, never a false GO). Blackwall is USDC-only today; wire decimals to
    the asset when that changes.
  * Latency. Pure-Python secp256k1 recovery is ~20ms per signed check -- negligible
    next to the on-chain settlement it guards, and only runs when a signature is
    present.

Phase 2 (BUILT, `verify_signer=True`): reconstruct the EIP-712 digest of the
`transferWithAuthorization` and RECOVER the signer (pure-Python secp256k1), then
confirm it is the authorization's stated `from`. Crucially the digest's DOMAIN is
built from the CLAIM (chainId from `chain`, verifyingContract from `asset`), so a
valid recovery also cryptographically binds the chain + asset -- closing the Phase-1
gap where those were self-declared. Recovery failure or a signer != `from` for a
KNOWN asset is a hard STOP; an unknown asset/chain (no trusted domain) or an absent
signature degrades to a warning (Phase-1 recipient+amount still bind).

Reuses x402.py's decode/extract helpers, addresses.py, and the pure-Python
keccak/secp256k1/eip712 primitives. Pure + stdlib.
"""
from __future__ import annotations

from decimal import Decimal

from addresses import addresses_equal, is_evm_address
from x402 import (EIP712_DOMAINS, _accepted, _authorization, decode_payment_header,
                  to_atomic, to_caip2)


def _decimal_places(decimals):
    """"7 decimal places" -- for a reason string that must not read as a
    comparison when no comparison happened."""
    try:
        return "%d decimal places" % int(decimals)
    except (TypeError, ValueError):
        return "this asset's units"


def _atomic_human(atomic, decimals):
    """Atomic units -> a human token amount string (e.g. 90000 -> "0.09"); falls
    back to the raw value if it can't be parsed. So reasons read in real amounts,
    not atomic units."""
    try:
        return format(Decimal(int(str(atomic))) / (Decimal(10) ** int(decimals)), "f")
    except Exception:
        return str(atomic)


def _decode(x_payment, decode):
    """`x_payment` may be a base64 X-PAYMENT string or a pre-decoded payment dict."""
    if isinstance(x_payment, dict):
        return x_payment
    if isinstance(x_payment, str):
        return (decode or decode_payment_header)(x_payment)
    return None


def _int_or_none(v):
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _show(v):
    """A short, log-safe rendering of an attacker-controlled value."""
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "..."


def _claim_domain(claim):
    """Build the EIP-712 domain FROM THE CLAIM (name/version from the asset's known
    domain, chainId from the claimed chain, verifyingContract = the claimed asset),
    or None if the asset/chain isn't a trusted known domain. Building it from the
    claim -- not the attacker's metadata -- is what makes a successful recovery bind
    the signature to the claimed chain + asset."""
    asset = claim.get("asset")
    if not is_evm_address(asset):
        return None
    nv = EIP712_DOMAINS.get(asset.lower())
    if not nv:
        return None
    caip2 = to_caip2(claim.get("chain"))
    if not (isinstance(caip2, str) and caip2.startswith("eip155:")):
        return None
    try:
        chain_id = int(caip2.split(":")[1])
    except (ValueError, IndexError):
        return None
    return {"name": nv["name"], "version": nv["version"],
            "chainId": chain_id, "verifyingContract": asset}


def _recover_signer(payment, auth, domain):
    """Recover the Ethereum address that signed the payment's EIP-3009
    authorization under `domain`, or None. NEVER raises."""
    try:
        from eip712 import (pubkey_to_address, split_signature,
                            transfer_authorization_digest)
        from secp256k1 import ecdsa_recover
        payload = payment.get("payload") if isinstance(payment, dict) else None
        sig = payload.get("signature") if isinstance(payload, dict) else None
        parts = split_signature(sig) if sig else None
        if not parts:
            return None
        r, s, rec = parts
        z = transfer_authorization_digest(domain, {
            "from": auth.get("from"), "to": auth.get("to"),
            "value": auth.get("value"), "validAfter": auth.get("validAfter"),
            "validBefore": auth.get("validBefore"), "nonce": auth.get("nonce")})
        q = ecdsa_recover(z, r, s, rec)
        return pubkey_to_address(q) if q else None
    except Exception:
        return None


# Assets whose decimals we KNOW. Canonical USDC deployments (all 6) plus the
# common 18-decimal stablecoins, so the overwhelmingly-common case resolves
# without the caller supplying anything.
KNOWN_DECIMALS = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,   # USDC  Base
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,   # USDC  Ethereum
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": 6,   # USDC  Arbitrum
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": 6,   # USDC  Polygon
    "0x0b2c639c533813f4aa9d7837caf62653d097ff85": 6,   # USDC  Optimism
    "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,   # USDT  Ethereum
    "0x6b175474e89094c44da98b954eedeac495271d0f": 18,  # DAI   Ethereum
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": 8,   # WBTC  Ethereum
    "0x056fd409e1d7a124bd7017459dfea2f387b6d5cd": 2,   # GUSD  Ethereum (2dp!)
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": 18,  # WETH  Ethereum
    "0x4200000000000000000000000000000000000006": 18,  # WETH  Base/OP
    # BSC stablecoins are 18 DECIMALS, not 6 -- verified on-chain via
    # decimals() on bsc-dataseed 2026-08-27. USDT is 6 on Ethereum and 18 on
    # BSC, which is exactly the trap a single global default walks into.
    "0x55d398326f99059ff775485246999027b3197955": 18,  # BSC-USD (USDT on BSC)
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": 18,  # BUSD  BSC
    "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d": 18,  # USD1  BSC
    "0xce24439f2d9c6a2289f741120fe202248b666666": 18,  # BSC
    # Solana USDC (base58 mint, not an 0x address)
    "epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v": 6,
}
KNOWN_SYMBOL_DECIMALS = {"usdc": 6, "usdt": 6, "dai": 18, "weth": 18,
                         "eth": 18, "wbtc": 8, "gusd": 2}


# ---------------------------------------------------------------------------
# Per-CHAIN decimals, resolved on-chain from the LIVE x402 corpus (2026-08-29).
#
# WHY A SECOND, CHAIN-KEYED TABLE. `KNOWN_DECIMALS` above is keyed by ADDRESS
# ALONE, which is only sound while every entry happens to agree across chains
# (all canonical USDC deployments are 6). It stops being sound the moment a
# corpus asset is NOT 6, because an address does not determine a token: the same
# 20 bytes are a different contract on a different chain, and nothing stops a
# 6-decimal token on one chain sharing an address with an 18-decimal token on
# another. This corpus contains exactly that hazard -- JPYC on Polygon is 18 --
# so the new entries are keyed by (network, asset) and NOT added to the flat
# table. `chain` is a REQUIRED request field (see validate_request), so the
# chain-keyed lookup covers every real request; a claim with no chain simply
# resolves to unknown, which is the module's safe answer.
#
# PROVENANCE. Every EVM value is a live `decimals()` read (token_decimals.py,
# with the totalSupply() confirmation on a zero result). 12 of the 16 EVM
# entries were read from >= 2 INDEPENDENT public RPC providers and all agreed;
# the 4 marked `1 rpc` had only one public endpoint available. Solana values are
# the SPL mint account's own decimals byte; Algorand's are the asset's params.
# The two Stellar entries are the protocol constant (classic Stellar amounts are
# stroops, 1e-7) rather than a contract read -- corroborated by two unrelated
# hosts whose Stellar leg is advertised at exactly 10x their 6-decimal legs.
#
# NOT a price table. Two of these are not dollars at all (JPYC is yen, EURC is
# euro). Knowing the SCALE lets the atomic amount check work; it does not make
# a JPYC quote comparable to a USDC one. See docs/DECIMALS_AUDIT.md.
KNOWN_DECIMALS_BY_CHAIN = {
    # -- EVM ---------------------------------------------------------------
    ("eip155:137", "0x431d5dff03120afa4bdf332c61a6e1766ef37bdb"): 18,  # JPYC (yen!) 4 rpc
    ("eip155:8453", "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42"): 6,   # EURC (euro) 4 rpc
    ("eip155:43114", "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e"): 6,  # USDC  Avalanche
    ("eip155:480", "0x79a02482a880bce3f13e09da970dc34db4cd24d1"): 6,    # USDC  World Chain
    ("eip155:4663", "0x5fc5360d0400a0fd4f2af552add042d716f1d168"): 6,   # USDG  Robinhood Chain
    ("eip155:196", "0x74b7f16337b8972027f6196a17a631ac6de26d22"): 6,    # USDC  X Layer
    ("eip155:196", "0x779ded0c9e1022225f8e0630b35a9b54be713736"): 6,    # USD-T0 X Layer
    ("eip155:196", "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"): 6,    # USDG  X Layer
    ("eip155:143", "0x754704bc059f8c67012fed69bc8a327a5aafb603"): 6,    # USDC  Monad (1 rpc)
    ("eip155:1329", "0xe15fc38f6d8c56af07bbcbe3baf5708a2bf42392"): 6,   # USDC  Sei (1 rpc)
    ("eip155:42220", "0xceba9300f2b948710d2653dd7b07f33a8b32118c"): 6,  # USDC  Celo (1 rpc)
    ("eip155:1187947933", "0x85889c8c714505e0c94b30fcfcf64fe3ac8fcb20"): 6,  # USDC.e SKALE (1 rpc)
    # Added 2026-08-30, surfaced by asset_coverage on api.402rates.com. Resolved
    # under the reviewed procedure: read from EVERY public RPC chain 50 publishes.
    # 7 of 7 answered and all agreed -- the widest agreement of any entry here.
    ("eip155:50", "0xfa2958cb79b0491cc627c1557f441ef849ca8eb1"): 6,   # USDC  XDC (7 rpc)
    # Added 2026-09-05, surfaced by the monthly asset_coverage run on
    # api.lastlookdata.com. NOT 6, and not a stablecoin: wrapped SOL on Base, 9
    # decimals. Same reviewed procedure -- 6 of the 10 public Base RPCs answered
    # and all 6 agreed on decimals=9 / symbol=SOL / name=Solana, 0 disagreements.
    # CORROBORATED BY THE CORPUS, which is what separates a read from a guess:
    # the same host quotes the same resource at 0.5 USDC, and 4913039 at 9
    # decimals is 0.004913 SOL -- the same half-dollar. Read at the corpus
    # default of 6 it would be 4.91 SOL, ~1000x, for a $0.50 API call.
    ("eip155:8453", "0x311935cd80b76769bf2ecc9d8ab7635b2139cf82"): 9,  # SOL  Base (6 rpc)
    # testnets -- present in the live corpus, so scored like any other asset
    ("eip155:84532", "0x036cbd53842c5426634e7929541ec2318f3dcf7e"): 6,  # USDC  Base Sepolia
    ("eip155:80002", "0x41e94eb019c0762f9bfcf9fb1e58725bfb0e7582"): 6,  # USDC  Polygon Amoy
    ("eip155:1952", "0xf0863d7a29a55d0c4263c11bfac754312ff078df"): 6,   # USDG  X Layer testnet
    ("eip155:5042002", "0x3600000000000000000000000000000000000000"): 6,  # USDC Arc testnet
    # -- Solana (SPL mint account) -----------------------------------------
    ("solana:5eykt4usfv8p8njdtrepy1vzqkqzkvdp",
     "es9vmfrzacermjfrf4h2fyd4kconky11mcce8benwnyb"): 6,                # USDT  Solana mainnet
    ("solana:etwtrabzayq6imfeykouru166vu2xqa1",
     "4zmmc9srt5ri5x14gagxhahii3gnpaeerypjgzjdncdu"): 6,                # USDC  Solana devnet
    # -- Algorand (asset params) -------------------------------------------
    ("algorand:wghe2pwdvd7s12bl5faop20egyesn73ktic1qzkkit8=", "31566704"): 6,  # USDC
    # -- Stellar (protocol constant: amounts are stroops, 1e-7) ------------
    ("stellar:pubnet",
     "usdc:ga5zsejyb37jrc5avcia5mop4rhtm335x2kgx3ihojapp5re34k4kzvn"): 7,
    ("stellar:pubnet",
     "ccw67tszv3sss2hxmbq5jfgckjnxkzm7uquwuzputhxstzleo7sjmi75"): 7,    # the same asset's SAC
    # -- Hyperliquid (canonical spotMeta weiDecimals) ----------------------
    # NB: the one live seller on this network quotes a HUMAN decimal string
    # ("0.003375"), not atomic units -- so this entry records the token's true
    # scale but does not make that seller's quote atomically comparable.
    ("hyperliquid:mainnet", "usdc:0x6d1e7cde53ba9467b783cb7c530ce054"): 8,
}

# Non-EVM network names the shared `x402.to_caip2` deliberately does not carry.
# EVM names resolve through that map (one source of truth); these three are the
# extras this table needs. Bare "solana" is absent ON PURPOSE -- it names neither
# mainnet nor devnet, and answering would be a guess. An unrecognized name falls
# through unchanged, fails to match, and resolves to unknown, which is safe.
_DECIMALS_NETWORKS = {
    "solana-mainnet": "solana:5eykt4usfv8p8njdtrepy1vzqkqzkvdp",
    "solana-devnet": "solana:etwtrabzayq6imfeykouru166vu2xqa1",
    "stellar": "stellar:pubnet",
}


def _chain_decimals(claim):
    """Decimals from the per-CHAIN table, or None.

    Both halves of the key are lowercased. For EVM that is plainly right (hex is
    case-insensitive, and a live 402 returns an EIP-55 CHECKSUMMED asset while
    our table stores lowercase). For base58/base32 ids it is a deliberate,
    documented widening: two distinct Solana mints differing ONLY in case is not
    a case that occurs, and matching case-sensitively would instead make the
    lookup miss on any caller that normalized its input.
    """
    if not isinstance(claim, dict):
        return None
    asset, network = claim.get("asset"), claim.get("chain") or claim.get("network")
    if not isinstance(asset, str) or not isinstance(network, str):
        return None
    net = to_caip2(network.strip())
    net = net.lower() if isinstance(net, str) else ""
    net = _DECIMALS_NETWORKS.get(net, net)
    return KNOWN_DECIMALS_BY_CHAIN.get((net, asset.strip().lower()))


# Optional on-chain resolver, installed by the caller at startup (see
# token_decimals.resolver). Left None by default so nothing here ever touches the
# network unless the operator opted in.
_ONCHAIN = None


def set_onchain_resolver(resolver):
    """Install (or clear) the on-chain `decimals()` reader used as a LAST resort."""
    global _ONCHAIN
    _ONCHAIN = resolver


def resolve_decimals(claim, decimals=None):
    """The asset's decimals, or None when genuinely unknown.

    MEASURED DEFECT (audit 2026-08-27): this module previously defaulted to
    `decimals=6` and `blackwall.forecast` passed 6 unconditionally, so the atomic
    comparison mis-scaled by 10^(d-6) for ANY asset that is not 6-decimal. Two
    consequences, both demonstrated:

      * a VALID 1.0 DAI payment (10^18 atomic) was reported as a mismatch and
        became a hard STOP -- blocking a correct payment;
      * a payment of 10^6 atomic DAI (0.000000000001 DAI) MATCHED a claim of
        "1.0 DAI" -- the gate's whole guarantee, void.

    So an unknown asset must be reported as UNVERIFIED rather than silently
    assumed.

    PRECEDENCE -- what we can VERIFY beats what we are TOLD. The asset's own
    decimals (address table, then symbol, then an optional on-chain read) win;
    the caller-supplied value fills in ONLY when the asset is genuinely unknown.

    SECOND AUDIT FINDING (2026-08-29, HIGH): this originally let the caller value
    win unconditionally. `blackwall.forecast` sources it from the UNTRUSTED
    request body (`payload.get("decimals")`), so a request could re-scale the very
    comparison that gives payload-sim its STOP authority. Reproduced end to end: a
    signed authorization for 10^12 atomic USDC (1,000,000 USDC) against a claim of
    "1.0 USDC" is hard-STOPped honestly, and became verdict=HOLD /
    hard_stop=False with `"decimals": 12` added to the request -- reported as
    amount_status="verified". That is the ORIGINAL defect relocated from a
    hardcoded 6 to attacker control, which is strictly worse because it is
    steerable. `decimals_conflict` reports a caller who contradicts a known asset.
    """
    known = known_decimals(claim)
    if known is not None:
        return known
    if decimals is not None and not isinstance(decimals, bool):
        # bool is an int subclass, so `"decimals": true` would otherwise become 1.
        try:
            d = int(decimals)
        except (TypeError, ValueError):
            return None
        return d if 0 <= d <= 36 else None
    return None


def known_decimals(claim):
    """The asset's decimals as WE can establish them, ignoring anything the caller
    asserted: address table, then symbol, then the optional on-chain read. None
    when we genuinely cannot tell."""
    asset = (claim or {}).get("asset")
    if not isinstance(asset, str):
        return None
    # The per-CHAIN table is consulted FIRST: it is strictly more specific than
    # the address-only table, so where both could answer the chain-keyed answer
    # is the one that cannot be wrong about which token this is.
    chained = _chain_decimals(claim)
    if chained is not None:
        return chained
    key = asset.strip().lower()
    if key in KNOWN_DECIMALS:
        return KNOWN_DECIMALS[key]
    if not key.startswith("0x") and key in KNOWN_SYMBOL_DECIMALS:
        return KNOWN_SYMBOL_DECIMALS[key]
    # Last resort: read decimals() on-chain. Opt-in, cached forever (the value
    # is immutable), and fail-open -- an unreachable node returns None, which
    # is exactly the safe behaviour the static table already produces.
    if _ONCHAIN is not None:
        try:
            # Pass the CHAIN: the same address is a different token on a
            # different network, and a resolver that routes by chain silently
            # falls back to its default without it. `lookup` takes network=None,
            # so omitting it does not error -- it just quietly asks the wrong node
            # and caches the answer forever.
            # NORMALISED to CAIP-2. The resolver routes on `chain_of()`, which
            # takes "eip155:8453" or a bare id and returns None for a human name
            # -- and every claim here carries "base"/"ethereum". Passing the raw
            # value left the routing INERT on every real request while looking
            # wired up: lookup takes network=None, so it does not error, it just
            # asks whatever node is default and caches that answer forever.
            _net = (claim or {}).get("chain") or (claim or {}).get("network")
            return _ONCHAIN.lookup(asset, to_caip2(_net) if _net else None)
        except Exception:
            return None
    return None


def decimals_conflict(claim, decimals):
    """True when the caller asserted decimals that CONTRADICT an asset we know.

    Not merely ignorable: an x402 v2 challenge carries the real decimals, so a
    value disagreeing with a token we can identify is an attack indicator, and
    the whole point of the atomic comparison is that it cannot be re-scaled by
    the party being checked."""
    if decimals is None:
        return False
    known = known_decimals(claim)
    if known is None:
        return False
    try:
        return int(decimals) != known
    except (TypeError, ValueError):
        return False


def check_payment_authorization(claim, x_payment, *, decimals=None, now=None,
                                verify_signer=True, decode=None):
    """Cross-check a signed x402 payment against the CLAIMED payment being scored.

    `claim`     = {counterparty, amount, asset, chain} (amount a decimal string or
                  number; asset may be a contract address or a symbol like "USDC").
    `x_payment` = the base64 X-PAYMENT the agent is about to SEND THE COUNTERPARTY
                  (or a pre-decoded dict). `decimals` is the asset's decimals
                  (USDC = 6). `now` (unix seconds), when given, enables the advisory
                  time-validity checks.

    Returns {"checked": bool, "matches": bool, "mismatches": [str], "warnings": [str]}.
    `checked` is False when no payment was supplied (the cross-check is OPT-IN;
    absence is NOT a failure). When supplied, every entry in `mismatches` is a
    hard-stop reason. NEVER raises -- a crafted payment can't crash the gate.
    """
    if x_payment is None or x_payment == "":
        return {"checked": False, "matches": True, "mismatches": [], "warnings": [],
                "signer_status": "not_applicable"}

    payment = _decode(x_payment, decode)
    if not isinstance(payment, dict):
        return {"checked": True, "matches": False, "warnings": [],
                "mismatches": ["could not decode the signed payment to verify it "
                               "matches the payment being scored"],
                "signer_status": "undecodable"}

    claim = claim if isinstance(claim, dict) else {}
    auth = _authorization(payment)
    acc = _accepted(payment)
    mismatches = []
    warnings = []

    # --- recipient: paying who you asked me to score? ---
    to, cp = auth.get("to"), claim.get("counterparty")
    if not to or not cp or not addresses_equal(to, cp):
        mismatches.append(
            "signed payment pays %s but you asked me to score %s (recipient mismatch)"
            % (_show(to), _show(cp)))

    # --- amount: same sum (compared in atomic units, reported in human units)? ---
    if decimals_conflict(claim, decimals):
        mismatches.append(
            "supplied decimals %s contradict the known decimals %s for asset %s "
            "-- refusing to re-scale the amount check"
            % (_show(decimals), _show(known_decimals(claim)), _show(claim.get("asset"))))
    resolved = resolve_decimals(claim, decimals)
    got = _int_or_none(auth.get("value"))
    if resolved is None:
        # Decimals unknown -> the atomic comparison is MEANINGLESS. Say so rather
        # than assuming 6, which previously let a 10^12 underpayment pass as a
        # match. Surfaced like `signer_status`: a GO must never be mistaken for
        # amount-verified.
        amount_status = "unverified_decimals"
        warnings.append(
            "amount NOT verified: the asset's decimals are unknown (%s), so the "
            "signed value %s cannot be compared to the claimed %s -- supply "
            "`decimals` to enable this check"
            % (_show(claim.get("asset")), _show(auth.get("value")),
               _show(claim.get("amount"))))
    else:
        # WHERE the scale came from decides what we may call this. Ground truth
        # (table / symbol / on-chain) -> verified. An asset we cannot identify
        # leaves the caller as the only source -- and in `forecast` that caller is
        # the request body, i.e. the party being screened. The arithmetic is still
        # done, but calling it "verified" would assert a check we did not perform,
        # which is the same category error as the original decimals bug.
        if known_decimals(claim) is None:
            amount_status = "asserted_decimals"
            warnings.append(
                "amount checked at CALLER-ASSERTED decimals (%s) -- the asset %s "
                "is not one we can identify, so the scale is unverified and this "
                "is not proof the amounts agree"
                % (_show(decimals), _show(claim.get("asset"))))
        else:
            amount_status = "verified"
        want = to_atomic(claim.get("amount"), resolved)
        # AUDIT FINDING (2026-08-29, MEDIUM). These three cases were collapsed
        # into one message that always read as a COMPARISON ("value is X but you
        # asked me to score Y") -- including when no comparison happened at all,
        # because one side did not convert. `to_atomic` returns None for a claim
        # amount carrying MORE precision than the asset has (0.00000001 on a
        # 7-decimal asset), and the response still reported
        # `amount_status: "verified"` -- asserting a check that never ran, the
        # exact category error the branch above exists to avoid. Reachable now
        # that the corpus contributes 7- and 8-decimal assets, where a quote with
        # more decimal places than the token supports is an easy mistake.
        if want is None:
            amount_status = "unrepresentable_amount"
            mismatches.append(
                "the amount %s cannot be expressed in %s -- it carries more "
                "precision than the asset has, so it was never compared to the "
                "signed value %s"
                % (_show(claim.get("amount")), _decimal_places(resolved),
                   _show(auth.get("value"))))
        elif got is None:
            amount_status = "unreadable_payment_value"
            mismatches.append(
                "the signed payment value %s is not a readable amount, so it "
                "could not be compared to the claimed %s"
                % (_show(auth.get("value")), _show(claim.get("amount"))))
        elif want != got:
            mismatches.append(
                "signed payment value is %s but you asked me to score %s"
                % (_atomic_human(auth.get("value"), resolved),
                   _show(claim.get("amount"))))

    # --- asset: same token? (only when the claim names a contract address; a
    #     symbol like "USDC" can't be checked against a contract address) ---
    _pl = payment.get("payload")
    _pl = _pl if isinstance(_pl, dict) else {}
    pay_asset = acc.get("asset") or payment.get("asset") or _pl.get("asset")
    claim_asset = claim.get("asset")
    if pay_asset is not None and is_evm_address(claim_asset) \
            and not addresses_equal(pay_asset, claim_asset):
        mismatches.append("signed payment asset %s != the scored asset %s"
                          % (_show(pay_asset), _show(claim_asset)))

    # --- network: same chain? (CAIP-2 aware, so "base" == "eip155:8453") ---
    net = acc.get("network") or payment.get("network")
    claim_chain = claim.get("chain")
    if net is not None and claim_chain is not None \
            and to_caip2(net) != to_caip2(claim_chain):
        mismatches.append("signed payment network %s != the scored chain %s"
                          % (_show(net), _show(claim_chain)))

    # --- a real EIP-3009 authorization always carries a nonce ---
    if not auth.get("nonce"):
        mismatches.append("signed payment carries no authorization nonce -- not a "
                          "valid EIP-3009 authorization")

    # --- Phase 2: recover the signer and confirm it is the stated payer. The
    #     domain is built from the CLAIM, so a valid recovery ALSO binds the
    #     signature to the claimed chain + asset (EIP-712 domain = chainId +
    #     token). Hard STOP on a bad/foreign signature for a KNOWN asset; unknown
    #     asset/chain or a missing signature degrades to a warning. ---
    # signer_status makes the Phase-2 outcome EXPLICIT so a caller can never mistake a
    # deferred (fast-path) verdict for a cryptographically-verified one:
    #   deferred    -- Phase 2 skipped (verify_signer=False); run the second stage.
    #   confirmed   -- signer recovered and equals the stated payer.
    #   mismatch    -- signer != payer / forged (a hard-stop reason was added).
    #   unverified  -- Phase 2 ran but couldn't (no domain / no sig / no `from`).
    signer_status = "deferred"
    if verify_signer:
        _m0, _w0 = len(mismatches), len(warnings)
        _from = auth.get("from")
        domain = _claim_domain(claim)
        payload = payment.get("payload") if isinstance(payment, dict) else None
        has_sig = isinstance(payload, dict) and bool(payload.get("signature"))
        if domain is None:
            warnings.append("signer not verified: no trusted EIP-712 domain for the "
                            "claimed asset/chain (%s)" % _show(claim.get("asset")))
        elif not has_sig:
            warnings.append("signer not verified: payment carries no signature")
        elif not _from:
            warnings.append("signer not verified: authorization has no `from`")
        else:
            recovered = _recover_signer(payment, auth, domain)
            if recovered is None:
                mismatches.append(
                    "signature is not a valid payer signature for the claimed "
                    "chain+asset -- forged, or a different chain/asset than scored")
            elif not addresses_equal(recovered, _from):
                mismatches.append(
                    "signature signer %s != the stated payer %s (from)"
                    % (_show(recovered), _show(_from)))
        signer_status = ("mismatch" if len(mismatches) > _m0
                         else "unverified" if len(warnings) > _w0 else "confirmed")

    # --- time validity: advisory only (a facilitator rejects these itself) ---
    if now is not None:
        vb = _int_or_none(auth.get("validBefore"))
        va = _int_or_none(auth.get("validAfter"))
        if vb is not None and vb <= int(now):
            warnings.append("signed authorization already expired "
                            "(validBefore is in the past)")
        if va is not None and va > int(now):
            warnings.append("signed authorization not yet valid "
                            "(validAfter is in the future)")

    return {"checked": True, "matches": not mismatches,
            "mismatches": mismatches, "warnings": warnings,
            "signer_status": signer_status,
            "amount_status": amount_status}
