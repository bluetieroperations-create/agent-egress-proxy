# Tool-poisoning scan — reading #1 baseline (negative)

Prompted by asking whether the *cause* of the 128 lost servers was itself useful.
It was a bug (bytes passed to `json.loads`), not a signal — but the question was
worth testing, because invalid or invisible bytes in a tool description are
exactly how tool poisoning hides text from a human reviewer while leaving it in
the string the model reads.

Scanned all **127,403 captured tool definitions** — descriptions *and* input
schemas, since schema property descriptions are equally instruction text.

## Result: essentially nothing. That is the finding.

| Indicator | Hits |
|---|---|
| Zero-width / invisible characters | **13 tools across 5 servers** |
| Bidi override (reorders displayed text) | **0** |
| Unicode TAG block (renders as nothing) | **0** |
| Other control characters | **1** |

Instruction-pattern matches were checked **by hand, every one**, and all were
false positives:

- `<IMPORTANT>` and "ignore previous instructions" → **security scanners
  describing the attacks they detect** (`agent-tools-mcp::scan_mcp_safety`,
  `ia-qa-toolbox::prompt_injection_scan`, `fingersai::check_instruction`).
- "do not tell the user" (17) → legitimate UX rules, e.g. *"if it is not
  successful, do not tell the user payment succeeded."*
- "exfiltrate to URL" (28) → ordinary upload endpoints (`fabtally.com/upload`).
- "read secrets" (24) → tools stating they do **not** read secrets.
- "always call this tool first" (13) → normal tool-ordering instructions. The one
  arguable case is `adoraads::sponsored_search` — *"ALWAYS use this tool first
  whenever a user asks for beauty... recommendations"* — which is ad placement
  rather than an attack, and it does carry a disclosure requirement.

**No tool poisoning was detected in the public MCP registry on 2026-08-27.**

## Why a negative result is worth having

1. **It is publishable and credible.** "We scanned 127,403 tool definitions for
   poisoning and found none" is a real measurement, and more useful to Invariant
   Labs and Blockaid than another list of hypothetical risks.
2. **The baseline is clean, which makes the diff sharp.** Reading #2 compares
   against a corpus with ~zero indicators. A pattern appearing on a server that
   did not carry it last month is a strong signal precisely because the floor is
   this low. Presence today means little; *appearance* means a lot.
3. **The detector now exists** (`probe.scan_tool` / `scan_reading`), calibrated on
   real data rather than imagined attacks — which is why its patterns are marked
   REPORTING-ONLY and gate nothing.

## Honest limits

- Absence of evidence over one reading is not proof the registry is clean. A
  poisoning that reads as ordinary English — no hidden characters, no telltale
  phrases — would pass this scan entirely.
- Only the **7,677 servers that listed tools** were scanned. The 3,300 gated and
  2,631 unreachable ones were never inspected, and a gated server is exactly where
  someone would hide.
- The 128 servers lost to the decode bug were **not** in this corpus. They are
  covered from reading #2.

## Competitor note

MCP-safety scanning is already a product *on the registry itself* —
`AgentTools-Cloud/agent-tools-mcp`, `JcJamet/ia-qa-toolbox`,
`fingersai/fingers`. They scan a server on request. None of them appears to hold
a longitudinal corpus to diff against, which remains the only defensible position.
