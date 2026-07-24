"""
enrich.py -- the enrichment layer (the thing that justifies the price).

A product's raw data is cheap/scattered; the *enriched verdict* (a clean
structured summary + flags + confidence) is what an agent pays $0.20 for
instead of doing the aggregation itself.

Two implementations:
  * StubEnricher  -- deterministic, no LLM, no network. Lets the whole platform
    run and be tested offline, and is a fine baseline for structured data.
  * ClaudeEnricher -- calls the Claude API for natural-language synthesis.
    Documented and wired, but NOT invoked unless config.enricher == "claude"
    and a key is present (kept out of the default path so tests stay hermetic).

Products call `ctx.enricher.summarize(...)`; swapping the backend changes
nothing in a product.
"""
from __future__ import annotations

import os


class Enricher:
    def summarize(self, *, kind, subject, facts, flags):
        """Return a dict: {"summary": str, "confidence": float, "model": str}.
        `facts` is the normalized data; `flags` are pre-computed risk booleans."""
        raise NotImplementedError


class StubEnricher(Enricher):
    """Deterministic, offline. Builds a readable summary from facts+flags with
    no model call. Confidence is derived from how many source fields are
    populated -- a real, if simple, signal."""

    def summarize(self, *, kind, subject, facts, flags):
        active = [name.replace("_", " ") for name, on in flags.items() if on]
        if active:
            headline = "%s shows: %s." % (subject, "; ".join(active))
        else:
            headline = "%s: no adverse signals in available records." % subject
        # confidence = fraction of expected fields that came back populated
        populated = sum(1 for v in facts.values() if v not in (None, "", [], {}))
        total = max(1, len(facts))
        confidence = round(populated / total, 2)
        return {"summary": headline, "confidence": confidence,
                "model": "stub-v1"}


class ClaudeEnricher(Enricher):
    """Natural-language synthesis via the Claude API. Only used when explicitly
    configured; falls back to a stub-style summary if the SDK/key is absent so
    a misconfig never takes the endpoint down."""

    def __init__(self, model="claude-sonnet-5"):
        self.model = model
        self._fallback = StubEnricher()

    def summarize(self, *, kind, subject, facts, flags):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return self._fallback.summarize(
                kind=kind, subject=subject, facts=facts, flags=flags)
        try:
            import anthropic  # optional dep; only imported on this path
        except ImportError:
            return self._fallback.summarize(
                kind=kind, subject=subject, facts=facts, flags=flags)
        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            "You are a %s analyst. Given these normalized records, write a "
            "2-3 sentence factual risk summary for an autonomous agent. Only "
            "state what the data supports.\n\nSubject: %s\nFacts: %s\nFlags: %s"
            % (kind, subject, facts, {k: v for k, v in flags.items() if v}))
        msg = client.messages.create(
            model=self.model, max_tokens=300,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return {"summary": text.strip() or subject, "confidence": 0.9,
                "model": self.model}


def build_enricher(name):
    if name == "claude":
        return ClaudeEnricher()
    return StubEnricher()
