# Round 3: Spanish / Portuguese sources

Premise: every incumbent that killed rounds 1-2 was English-language and US-focused
(DOTestimate, BRIMR, 46brooklyn, Cirium, COLA Cloud). If the incumbent set is
language-bound, a Spanish- or Portuguese-language public dataset should be open.

Tested five. All five are occupied. The premise was wrong.

| # | Dataset | Free + bulk? | Incumbent found | Verdict |
|---|---------|--------------|-----------------|---------|
| 1 | **Brazil CNPJ** (Receita Federal) — every Brazilian company + its partners, ~85GB | Yes. Official downloads "extremamente lentos" | Casa dos Dados (CDN mirror), `aphonsoar/Receita_Federal_do_Brasil_-_Dados_Publicos_CNPJ` (public ETL), Base dos Dados, Power BI dashboards | DEAD — the slowness *is* the moat, and four parties already crossed it |
| 2 | **IMMEX Mexico** — maquiladora program registry | **No public registry.** Search returns no open-data endpoint. A purge of inactive programs is underway | n/a | DEAD — data does not exist publicly |
| 3 | **SECOP I / II Colombia** — public procurement, datos.gov.co Socrata, CSV/JSON/RDF/XML | Yes, genuinely open | INFOCONTRATOS (AI + predictive, 100% of contracts real-time), Colombia Compra Eficiente's own dashboards, plus four commercial SaaS: Highteck, Licitarus, LicitIA, BuscaSECOP | DEAD — most crowded of the five |
| 4 | **SAT Lista 69-B / EFOS Mexico** — 14,055 shell companies issuing fake invoices, monthly | Yes | datospublicos.mx/efos, ChecaFactura, tesio.com.mx, induxsoft — **and** the English-language gap is already closed: Apify hosts batch RFC→69-B screening actors, plus iAudita and Commenda | DEAD |
| 5 | **Nearshoring supplier due diligence** (the buyer, not a dataset) | n/a | Prodensa (supplier portal, IMMEX/VAT/REPSE checks), Ethixbase360 (TPRM), Importivity (vetted factories) | DEAD |

## The angle I thought was open, and why it isn't

Hypothesis: a small/mid US manufacturer nearshoring to Mexico has no affordable
English-language supplier-risk tool — Orbis, D&B, LexisNexis, World-Check are all
enterprise-priced, and the free Mexican tools are Spanish-only.

Checked it. Wrong. Apify already hosts SAT 69-B batch-screening actors written in
English and documented for exactly that use case ("vendor onboarding", "supplier due
diligence", "AML screening"). iAudita sells "SAT compliance in English." The
language barrier was the whole thesis and someone crossed it first.

## Running tally

17 datasets tested across three rounds. Zero survived. Same three causes every time:

1. **Free incumbent already exists** (13 of 17)
2. **The number measures the reporter, not reality** (NIH size, NHTSA counts, FAA SDR)
3. **Data locked or nonexistent** (MSHA aggregates, NMLS, FAA Part 108, IMMEX)

## The structural lesson

The search itself is the bad bet. Any public dataset discoverable in a few searches
is discoverable by everyone else in a few searches — that is what "public" means. The
base rate of finding an unclaimed one is not low, it is approximately zero, and three
rounds of evidence say so.

Two assets from this work were built, verified, and never taken to a buyer:

- **NIH like-for-like correction** — the public math is off by ~31 points
  (naive -26.6% vs true +4.1%). Measured, tested, unpublished.
- **PFAS exceedance list** — 1,717 named water systems over EPA limits, tied to
  $14B in settlements. Built and tested; incumbency never checked.

Neither is blocked by a competitor. Both are blocked by not having been sent to
anyone. That is a different problem from the one the last three rounds were solving.

---

# Follow-up: "can they build it, or have they?"

Right question. Finding data is not shipping a product, and several kills above were
made on the existence of a *mirror* rather than a *product*. Re-checked. Answer: they
have built it — including the two assets I had listed as unclaimed.

## The incumbents are products, not mirrors

- **Brazil CNPJ** — Casa dos Dados ships an API + webhooks. Linkana and MonitorCNPJ
  add real-time risk scoring, ownership-change alerts, sanctions and lawsuit
  monitoring. The change-detection layer I assumed was the gap is the product.
- **SAT 69-B** — native **Odoo ERP modules** flag a blacklisted supplier at invoice
  time; CrisKco runs scheduled re-screening pushed into SAP Business One, Contpaqi
  and Aspel. Not a lookup. Integrated monitoring.

## Both "unused assets" are already published

- **PFAS** — DEAD. **CheckPFAS** is a free independent tool mapping UCMR 5 to every
  US ZIP. **EWG** maintains an interactive national map, refreshed with EPA's March
  2026 data. And the exact figure I computed — *1,717 systems exceed EPA limits* —
  is already published verbatim in public writeups. I did not find something. I
  recomputed something.
- **NIH** — DEAD. The like-for-like correction is the **forward-funding** story, and
  it is reported by **Science/AAAS**, the **AAU**, and **STAT**: forward funding rose
  from 5-15% of new grant dollars in FY2024 to ~40% in FY2025, which is exactly why
  total obligations look stable while new-award counts fall 8.6%. Congress has since
  capped it. This is a covered story, not an overlooked one.

## Correctness note on the PFAS code

`pfas/pfas_exceedance.py` is now **out of date regardless of its commercial value**.
In May 2026 EPA moved to roll back limits on GenX (HFPO-DA), PFBS, PFNA and PFHxS,
retaining only PFOA and PFOS; the compliance deadline is proposed to slip 2029 -> 2031.
`LIMIT_NGL` still carries all five, so the module over-counts exceedances against the
current rule. Fix before any reuse.

## Conclusion

19 checks, 0 survivors — and the two "survivors" only survived because I had not
checked them. In every single case the data was found *and* built on by someone with
more resources and a head start.

The generalizable finding is not about any one dataset. It is that the gap I kept
looking for — "they have the data but nobody built the analytical layer" — did not
occur once in nineteen attempts. Public data attracts builders at a rate that closes
that gap faster than a solo search can find it. This approach should be abandoned,
not iterated.
