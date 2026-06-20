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
- A merged PR identifies the branch as its head ref and the merge is confirmed
  on GitHub.
- The branch was created during the current cleanup session, contains no unique
  useful work, and remote verification confirms no open PR depends on it.

For local branches, prefer `git branch -d`. Use `git branch -D` only when the
hook can prove patch-equivalence or the operator explicitly approves preserving
the evidence elsewhere first.

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

## Evidence Checklist

Before deleting or archiving a branch, capture the current evidence:

```bash
git fetch --prune origin
git status --short --branch
git branch -vv --sort=refname
git branch -r --sort=refname
git merge-base --is-ancestor <branch> main
git cherry -v main <branch>
git rev-list --left-right --count --cherry-pick main...<branch>
git diff --stat main..<branch>
gh pr list --state all --head <branch-name> --json number,state,title,mergedAt,url,headRefName
```

Interpretation:

- Ancestor-merged: delete.
- All `git cherry` rows are `-`: delete after confirming no active PR points at
  the branch.
- Branch-side non-equivalent commits exist: preserve, archive, or request an
  explicit abandonment decision.
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
