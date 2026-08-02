"""
Consume sanctions screening from real providers; produce ONE canonical verdict
the neutral Traceipt layer can anchor.

The strategy this encodes: we will never out-analyze Chainalysis/TRM, and we do
not try. We *consume* their free, public sanctions-screening APIs (designed to be
called) as inputs, blend them into a single deterministic verdict, and let
Traceipt bind + anchor that verdict into a compliance-bound receipt. That makes a
risk engine's answer independently provable -- something a risk vendor cannot do
about its own output without a neutrality conflict. The more engines we can cite,
the more neutral the anchor.

This module is CONSUMER-side on purpose. It never lives inside the neutral
service (service.py only ever sees the verdict *digest*, never the provider
calls), so anchoring stays engine-agnostic: any provider here, Black_Wall's own
on-chain heuristics, or a future paid feed can feed the same verdict shape.

Determinism matters: the verdict is what gets hashed (screening.verdict_digest,
byte-identical to Black_Wall's traceipt_attest.verdict_digest). `build_verdict`
lower-cases the subject address and sorts the screens by provider so the same
inputs always yield the same digest. `verify_verdict` lets any third party
RECOMPUTE the GO/STOP/REVIEW decision from the cited evidence rather than trust
the label.

Network providers are optional and go through the environment's egress proxy /
CA exactly like settlement.py (plain urllib + a UA header). The offline provider
needs no key or network so the end-to-end demo/tests run anywhere.
"""
from __future__ import annotations

import base64
import json
import urllib.request
from dataclasses import dataclass, field

DEFAULT_POLICY = "ofac-sanctions-v1"

# Chainalysis publishes this address in its sanctions-API documentation as a
# guaranteed test-positive. We use it as the offline fixture's default listed
# entry so the demo shows a real STOP path without a key or network. It is a
# documentation sample, not a claim about any live wallet.
CHAINALYSIS_DEMO_SANCTIONED = "0x7f367cc41522ce07553e823bf3be79a889debe1b"

_UA = {"User-Agent": "Traceipt/0.2 screening-consumer",
       "Accept": "application/json"}


@dataclass(frozen=True)
class ScreeningResult:
    """One provider's answer for one address.

    `checked` is the honesty bit: a provider that errored (no key, network
    down, rate-limited) returns checked=False, listed=False. The verdict builder
    will NOT clear an address on the strength of a screen that never ran -- it
    downgrades to REVIEW instead. So a failed screen can never masquerade as a
    clean one.
    """
    provider: str
    address: str
    listed: bool
    checked: bool
    source: str = ""
    matches: tuple = ()
    error: str = ""

    def as_screen(self) -> dict:
        return {
            "provider": self.provider,
            "listed": self.listed,
            "checked": self.checked,
            "source": self.source,
            "matches": list(self.matches),
        }


# -- providers ---------------------------------------------------------------

class OfflineSanctionsProvider:
    """Deterministic, no key, no network -- for demos, tests, and air-gapped
    reproduction. Flags any address in `sanctioned` (default: the Chainalysis
    documentation sample). Lower-cased comparison."""

    name = "offline-fixture"

    def __init__(self, sanctioned=None, *, source="offline-fixture"):
        self._set = frozenset(
            a.lower() for a in (sanctioned or [CHAINALYSIS_DEMO_SANCTIONED]))
        self._source = source

    def screen(self, address: str, *, chain: str = "ethereum") -> ScreeningResult:
        listed = address.lower() in self._set
        return ScreeningResult(
            self.name, address, listed, True, self._source,
            ("OFAC-SDN",) if listed else ())


class ChainalysisSanctionsProvider:
    """Chainalysis free public sanctions-screening API. Consuming it within its
    terms (screening addresses you transact with) is the intended use -- we do
    NOT rebuild their proprietary attribution graph. Needs a free API key."""

    name = "chainalysis"
    ENDPOINT = "https://public.chainalysis.com/api/v1/address/{address}"

    def __init__(self, api_key: str, *, timeout: float = 10.0, transport=None):
        self._key = api_key
        self._timeout = timeout
        self._transport = transport or self._http_get

    def _http_get(self, url: str) -> dict:
        req = urllib.request.Request(
            url, headers={**_UA, "X-API-Key": self._key})
        with urllib.request.urlopen(req, timeout=self._timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def screen(self, address: str, *, chain: str = "ethereum") -> ScreeningResult:
        try:
            data = self._transport(self.ENDPOINT.format(address=address))
        except Exception as e:  # noqa: BLE001 -- a failed screen must not clear
            return ScreeningResult(self.name, address, False, False,
                                   "chainalysis-public-sanctions",
                                   error=str(e)[:200])
        ids = data.get("identifications") or []
        matches = tuple(
            i.get("category") or i.get("name") or "sanctioned" for i in ids)
        return ScreeningResult(self.name, address, bool(ids), True,
                               "chainalysis-public-sanctions", matches)


class TRMSanctionsProvider:
    """TRM Labs free public sanctions-screening API. Basic-auth with the API
    key as both user and password (per TRM's public docs)."""

    name = "trm"
    ENDPOINT = "https://api.trmlabs.com/public/v1/sanctions/screening"

    def __init__(self, api_key: str, *, timeout: float = 10.0, transport=None):
        self._key = api_key
        self._timeout = timeout
        self._transport = transport or self._http_post

    def _http_post(self, body: list) -> list:
        token = base64.b64encode(f"{self._key}:{self._key}".encode()).decode()
        req = urllib.request.Request(
            self.ENDPOINT, data=json.dumps(body).encode("utf-8"),
            headers={**_UA, "Content-Type": "application/json",
                     "Authorization": f"Basic {token}"})
        with urllib.request.urlopen(req, timeout=self._timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def screen(self, address: str, *, chain: str = "ethereum") -> ScreeningResult:
        try:
            rows = self._transport([{"address": address, "chain": chain}])
        except Exception as e:  # noqa: BLE001
            return ScreeningResult(self.name, address, False, False,
                                   "trm-public-sanctions", error=str(e)[:200])
        row = (rows or [{}])[0] if isinstance(rows, list) else {}
        listed = bool(row.get("isSanctioned"))
        entities = row.get("sanctionedEntities") or []
        matches = tuple(
            e.get("entity") or e.get("name") or "sanctioned" for e in entities)
        return ScreeningResult(self.name, address, listed, True,
                               "trm-public-sanctions",
                               matches or (("sanctioned",) if listed else ()))


# -- screen + verdict --------------------------------------------------------

def screen(address: str, providers, *, chain: str = "ethereum"):
    """Run `address` through every provider. Never raises: each provider catches
    its own failures and returns checked=False, so a dead provider degrades the
    verdict to REVIEW rather than crashing the screen."""
    return [p.screen(address, chain=chain) for p in providers]


def build_verdict(address: str, results, *, decided_at: str,
                  policy: str = DEFAULT_POLICY) -> dict:
    """Blend provider results into ONE canonical verdict object -- the exact
    thing screening.verdict_digest hashes and Traceipt binds/anchors.

    decision:
      * STOP   -- any provider listed the address (sanctions hit).
      * GO     -- every cited provider actually ran and none listed it.
      * REVIEW -- at least one screen did not run (no key / error / no
                  providers), so we cannot honestly clear it.

    Deterministic: address is lower-cased, screens are sorted by provider, so
    identical inputs always produce an identical digest across ecosystems."""
    # Sort by the FULL canonical screen (not just provider name) so the order
    # -- and therefore the digest -- is total and deterministic even when two
    # screens share a provider name.
    screens = sorted((r.as_screen() for r in results),
                     key=lambda s: json.dumps(s, sort_keys=True))
    listed_any = any(s["listed"] for s in screens)
    all_checked = bool(screens) and all(s["checked"] for s in screens)
    decision = "STOP" if listed_any else ("GO" if all_checked else "REVIEW")
    return {
        "policy": policy,
        "subject": {"address": address.lower()},
        "decided_at": decided_at,
        "decision": decision,
        "screens": screens,
    }


def verify_verdict(verdict: dict):
    """Recompute the decision from the cited evidence. Returns (ok, problems).

    This is the point of a neutral, provable verdict: the STOP/GO/REVIEW label
    is not merely trusted -- any third party re-derives it from the screens the
    verdict itself carries, and catches a mislabeled verdict."""
    problems: list[str] = []
    screens = verdict.get("screens") or []
    if not isinstance(screens, list) or not screens:
        problems.append("verdict cites no screens")
        screens = []
    listed_any = any(s.get("listed") for s in screens)
    all_checked = bool(screens) and all(s.get("checked") for s in screens)
    expected = "STOP" if listed_any else ("GO" if all_checked else "REVIEW")
    if verdict.get("decision") != expected:
        problems.append(
            f"decision {verdict.get('decision')!r} does not follow from the "
            f"screens (the cited evidence implies {expected!r})")
    return (not problems), problems


def providers_from_env(env: dict) -> list:
    """Assemble live providers from env keys, always including the offline
    fixture so a screen still runs (as REVIEW-or-fixture) with no keys set.
    CHAINALYSIS_API_KEY and TRM_API_KEY enable the real feeds."""
    providers: list = []
    ck = env.get("CHAINALYSIS_API_KEY")
    if ck:
        providers.append(ChainalysisSanctionsProvider(ck))
    tk = env.get("TRM_API_KEY")
    if tk:
        providers.append(TRMSanctionsProvider(tk))
    if not providers:
        providers.append(OfflineSanctionsProvider())
    return providers
