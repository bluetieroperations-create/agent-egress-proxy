# Post-merge handoff — PR #3 is MERGED. Do NOT stack new commits on it.

**Status (2026-08):** PR #3 ("Blackwall: pre-signature x402 payment-verdict service")
was **merged into `main`** as merge commit `60c5a4b`. The branch
`claude/blackwall-x402-integration-j3rdab` and its 168 commits (up to `b1623e9`) are now
part of `main`'s history. **The PR is closed and finished — it can no longer track new
work.**

This document explains what to do — and what NOT to do — for any follow-up.

---

## The one rule

**A merged PR is done. New work is a NEW change on a fresh base, and a NEW pull request.**
Never reopen, re-push, or "continue" a merged PR by adding commits to the old branch tip.

---

## ✅ DO

1. **Start every follow-up from the latest `main`.** Reset the working branch onto it
   (the branch currently holds only already-merged history, so this discards nothing):

   ```sh
   git fetch origin main
   git checkout -B claude/blackwall-x402-integration-j3rdab origin/main
   # now HEAD == origin/main; make your fresh changes here
   ```

   (You may keep the same branch NAME — just re-base it onto `main` first. Or use a new
   name; either is fine.)

2. **Verify the base before committing.** `git log --oneline -1 origin/main` should be the
   merge commit (or newer). Your new commits sit ON TOP of it, never before it.

3. **Open a NEW pull request** for the follow-up. It targets `main` and will show only your
   NEW commits as the diff (main already contains everything through `b1623e9`). It is a
   *different* PR from #3 — do not describe it as "updating #3".

4. **Before every push:** `git fetch` + rebase onto the remote tip, then verify
   `local HEAD == remote HEAD` after pushing. (A same-branch overwrite has bitten this repo
   before — see `CLAUDE.md`.)

5. **If the push is a branch reset to `origin/main`** (the branch has only merged history),
   a `git push --force-with-lease` is the sanctioned way to move the stale branch ref
   forward. `--force-with-lease` (never plain `--force`) so you can't clobber someone
   else's push.

---

## ❌ DO NOT

1. **Do NOT add commits on top of the old branch tip (`b1623e9`) and treat them as
   continuing PR #3.** #3 is merged; those commits belong on a fresh main-based branch in a
   new PR.

2. **Do NOT re-merge the 168 already-merged commits.** If a future merge of this branch were
   done as a **squash** instead of a merge commit, the old branch history would NOT be an
   ancestor of `main`, and PR-ing the un-reset branch would re-introduce all 168 commits as
   "new" — a huge, confusing diff. Resetting onto `main` first prevents this regardless of
   how #3 was merged. (It was a merge commit, so history IS preserved — but reset anyway;
   it's the habit that's safe in all cases.)

3. **Do NOT `git revert` or `git reset --hard` to "undo" the merge** to make room for new
   work. The merge is the shipped history. Build forward.

4. **Do NOT plain `--force` push.** Use `--force-with-lease`, and only for the branch-reset
   case above.

5. **Do NOT reuse `receipt_id`s, commit trailers, or the PR-#3 identity** for new work — a
   new change is a new change.

---

## Quick "am I doing it right?" check

```sh
git merge-base --is-ancestor origin/main HEAD && echo "OK: my work builds on top of main" \
  || echo "STOP: my base is behind main -- reset onto origin/main first"
```

If that prints `STOP`, run the reset in the **DO #1** block before committing anything.

---

## Context for the next session

- The whole project now lives on `main`. `CLAUDE.md` is the map; `docs/HANDOFF.md` is the
  cross-project handoff (Blackwall + Traceipt live in one repo as two branches — do not
  merge them).
- Merging PR #3 **activated the `seed-refresh` and `seed-freshness` crons** (GitHub
  `schedule:` fires only from the default branch). They now run on their own; a guarded
  auto-refresh of the reputation corpus opens its own PRs via `.github/workflows/`.
- Full suite: **937 tests** (run command in `CLAUDE.md`). Keep it green; keep the
  behavioral oracle (`verdict_oracle.py`) byte-identical unless a verdict change is
  intentional (then regenerate the golden in the same commit).
