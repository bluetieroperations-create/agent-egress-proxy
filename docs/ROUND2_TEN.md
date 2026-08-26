# Round 2: ten candidates, with the incumbency check up front

**Date:** 2026-08-25 · Rules unchanged: free data, remote sale, incumbency checked first.

## Agentic AI is picked over — checked, not assumed

| idea | verdict |
|---|---|
| MCP server security scanning | **DEAD.** Snyk ToxicSkills (3,984 skills scanned), Cisco AI Defence, MCP-Scan, mcp-scanner, MCPGuard, plus free scanners. |
| robots.txt / AI crawler blocking | **DEAD.** Cloudflare network-wide reports, Presenc AI adoption tracker, technologychecker. |
| AI crawler monetization | **DEAD.** Cloudflare owns it end to end — Pay Per Crawl → Pay Per Use, GoDaddy partnership across 82M domains. |
| FAA Part 108 / BVLOS drones | **DEAD for now.** Still at proposed-rule stage. No data exists yet. |

**The lesson:** AI is the most-watched field on earth. "Overlooked" and "AI" rarely
co-occur. One AI idea survived and it made the list below.

## The ten

Verification is stated per row. Only two were checked properly.

| # | Idea | Free data | Status |
|---|---|---|---|
| 1 | **EU AI Act training-data summaries** — mandatory public disclosure since Aug 2025; every GPAI provider must publish what its model was trained on, to a fixed EU template | EU template filings | ✅ **No aggregator found.** Small N, high profile |
| 2 | **Unregistered data brokers (CA)** — CalPrivacy is fining unregistered brokers **$200/day per consumer**; the registry is public, so the gap between "registered" and "obviously a broker" is computable | CA registry + DROP | ✅ **Enforcement live**, 345k deletion requests. No gap-detection product found |
| 3 | **Mandated salary ranges in job posts** — pay-transparency laws now force posted pay bands in CO, CA, WA, NY and more. Unlike Levels.fyi this is *legally mandated*, not self-reported | Job postings | ❓ Unchecked — Levels.fyi/Payscale exist but on self-reported data |
| 4 | **EU CSRD sustainability filings** — first big wave 2025; thousands of firms newly publishing granular machine-readable data | XBRL/ESEF filings | ❓ Unchecked. Large and genuinely new |
| 5 | **Financial Data Transparency Act conversions** — US financial regulators are being forced to make filings machine-readable, 2024–2026 | New structured feeds | ❓ Unchecked. New format = new products |
| 6 | **Hospital price-transparency non-compliance** — CMS publishes enforcement actions; the list of who is *still* not compliant is derivable | CMS enforcement data | ❓ Unchecked. Turquoise/Serif do rates, not compliance |
| 7 | **Medicare drug price negotiation** — first negotiated prices effective 2026; ripple effects across formularies are computable | CMS | ❓ Unchecked. Likely crowded — pharma is well covered |
| 8 | **EPA e-Manifest** — every hazardous waste shipment, per facility, public 90 days later. A factory activity index | RCRAInfo | ❓ From set B, never tested |
| 9 | **NHTSA complaints** — daily since 1949; complaint velocity precedes recalls | data.gov | ❓ From set B, never tested |
| 10 | **State DOT bid lettings** — awarded contracts with material quantities; a forward order book for cement and aggregates | 50 state DOTs | ❓ From set B, never tested |

## Which two I would check next

**#2 (unregistered data brokers)** — because the money is already moving. A regulator is
actively fining people, the penalty is specific ($200/day/consumer), and the buyer is
obvious: the brokers themselves want to know if they are exposed, and law firms want the
list. It is also small enough to test in a day.

**#1 (AI Act training-data summaries)** — because it is the only AI idea that survived,
it is brand new, and the disclosures are legally required and comparable by template.

## Rule I am carrying forward

Three of the last five deaths were a **free incumbent**. Two were the data not meaning
what I needed. So the order is fixed: **who already sells this → does the number mean
what I need → only then build.** No exceptions, including when something looks obvious.
