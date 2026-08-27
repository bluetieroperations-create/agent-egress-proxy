# Going a layer below the obvious sectors

**Date:** 2026-08-25 · Targeting buyers with **no private data alternative** — usually
someone crossing into a sector they do not operate in.

## 1. MSB license gap — best structure found in this session

**The fact that makes it work:** federal registration is a *notice filing, not a
license.* A company can appear in FinCEN's MSB registry and still lack the state money
transmitter licenses it needs to legally operate there.

Both sides are free and public:
- **FinCEN MSB Registrant Search** — who registered federally
- **NMLS Consumer Access** — which state licenses they actually hold

The gap between them — *registered federally, operating in states where it is
unlicensed* — is computable and is a genuine red flag.

**Buyer:** banks. They de-risk MSBs constantly, they must verify before onboarding, and
today they do it manually, one customer at a time. No vendor found selling license-gap
monitoring; Alloy and the AML platforms do identity and screening, not this.

**Better shape than the data-broker idea.** That one produced a list of accusations. This
one answers a bank's question about *one counterparty it is already considering*. A false
positive costs the bank an extra check, not someone's reputation.

**The catch, measured:** there is **no bulk download**. The FinCEN registrant search is a
web form, and the published "MSB Registration List" PDF on fincen.gov is **current as of
December 2011** — 38,633 registrants, fourteen years stale. NMLS Consumer Access is a
lookup form too.

So acquisition means scraping two government portals, one of which is a state-regulator
system likely to resist automation — the same browser-automation cost that made the
Chapter 11 idea expensive.

> The friction is simultaneously the moat and the bill. That is the trade in every idea
> still standing.

## 2. Grain elevator bond adequacy — real problem, weak buyer

State agriculture departments license grain warehouses and publish bond amounts. Farmers
who store grain lose everything when an elevator fails, and the bonds are routinely far
too small:

- Nebraska (Pierce): **$9M+ in unpaid farmer claims** against **$880k of bonds** across
  three locations. Claimants recovered roughly **10 cents on the dollar**.
- Michigan: about **60 elevator failures**, ~**$13M** in producer losses.
- Texas: more than a dozen failures in recent years.

**The computable signal:** bond amount versus grain actually stored — a coverage ratio
per licensed elevator, from public licensing data.

**Why I am not pushing it:** the people harmed are farmers, who do not buy data. The
buyers with money — grain buyers, insurers, state regulators — are few and slow. Real
problem, poor market.

## 3. Funeral homes — dead

The FTC Funeral Rule requires price disclosure **in person and by phone, not online.**
The amendment to mandate online pricing remains open and unadopted as of 2026. **There is
no dataset.** Killed in one search.

## The filter that produced these

Earlier rounds failed because the buyer already had better private data. So this round
searched for buyers who structurally cannot:

- Banks assessing money-transmitter customers (a bank is not an MSB)
- Grain buyers assessing elevators
- Anyone underwriting a sector they do not operate in

That filter is worth keeping. It is the first one that produced a candidate whose buyer
genuinely lacks an alternative.

## Next, if MSB continues

1. Confirm whether NMLS Consumer Access permits automated queries at all. If it hard
   blocks, cost rises sharply. **Check this first — it can kill the idea.**
2. Ask two bank BSA officers: *"when you onboard an MSB, how do you verify its state
   licenses, and how long does it take?"* If the answer is "we check NMLS by hand, it
   takes an hour," there is a product. If they already have a vendor, there is not.
