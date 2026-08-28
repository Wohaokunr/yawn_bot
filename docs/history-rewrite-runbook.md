# Public history rewrite runbook

This runbook is for the one-time cleanup required before changing YawnBot from a private repository to a public repository.

It does **not** change the production server, `/opt/yawnbot`, the `production` Environment, runtime `.env`, NapCat state, or the forced-command deployment protocol.

## Why a rewrite is required

The current source tree is guarded by `tools/repo_guard.py`, but older reachable commits contain runtime/private paths such as historical environment files, SQLite WAL/SHM files, downloaded Fanqie content, developer-tool state, and Windows cache files. Deleting those paths only from HEAD is insufficient for a public repository because reachable branch/tag history remains accessible.

## Safety invariants

A rewrite is acceptable only if all of the following hold:

1. `tools/history_rewrite_plan.py` reports no sensitive path still tracked in current HEAD.
2. The disposable-mirror workflow completes successfully.
3. `tools/history_secret_audit.py` passes after rewriting the disposable mirror.
4. The current HEAD tree SHA is identical before and after rewriting.
5. Expected release tags survive the rewrite.
6. A repository-external backup exists before any remote force-update.
7. The repository remains private until old production Actions logs and all other open-source blockers are resolved.

## Phase A — disposable-mirror validation

The `History rewrite dry-run` workflow fetches all branches/tags, creates an exact removal list using the same path classifier as the history audit, clones a temporary mirror, executes `git-filter-repo`, and re-runs the audit.

No rewritten refs are pushed by this workflow and no history bundle is uploaded as an Actions artifact.

The path list is intentionally not uploaded because it describes private historical layout. The temporary mirror is destroyed with the runner.

## Phase B — create a repository-external backup

Before changing remote refs, create a mirror clone outside GitHub and keep it private/offline:

```bash
git clone --mirror git@github.com:Wohaokunr/yawn_bot.git yawn_bot-before-public-rewrite.git
cd yawn_bot-before-public-rewrite.git
git bundle create ../yawn_bot-before-public-rewrite.bundle --all
sha256sum ../yawn_bot-before-public-rewrite.bundle
```

Do not upload this bundle to this repository, its Releases, Issues, or Actions artifacts. It intentionally contains the history being removed.

Verify the backup before proceeding:

```bash
git bundle verify ../yawn_bot-before-public-rewrite.bundle
```

## Phase C — rewrite a separate maintenance mirror

Create another mirror for the actual rewrite rather than modifying the backup:

```bash
git clone --mirror git@github.com:Wohaokunr/yawn_bot.git yawn_bot-public-rewrite.git
cd yawn_bot-public-rewrite.git
python /path/to/clean-checkout/tools/history_rewrite_plan.py \
  --paths-out /tmp/yawnbot-history-paths.txt \
  --manifest-out /tmp/yawnbot-history-manifest.json

git filter-repo --force --invert-paths \
  --paths-from-file /tmp/yawnbot-history-paths.txt

python /path/to/clean-checkout/tools/history_secret_audit.py
```

Before pushing, compare the intended current branch tree with the rewritten current branch tree. Commit SHAs are expected to change; the source tree must not.

## Phase D — remote ref update

Remote history rewriting is a maintenance operation. Stop normal repository writes while it is in progress.

The affected long-lived branches currently include `main` and any branch still intended to remain after cleanup. Old temporary branches should preferably be deleted rather than preserved solely to keep dirty history reachable.

All public release tags that are retained must point to their rewritten equivalent commits. Because old release assets contain clean-checkout deployment packages but their GitHub Release provenance points at old commit SHAs, Releases must be reviewed after tag rewriting. Do not claim old checksums/provenance describe a newly rewritten commit unless the released payload has been independently verified as identical.

Only after the backup and disposable validation are complete should rewritten branch/tag refs be force-updated.

## Phase E — post-rewrite verification

Immediately after the force-update:

```bash
git clone --mirror git@github.com:Wohaokunr/yawn_bot.git post-rewrite-check.git
cd post-rewrite-check.git
python /path/to/clean-checkout/tools/history_secret_audit.py
```

Also verify:

- `main` contains the expected current source tree;
- CI runs on the rewritten `main`;
- release tags point to rewritten commits;
- no temporary backup ref remains on GitHub;
- no GitHub Release, Issue, PR attachment, or Actions artifact contains a copy of removed private history;
- production deployment still uses immutable image digest + the existing server-side deployment protocol.

## Rollback

If a remote ref was moved incorrectly, restore it from the external mirror/bundle while the repository is still private. Do **not** create a `backup/*` GitHub branch containing the old history, because such a branch would make the removed objects reachable again when the repository becomes public.

After rollback, re-run the full history audit and reassess the rewrite plan before retrying.
