# Turnkey — the message (no email address exists to send to)

Checked: no published email for Turnkey or its founders. Only a **Contact Sales**
form on turnkey.com, and Bryce Ferguson's LinkedIn (CEO, ex-Coinbase Custody).
So this is written short enough for a contact form, and works as a LinkedIn note.

## Why Turnkey and not Privy

Privy's pre-signature screening slot is filled by **Blockaid** (also MetaMask,
Coinbase, Uniswap). Turnkey's own AI-Agents page names the slot and leaves it open:

> "Require co-approval from a user, operator, **or risk service** before executing
> high-value agent actions."

No risk-service partner is named. That sentence is the entire reason to write.

## The message

> **Subject:** Risk-service co-approval for agent payments
>
> Your AI-Agents page describes requiring co-approval from "a user, operator, or
> risk service" before high-value agent actions. I've built the risk-service side
> of that and wanted to see whether it's something you'd want to plug in.
>
> It answers a different question from transaction-security scanning. Blockaid-style
> tools ask "is this transaction malicious?" — drainer contracts, known-bad
> addresses. Mine asks "is this payment sane?": is the amount far off what this
> payee has actually settled for historically, do the signed EIP-3009 authorisation
> and the stated claim match, is the payee's payer set a wash farm, is the endpoint
> still serving what it advertises. A clean contract paying a fair-looking address
> at 40x the going rate isn't malicious — it's a bad payment, and it clears a
> malice check.
>
> It returns GO / HOLD / STOP before signing, and there's already a working Turnkey
> shim that maps an activity request into the check, with an explicit
> fail-open/fail-closed toggle for when the verdict service is unreachable.
>
> Two questions: is that co-approval slot open to third parties, and is this the
> shape of thing you'd want in it? Happy to send the technical detail or run it
> against sample activity.
>
> — [name], bluetier.operations@gmail.com

## Why it's shaped this way

- **Quotes their own sentence back.** It is the reason the message is not spam.
- **Names the incumbent and concedes it.** Blockaid is real, it works, and claiming
  to replace it would be wrong. The pitch only works as a complement.
- **The 40x line is the whole argument** in one sentence: malice and sanity are
  different questions, and one tool cannot be assumed to cover the other.
- **Mentions the shim exists** — `integrations/wallets/turnkey_signer.py` is real
  and tested, so this is not vapor. It does not overclaim beyond that.
- **Names the availability toggle unprompted.** Any wallet provider's first
  objection to a guard in the signing path is "what happens when you're down."
  Answering before they ask is the difference between a reply and silence.
- **Asks whether the slot is open** before pitching fit. If it is already
  contracted, that is a one-line answer and saves everyone the call.

## Honest caveats to keep in mind

- Absence of a published partner is not proof the slot is open. They may already
  have one unannounced.
- Turnkey is a $30M-Series-B company; a Gmail address from an unknown sender is a
  real credibility tax. The specificity of the quote is what has to carry it.
- This is a partnership/BD motion, not a self-serve sale. It is still entirely
  behind a computer, but it is slower than the security-researcher outreach.
