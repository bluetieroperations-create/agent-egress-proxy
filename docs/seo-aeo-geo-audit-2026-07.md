# SEO / AEO / GEO Audit — BlueTier Operations portfolio

**Date:** 2026-07-31
**Scope:** Black_Wall (blackwalltier.com), Traceipt (traceipt.xyz), the "x402 + Blackwall" query space, and the BlueTier Operations brand entity.
**Method:** Live fetches of both domains (homepage HTML, HTTP headers, `robots.txt`, `sitemap.xml`, `llms.txt`, `.well-known`), npm + PyPI registry metadata, and web searches for brand and category queries as they resolve today.

- **SEO** — classic search engine visibility (Google/Bing crawling, indexing, ranking).
- **AEO** — Answer Engine Optimization: being the extracted answer in featured snippets, and in answer engines like Perplexity / ChatGPT Search / Google AI Overviews.
- **GEO** — Generative Engine Optimization: being present in the corpora and live-retrieval surfaces LLMs cite (llms.txt, package registries, MCP directories, GitHub, ecosystem listings).

---

## Executive summary

| Property | Technical SEO | AEO readiness | GEO readiness | Current visibility |
|---|---|---|---|---|
| **blackwalltier.com (Black_Wall)** | **B+** — near-complete on-page setup | **B** — strong assets, terminology drift | **A-** — llms.txt is genuinely best-practice | **Low** — third parties (PyPI, Glama) outrank the domain for its own brand |
| **traceipt.xyz (Traceipt)** | **F** — broken `<head>`, soft-404s everywhere | **F** — no extractable metadata at all | **F** — no llms.txt, no registry/ecosystem footprint | **Zero** — not surfacing for its own name |
| **"x402 Blackwall" query space** | n/a | — | — | **Zero** — no association exists in search or AI answers |
| **BlueTier Operations (entity)** | n/a | — | — | **Near-zero** — collides with unrelated Australian firms; only Glama listings surface |

**The one-line diagnosis:** Black_Wall has done the on-page work and now has an *authority and entity-consistency* problem; Traceipt has strong copy trapped in a technically broken page and is invisible; and nothing yet connects the portfolio to the x402 ecosystem it's positioned around, or to a coherent "BlueTier Operations" entity.

---

## 1. Black_Wall — blackwalltier.com

### What's already right (keep all of this)

Verified live on 2026-07-31:

- `<title>` ("Black_Wall // Pre-action outcome API for AI agents"), meta description, full OpenGraph + Twitter Card set with 1200×630 image, canonical, favicon set.
- **JSON-LD `SoftwareApplication`** with `Offer` (free tier), `provider` Organization (BlueTier Operations), keywords, `softwareHelp`.
- **robots.txt explicitly allowlists AI crawlers** (GPTBot, OAI-SearchBot, ClaudeBot, PerplexityBot, Google-Extended…) and declares the sitemap.
- **sitemap.xml** with 7+ URLs and **hreflang alternates (en/es/pt)** on `/failure-modes`.
- **`/llms.txt`** is exemplary: positioning, agent usage protocol, OpenAPI link, npm/MCP install, integrations, failure-mode taxonomy, benchmark, incidents. Also `llms-full.txt` and a `.well-known/blackwall-signing-keys.json` with a `docs` pointer.
- HSTS, correct security headers, fast static serving on Vercel (~0.8s full fetch).
- PyPI `blackwall-sdk` has complete `project_urls` (Homepage, Docs, Source, Issues, "Free API key") and good keywords.

This is a top-decile GEO setup for a small site. The problems are elsewhere.

### Findings

**BW-1 (High) — The domain doesn't rank for its own brand.**
Searches for `"blackwalltier.com" OR "Blackwall Tier"` return PyPI (`blackwall-sdk`), Glama (`blackwall-mcp`, `blackwall-mcp-remote`)… but not blackwalltier.com itself in top results. Answer engines currently reconstruct what Black_Wall is *from third-party listings*, not from your site. Likely causes: young domain, near-zero inbound links, and no webmaster-tools submission. Actions:
- Register the site in **Google Search Console** and **Bing Webmaster Tools** (Bing feeds ChatGPT Search and Copilot), submit the sitemap, and check the Index Coverage report for surprises.
- Enable **IndexNow** (trivial on Vercel — a key file plus a ping on deploy) so Bing/Seznam/Yandex pick up changes immediately.

**BW-2 (High) — Severe brand-name collision, and the site doesn't fight it.**
The "Blackwall" SERP is owned by Cyberpunk 2077 lore, Dragon Age companions, three London Wikipedia entries, and — worst — **blackwall.com, an actual bot-protection security company** (formerly BotGuard). A security buyer who hears "Blackwall" and googles it lands on a competitor-adjacent company. Mitigations:
- Consistently pair the name with a disambiguator everywhere off-site: "**Black_Wall by BlueTier Operations**" or "**Black_Wall (blackwalltier.com)**" in every README, directory listing, and post.
- Own the two-word brand queries you *can* win: "Blackwall Tier", "Black Wall AI agents", "blackwall MCP". The npm/PyPI keyword `blackwall` already helps; add `blackwall-tier` and `blackwalltier` as keywords/aliases.

**BW-3 (High) — Brand-string fragmentation confuses entity resolution.**
Across your own surfaces the product is written as `Black_Wall`, `BLACK_WALL` (npm description), `Blackwall` (package names), and "Blackwall Tier" (how searchers/AI describe it, from the domain). Knowledge graphs and LLMs treat these as weak signals of the *same* entity only if you tie them together. Actions:
- Add `"alternateName": ["Blackwall", "BLACK_WALL", "Blackwall Tier"]` to the `SoftwareApplication` JSON-LD.
- Pick one canonical rendering (recommend **Black_Wall**) and use it in the first sentence of every off-site description.

**BW-4 (High) — No `sameAs` / entity graph in the structured data.**
The JSON-LD has no `sameAs` links and the `provider` Organization's URL just points back at blackwalltier.com. Add to the JSON-LD:
- `sameAs`: GitHub org (`github.com/bluetieroperations-create`), npm package page, PyPI project page, Glama listings, and any social profiles.
- Give the `provider` Organization its own `@id`, and consider a small `/about` page for BlueTier Operations that carries an `Organization` schema (see §4).

**BW-5 (Medium) — Verdict terminology drifts across surfaces.**
Meta description says gating is "**GO/CONFIRM/STOP**"; the JSON-LD and llms.txt say recommendation is "**GO / CAUTION / STOP**" with a separate gate "**AUTO / CONFIRM / HUMAN_REQUIRED**". AEO extraction rewards exact-string consistency — an answer engine quoting your meta description will describe the API wrong. Fix the meta description to match the API's real vocabulary.

**BW-6 (Medium) — No FAQ/Q&A content or `FAQPage` schema.**
Answer engines are question-driven. The site has excellent assets (`/failure-modes`, `/benchmark`, `/incidents`) but no page answering the queries people actually type: "how do I stop an AI agent from running destructive SQL", "what is a pre-action gate for AI agents", "how to gate AI agent actions before execution", "AI agent prompt injection guardrail API". Add an FAQ section (with `FAQPage` JSON-LD) and/or short question-titled pages. The category SERP is currently owned by Snyk, Salesforce, Cequence, Frontegg, agno, Cloudanix — you will not beat them on "AI guardrails", but the long-tail question space ("pre-action", "preflight", "before it runs") is winnable and matches your exact positioning.

**BW-7 (Medium) — npm package has no `homepage` field.**
`blackwall-mcp@1.4.1` has `homepage: None`. npm is a first-class GEO surface (LLMs read registry metadata constantly). Add `homepage: "https://blackwalltier.com"` and a `repository` field to package.json and republish. (PyPI is already correct — mirror that.)

**BW-8 (Medium) — Authority deficit: ~2 referring domains.**
Only Glama and PyPI link in. Highest-leverage, zero-budget link/GEO targets, roughly in order:
1. **Official MCP servers registry** (modelcontextprotocol registry) + **Smithery, PulseMCP, mcp.so, Cursor's MCP directory** — each is crawled by every answer engine for "MCP server for X" queries.
2. **awesome-mcp-servers**, **awesome-ai-safety / awesome-llm-security** GitHub lists.
3. A **Show HN** and a **Product Hunt** launch (the incidents page — "real-world AI-agent disasters, cited" — is genuinely HN-shaped content).
4. Integration write-ups in LangChain/CrewAI/AutoGen community showcases (you already ship those integrations in `blackwall-integrations`).

**BW-9 (Low) — Sitemap entries have no `<lastmod>`.** Add lastmod (deploy-time) so crawlers prioritize refreshed pages; also confirm `/es/` and `/pt/` pages return self-referencing canonicals.

**BW-10 (Low) — Docs are a text file.** `softwareHelp` points to `openapi.yaml` and human docs live in `llms-full.txt`. That's great for LLMs, weak for SEO — HTML docs pages (even thin ones wrapping the same content) create indexable, linkable surface for query intent like "black_wall api docs".

---

## 2. Traceipt — traceipt.xyz

The copy is strong ("Your agents pay for things. Traceipt proves it.", the 402→receipt walkthrough, Ed25519/Merkle/offline-verify trust bullets). Technically, however, the page is invisible to every engine class.

### Findings

**TR-1 (Critical) — `<title>` is outside `<head>`; the `<head>` is nearly empty.**
The served HTML closes `</head>` at byte ~132 after only charset+viewport; `<title>Traceipt: receipts for machines that spend money</title>` appears *inside `<body>`*, along with all styles. Browsers error-correct this; parsers, snippet generators, and social scrapers often don't. There is **no meta description, no OpenGraph, no Twitter Card, no canonical, no favicon, no JSON-LD — nothing extractable**. This single template bug nullifies the entire on-page layer. Fix the document structure so head contains: title, meta description, canonical, OG + Twitter set (with an OG image — the mock receipt visual is the obvious asset), favicon, JSON-LD.

**TR-2 (Critical) — Catch-all rewrite: every path returns HTTP 200 with the homepage.**
Verified: `GET /zzz-does-not-exist` → **200**, 18,924 bytes (identical to `/`). Consequently `robots.txt`, `sitemap.xml`, and `llms.txt` all "exist" as HTML soft-404s. Effects: crawlers see infinite duplicate URLs, robots.txt is garbage (Google tolerates it but Bing and smaller AI crawlers behave unpredictably), no sitemap is discoverable, and site quality signals are poor. Fix the host config (Cloudflare in front — likely a Pages/Workers SPA fallback) to: serve real `robots.txt` / `sitemap.xml` / `llms.txt` as text, and return **404** (or 200 only for real routes) for unknown paths.

**TR-3 (High) — No structured data.** Add `SoftwareApplication` (or `Service`) + `Organization` (provider: BlueTier Operations, `sameAs` → blackwalltier.com, GitHub) + `FAQPage` for the compliance questions the page already answers implicitly ("will an auditor accept x402 payments", "how do I reconcile AI agent payments", "x402 receipt verification").

**TR-4 (High) — Zero index presence and zero x402 association.**
Searches for `"traceipt"` return nothing about the product; searches for x402 audit trails return Apiosk, Nevermined, FluxA, Tangle, x402b — a competitor set actively occupying the exact "audit-ready x402 payments" positioning. Actions:
- After TR-1/TR-2, register with Search Console + Bing Webmaster Tools, submit sitemap, enable IndexNow.
- **Get listed in the x402.org ecosystem directory** and the x402-foundation GitHub ecosystem/awesome lists — this is the single most-retrieved surface for "x402 + <anything>" queries in answer engines.
- Publish 2–3 question-shaped pages targeting the winnable long tail: "x402 receipts", "audit trail for x402 payments", "how finance teams reconcile AI agent spending". The homepage copy about "ten thousand tiny payments with no invoice, no vendor, no reason" is already the perfect seed for this.

**TR-5 (Medium) — Ship an llms.txt.** Copy the Black_Wall pattern verbatim (positioning → how an agent should use it → API → verification endpoint → keys). Traceipt's audience is literally agents and the developers wiring them; this is the highest-ROI GEO artifact available.

**TR-6 (Medium) — Not connected to the family.** Nothing on traceipt.xyz links to blackwalltier.com or vice versa (Black_Wall's llms.txt lists integrations but not Traceipt). Cross-link them in footers, llms.txt files, and `sameAs`/`provider` schema so engines learn one entity ships both. "Signed receipts" is even shared vocabulary — Black_Wall has a receipts-verify endpoint; make the conceptual link explicit on both sites.

**TR-7 (Low) — `.xyz` TLD carries spam priors** in some filters. Not worth migrating over, but it raises the bar on entity signals (schema, registry listings, cross-links) — all the more reason to do TR-3/TR-6 thoroughly.

---

## 3. "x402 Blackwall" query space

Today the association does not exist: x402 queries return Coinbase/QuickNode/Eco/x402.org, and appending "blackwall" returns nothing — search engines ask "did you mean the game?" This matters because agentic-payments is the portfolio's clearest wedge narrative: **Black_Wall gates the payment before it happens; Traceipt proves it after it clears.** Two locks, same story as this very repo's README.

Actions:
1. A joint page/post on both domains: "Guardrails and receipts for x402 payments" — Black_Wall's `unverified payments` failure mode + Traceipt's signed receipt, one diagram, install snippets for both.
2. x402.org ecosystem + x402-foundation GitHub listings for both products (the Stripe/PayPal/Coinbase guards in `blackwall-integrations` are the credential for Black_Wall's entry).
3. Use exact co-occurring strings LLMs can retrieve: "x402 pre-action gate", "x402 payment receipts", "x402 audit trail" in titles/H2s — these are the queries the competitor set (Apiosk, Nevermined, x402b) is currently winning by default.

---

## 4. BlueTier Operations — brand entity

`"bluetier operations"` currently resolves to: Bluetier Consulting (Tasmanian infrastructure firm, bluetier.com.au), Save the Blue Tier (environmental group), ZoomInfo stubs — and, for the actual org, only Glama's author pages. The GitHub handle `bluetieroperations-create` reads as an auto-generated artifact and is currently the org's most-cited identifier, which fragments the entity further.

Actions:
1. **Create one canonical entity home** — a `/about` page on blackwalltier.com (or a tiny bluetieroperations site) carrying `Organization` JSON-LD: legal name "BlueTier Operations", `sameAs` → GitHub org, npm, PyPI, Glama, both product domains.
2. **GitHub org profile README** (`.github` repo) naming the portfolio: Black_Wall, Traceipt, blackwall-sdk, blackwall-integrations, this egress proxy — with one-liners and links. GitHub org pages rank easily for brand queries and are heavily retrieved by LLMs.
3. Standardize the public string to **"BlueTier Operations"** (one word "BlueTier", capital T) in every schema `provider`, package `author`, and directory listing.
4. Every product's JSON-LD `provider` should reference the same Organization `@id` so the graph connects: BlueTier Operations → {Black_Wall, Traceipt}.

---

## 5. Prioritized action plan

### P0 — this week (fixes broken things)
| # | Action | Property |
|---|---|---|
| 1 | Fix HTML structure: title/meta/OG/canonical/JSON-LD into a real `<head>` | traceipt.xyz |
| 2 | Kill the catch-all 200: real robots.txt, sitemap.xml, 404s for unknown paths | traceipt.xyz |
| 3 | Register both domains in Google Search Console + Bing Webmaster Tools; submit sitemaps; enable IndexNow | both |
| 4 | Republish `blackwall-mcp` with `homepage` + `repository` fields | npm |
| 5 | Align verdict vocabulary (GO/CAUTION/STOP + AUTO/CONFIRM/HUMAN_REQUIRED) across meta description, JSON-LD, llms.txt | blackwalltier.com |

### P1 — this month (builds the entity)
| # | Action | Property |
|---|---|---|
| 6 | Add `sameAs` + `alternateName` to Black_Wall JSON-LD; ship Traceipt JSON-LD; shared Organization `@id` | both |
| 7 | Cross-link the family: footers, llms.txt files, about page with Organization schema, GitHub org profile README | all |
| 8 | Traceipt llms.txt (clone the Black_Wall pattern) | traceipt.xyz |
| 9 | Submit to MCP directories (official registry, Smithery, PulseMCP, mcp.so, Cursor) and awesome-lists | Black_Wall |
| 10 | x402.org ecosystem + x402-foundation listings for both products | both |
| 11 | FAQ content + `FAQPage` schema targeting question-shaped queries | both |

### P2 — this quarter (earns authority)
| # | Action | Property |
|---|---|---|
| 12 | Joint "guardrails + receipts for x402" narrative page; target "x402 receipts / x402 audit trail" co-occurrence | both |
| 13 | Show HN + Product Hunt (lead with the incidents page or the benchmark) | Black_Wall |
| 14 | HTML docs pages (wrap llms-full.txt / OpenAPI content) | blackwalltier.com |
| 15 | Long-tail content program: pre-action/preflight question pages; finance-reconciliation pages for Traceipt | both |
| 16 | Monitor AI-answer citations monthly (ask ChatGPT/Perplexity/Claude "what is Black_Wall / Traceipt / who provides x402 receipts" and track drift) | all |

---

## Appendix — raw evidence (2026-07-31)

- `https://blackwalltier.com/` → 200, 78,415 bytes, Vercel, HSTS; full head metadata + 1× JSON-LD (`SoftwareApplication`).
- `https://blackwalltier.com/robots.txt`, `/sitemap.xml`, `/llms.txt`, `/.well-known/blackwall-signing-keys.json` → all valid, contents as described above.
- `https://traceipt.xyz/` → 200, 18,924 bytes, Cloudflare; `</head>` precedes `<title>`; zero meta/OG/schema.
- `https://traceipt.xyz/zzz-does-not-exist` → **200** (soft-404); `robots.txt` / `sitemap.xml` / `llms.txt` all return the homepage HTML.
- npm `blackwall-mcp@1.4.1`: description + keywords present, `homepage: null`.
- PyPI `blackwall-sdk 0.1.0`: complete `project_urls`, good keywords.
- Brand searches: "blackwalltier.com / Blackwall Tier" → PyPI + Glama + games/geography, domain absent; "traceipt" → nothing relevant; "x402 blackwall" → no association; "bluetier operations" → Australian consultancy + Glama author pages.
