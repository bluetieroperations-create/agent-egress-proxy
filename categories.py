#!/usr/bin/env python3
"""
categories.py -- best-effort SERVICE-CATEGORY heuristics for an x402 resource.

A tiny, dependency-free classifier shared across the x402 tooling: given a resource
URL (or a payee's set of URLs), return a coarse service category -- what the endpoint
SELLS. Kept in its own stdlib-only module (no ecosystem_scan / verdict imports) so any
consumer -- Blackwall's `ecosystem_scan`, or Traceipt tagging a receipt -- can reuse it
cheaply.

DESCRIPTIVE ONLY. The category is derived from the seller-controlled resource URL, so
it is advisory metadata for analytics/reporting -- it must NEVER gate a verdict, key a
billing decision, or be treated as an assertion of fact. Classification of arbitrary
API paths is inherently fuzzy; ~a third of a live crawl lands in "other", which is
expected, not a defect. Extend CATEGORY_RULES as the ecosystem's vocabulary grows.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

CATEGORY_UNCLASSIFIED = "other"

# (slug, regex) matched IN ORDER against a resource's "host path" (lowercased);
# first hit wins. Order encodes precedence for overlaps (e.g. a commerce checkout
# on an ".ai" host is commerce, not ai-agents).
CATEGORY_RULES = (
    ("commerce",  r"gift|product|checkout|invoice|esim|shop|store/|cart|/buy|order|refill|billing|catalog"),
    ("dev-tools", r"cors|/hash|hmac|/json|/curl|http-status|/echo|/proxy|status-check|base64|/uuid|regex|/dns|whois|domain-|geocode|/resolve|translate"),
    ("onchain",   r"/chain|/block|/tx|erc20|erc721|token-|/nonce|/receipt|contract|/gas|/rpc|holder|/wallet|/address|/ens|mainnet|quicknode|/node|solana|onchain"),
    ("ai-agents", r"\.ai|/ai/|llm|gpt|deepseek|/chat|completion|/ask|/agent|inference|/mcp|/model|prompt|embed|/rag|/analyze|generate|summar"),
    ("finance",   r"quote|/trade|perp|yield|swap|/price|/market|signal|macro|finance|/fund|/risk|portfolio|defi|tradfi|altcoin|sentiment|equity|stablecoin|/rates|/stock|ticker|earnings|coin"),
    ("search-data", r"search|extract|scrape|/fetch|/data|edgar|filing|/company|weather|timezone|/geo|/trend|lookup|/report|hackernews"),
    ("content-media", r"content|news|recipe|joke|/fact|/feed|/tick|/image|/video|meme|/story|/blog|entertainment|twitter"),
    ("email-comms", r"/mail|email|/send|inbox|/message|notify|/sms"),
    ("storage-files", r"upload|storage|/file|ipfs|/pin"),
    ("identity-security", r"/auth|verify|sanction|kyc|captcha|identity|/login"),
)


def classify_resource(url):
    """Category slug for a single resource URL (host+path keyword match), or
    CATEGORY_UNCLASSIFIED. PURE."""
    u = urlparse(url or "")
    hay = ((u.hostname or "") + " " + (u.path or "")).lower()
    for name, pat in CATEGORY_RULES:
        if re.search(pat, hay):
            return name
    return CATEGORY_UNCLASSIFIED


def classify_category(resources):
    """Best-effort dominant service category for a payee across its resource URLs.
    A classified category beats 'other' on a tie, so a single unclassifiable URL
    among classified ones doesn't hide the signal. PURE + heuristic."""
    counts = {}
    for url in resources or []:
        c = classify_resource(url)
        counts[c] = counts.get(c, 0) + 1
    if not counts:
        return CATEGORY_UNCLASSIFIED
    # dominant by count; on ties prefer a real category over 'other', then name.
    return max(counts, key=lambda c: (counts[c], c != CATEGORY_UNCLASSIFIED, c))
