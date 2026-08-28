# Public history rewrite runbook

This runbook is for the one-time cleanup required before changing YawnBot from a private repository to a public repository.

It does **not** change the production server, `/opt/yawnbot`, the `production` Environment, runtime `.env`, NapCat state, or the forced-command deployment protocol.

## Why a rewrite is required

The current source tree is guarded by `tools/repo_guard.py`, but older reachable commits contain runtime/private paths such as historical environment files, SQLite WAL/SHM files, downloaded Fanqie content, developer-tool state, Windows cache files, and historical credential values. Deleting those paths only from HEAD is insufficient because reachable branch/tag history becomes accessible when the repository is public.

## Safety invariants

A rewrite is acceptable only if all of the following hold:

1. `tools/history_rewrite_plan.py` reports no removal target still tracked in the current branch tree.
2. Planning and rewriting operate on the same publishable-ref mirror containing real branches and tags only.
3. Historical credential values are redacted through an ephemeral replacement file whose contents are never logged or uploaded.
4. `tools/history_secret_audit.py` passes after rewriting the disposable mirror.
5. The current branch tree SHA is identical before and after rewriting.
6. Expected release tags survive the rewrite.
7. A repository-external private backup exists before any remote force-update.
8. The repository remains private until old production Actions logs and all other open-source blockers are resolved.

## Phase A — disposable-mirror validation

The `History rewrite dry-run` workflow fetches all real branches/tags, creates a separate bare mirror containing only those publishable refs, enumerates every file path in every reachable commit tree, produces exact path-removal and credential-redaction plans, runs `git-filter-repo`, and re-runs the full history audit.

The workflow has `contents: read` only. It never pushes rewritten refs. Path lists, replacement files, and rewritten history are kept in the runner temporary directory and are not uploaded as Actions artifacts.

The current validated dry-run also requires the source tree of the phase-2 branch to remain byte-identical and verifies that `main` plus retained release tags still exist after rewriting.

## Phase B — create a repository-external backup

Before changing remote refs, create a mirror clone outside this repository and keep it private/offline:

```bash
git clone --mirror git@github.com:Wohaokunr/yawn_bot.git yawn_bot-before-public-rewrite.git
cd yawn_bot-before-public-rewrite.git
git bundle create ../yawn_bot-before-public-rewrite.bundle --all
sha256sum ../yawn_bot-before-public-rewrite.bundle
```

Do not upload this bundle to this repository, its Releases, Issues, PRs, or Actions artifacts. It intentionally contains the history being removed.

Verify the backup before proceeding:

```bash
git bundle verify ../yawn_bot-before-public-rewrite.bundle
```

Keep both the bundle checksum and the original mirror until the public conversion has been completed and independently verified.

## Phase C — build the actual maintenance mirror

Use a clean checkout containing the approved planner/auditor as the tool source. Build a separate bare repository containing only real remote branches and tags:

```bash
SOURCE=/path/to/clean-checkout
REWRITE=/path/to/yawn_bot-public-rewrite.git
TMP=/path/to/private-temp
TARGET_BRANCH=main

rm -rf "$REWRITE"
git init --bare "$REWRITE"
git -C "$REWRITE" fetch git@github.com:Wohaokunr/yawn_bot.git \
  '+refs/heads/*:refs/heads/*' \
  '+refs/tags/*:refs/tags/*'

cd "$REWRITE"
python "$SOURCE/tools/history_rewrite_plan.py" \
  --current-ref "refs/heads/$TARGET_BRANCH" \
  --paths-out "$TMP/yawnbot-history-paths.txt" \
  --replacements-out "$TMP/yawnbot-history-replacements.txt" \
  --manifest-out "$TMP/yawnbot-history-manifest.json"
```

The replacement file contains historical credential values. Never print it, commit it, copy it into the repository, or upload it to GitHub.

Record the intended current tree before rewriting:

```bash
BEFORE_TREE="$(git rev-parse "refs/heads/$TARGET_BRANCH^{tree}")"
```

Run the exact rewrite:

```bash
git filter-repo --force --invert-paths \
  --paths-from-file "$TMP/yawnbot-history-paths.txt" \
  --replace-text "$TMP/yawnbot-history-replacements.txt"
```

Immediately enforce the tree-identity and history-audit gates:

```bash
AFTER_TREE="$(git rev-parse "refs/heads/$TARGET_BRANCH^{tree}")"
test "$BEFORE_TREE" = "$AFTER_TREE"
python "$SOURCE/tools/history_secret_audit.py"

for ref in \
  refs/heads/main \
  refs/tags/v0.1.0-rc.1 \
  refs/tags/v0.1.0-rc.2 \
  refs/tags/v0.1.0-rc.3 \
  refs/tags/v0.1.0-rc.4; do
  git show-ref --verify --quiet "$ref"
done
```

Commit SHAs are expected to change; the current source tree must not.

## Phase D — remote ref update

Remote history rewriting is a maintenance operation. Stop normal repository writes while it is in progress and keep the repository private.

Review which branches should remain public. Obsolete temporary branches are better deleted than retained solely to keep unnecessary history reachable. Every retained branch must point to its rewritten equivalent commit.

All retained release tags must also be moved to their rewritten equivalents. Existing GitHub Releases require a separate provenance review because their metadata references old commit SHAs. Do not claim an old checksum or provenance statement describes a rewritten commit unless the released payload has been independently verified as identical.

Only after the external backup, disposable dry-run, tree-identity gate, full audit, and retained-ref checks all pass should rewritten refs be force-updated.

Do not create a `backup/*` branch or tag on GitHub before the push; doing so would make the private history reachable again when the repository becomes public.

## Phase E — post-rewrite verification

Immediately after the force-update, create a fresh mirror from GitHub and audit the actual remote state rather than trusting the maintenance clone:

```bash
git clone --mirror git@github.com:Wohaokunr/yawn_bot.git post-rewrite-check.git
cd post-rewrite-check.git
python /path/to/clean-checkout/tools/history_secret_audit.py
```

Also verify:

- `main` contains the expected current source tree;
- normal CI runs on the rewritten `main`;
- retained release tags point to rewritten commits;
- no temporary backup ref remains on GitHub;
- no GitHub Release, Issue, PR attachment, or Actions artifact contains a copy of removed private history;
- production deployment still uses immutable image digest plus the existing server-side deployment protocol.

## Rollback

If a remote ref is moved incorrectly, restore it from the external mirror/bundle while the repository is still private. Do **not** create a GitHub backup ref containing the old history.

After rollback, re-run the full history audit and reassess the rewrite plan before retrying.
