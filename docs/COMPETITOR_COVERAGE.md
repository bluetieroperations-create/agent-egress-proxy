# What competitors actually screen — measured from tool schemas

The plan was to pay $0.001 for `lion_wallet_screen` and see what it returned.
That was blocked (no `eth-account`, no funded signer). The substitute turned out
to be **stronger evidence**, and free.

**A tool cannot reason about a value it is never given.** So instead of buying one
response, read the input schemas: they bound what a tool can *ever* do.

## The direct competitors

Pulled live from their own `tools/list`:

| Tool | Input parameters | Takes an amount? |
|---|---|---|
| `lionx402 :: lion_wallet_screen` | `address` | **No** |
| `lionx402 :: lion_multi_sanctions_bundle` | `address`, `name`, `domain` | **No** |
| `lionx402 :: lion_compliance_bundle` | `address`, `domain`, `token`, `name`, `receipt` | **No** |
| `trust-score :: trust_score_evaluate` | `target`, `checks` | **No** |

`lion_wallet_screen` advertises *"PASS/WARN/BLOCK before paying anyone"* and its
entire input is one address. It is structurally incapable of judging an amount.

## The whole ecosystem, from the committed reading

Across **127,403 tool definitions**, tools whose NAME identifies them as
counterparty screening (screen / sanction / verdict / compliance / AML / KYC /
rug / honeypot / trust-score / risk-check):

| | |
|---|---|
| Screening tools | **913** |
| ...accepting an amount parameter at all | **22 (2.4%)** |
| ...genuinely amount-aware on inspection | **4** |

Most of the 22 are false positives of the keyword match — `size` for pagination,
`price` on a stock screener, `summary` matching on "sum".

**A methodological note, because the first pass was wrong.** Matching on tool
*descriptions* rather than names returned 6,512 "risk tools" of which 23.4% took
an amount — a meaningless number that swept in trading and DeFi tools
(`amountIn` on a sandwich-attack tool, `pageSize`, `accepts_usdc`). Matching on
the tool NAME is what makes the 913 defensible.

## What the four amount-aware ones actually do

All four are **threshold** checks. None compares an amount to what the payee has
historically charged:

| Tool | What the amount is for |
|---|---|
| `sentinel-compliance :: travel_rule_screen` | FATF R16 travel-rule threshold |
| `Piwe :: aml_screen` | OFAC/UN watchlists; amount incidental |
| `dpx :: ramp.compliance_screen` | CTR-equivalent flag at $10K |
| `ghosbc-safety-gate :: screen_consequential_action` | a **caller-declared** amount ceiling |

The last is the closest and still different in kind: the caller supplies the
limit. It does not know what the payee normally charges, so it cannot tell a fair
price from a 40x one — only whether the caller's own stated cap was exceeded.

## The finding

**The x402 ecosystem screens WHO you are paying. Essentially nobody screens HOW
MUCH.**

Of 913 screening tools, zero compare a quoted amount against that payee's own
settled history. That is `blackwall.price_anomaly_ratio`, and on this corpus it
is unoccupied.

## Honest limits

- Schemas bound what a tool can accept, not what it does internally. A tool could
  in principle fetch the amount itself from a passed resource URL — none of the
  four descriptions suggests that, but it is not excluded.
- Only the **7,677** servers that listed tools are covered. The 3,300 gated ones
  were never inspected.
- "Unoccupied" is not "valuable". It establishes the gap is real; it says nothing
  about whether anyone will pay to fill it. That question is still open and still
  only answerable by the outreach.
