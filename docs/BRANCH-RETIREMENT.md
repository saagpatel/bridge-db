# Branch Retirement Policy

Use this policy when cleaning up local or remote bridge-db branches after PRs,
sprints, or maintenance sessions.

The goal is to remove obvious branch noise without deleting the only remaining
pointer to useful work or an unresolved decision.

## Safe To Delete

A branch is safe to delete when at least one of these is true:

- It is ancestor-merged into `main`.
- `git cherry -v main <branch>` shows every branch-side commit with `-`, meaning
  the patch content is already represented on `main`.
- Its tree is identical to current `main`, even if the commit topology differs
  because a connector or contents API recreated the same file state through
  different commits.
- A merged PR identifies the branch as its head ref and the merge is confirmed
  on GitHub.
- The branch was created during the current cleanup session, contains no unique
  useful work, and remote verification confirms no open PR depends on it.

For local branches, prefer `git branch -d`. Use `git branch -D` only when the
hook can prove patch-equivalence or identical tree state, or when the ref is
already in the explicit retired-ref namespace `archive/`.

## Preserve Or Archive

Do not delete a branch only because it is old, ugly, or inconvenient.

Preserve or rename it under `archive/` when:

- It contains non-equivalent commits that are not on `main`.
- It changes behavior in a way that may still be a useful option.
- It has no PR trail, owner decision, or written abandonment note.
- Deleting it would erase the easiest diff for a future decision.

For local-only branches, an `archive/<topic>-<date>` rename is usually enough.
For shared remote branches, leave the ref in place until there is explicit owner
or repo-policy approval to delete it.

Archived local refs are not permanent by default. After their evidence and
abandonment decision are recorded in the session report or bridge activity, the
local `archive/*` pointer may be deleted with `git branch -D`. This keeps
non-equivalent work from disappearing accidentally while still letting cleanup
finish once the branch has been intentionally retired.

## Evidence Checklist

Before deleting or archiving a branch, capture the current evidence:

```bash
git fetch --prune origin
git status --short --branch
git branch -vv --sort=refname
git branch -r --sort=refname
git merge-base --is-ancestor <branch> main
git cherry -v main <branch>
git rev-parse main^{tree} <branch>^{tree}
git rev-list --left-right --count --cherry-pick main...<branch>
git diff --stat main..<branch>
gh pr list --state all --head <branch-name> --json number,state,title,mergedAt,url,headRefName
```

Interpretation:

- Ancestor-merged: delete.
- Tree-identical to `main`: safe to delete locally; for remote branches, still
  confirm no active PR points at the branch before deleting.
- All `git cherry` rows are `-`: delete after confirming no active PR points at
  the branch.
- Branch-side non-equivalent commits exist: preserve, archive, or request an
  explicit abandonment decision.
- Branch-side non-equivalent commits already live under `archive/`: delete only
  after the abandonment decision has been recorded.
- The diff removes current schema, auth, workflow, or test surfaces from `main`:
  treat it as stale/regressive unless a current owner says otherwise.

## Current Disposition Pattern

When a branch cannot be safely deleted, record a short disposition in the
session report or bridge activity:

- Branch name
- Last commit date and subject
- Whether it is ancestor-merged
- Patch-equivalence summary
- Diff risk, such as "would remove current v10 CAS/provenance code"
- Decision: delete, archive locally, preserve remote, or request owner decision

This keeps branch cleanup factual instead of vibes-based.
