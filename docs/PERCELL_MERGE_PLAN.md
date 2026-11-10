# PerCell repository consolidation plan

Consolidate four GitHub repositories into one repository named `PerCell` whose history contains
every version, so that any older version can be checked out by tag.

| Version | Current repo | Tag to create |
|---|---|---|
| 1 | `microscopy-analysis-single-cell` | `v0.1.0` |
| 2 | `PerCell` | `v0.2.0` |
| 3 | `PerCell3` | `v0.3.0` |
| 4 | `PerCell4` (current development) | `v0.4.0` |

Work happens in a clone of `PerCell4`; that repo becomes `PerCell` at the end.

Claude Code: read the whole plan before starting. Every phase has a verification step — do not move
on until it passes. Confirm the decisions in the next section with Joshua before Phase 0.

---

## Decisions (recommended defaults — confirm before starting)

1. **Full history, linked as a lineage.** Every commit from all four repos ends up in the new repo,
   joined by "supersede" merge commits so `git log` walks back from v0.4.0 through v0.1.0.
   Alternative not chosen: keep only PerCell4's history and add tagged snapshots of v1–v3 — simpler,
   but throws away the per-commit history of three repos for no real saving.

2. **Rename, don't delete.** Rename GitHub `PerCell` (v2) → `PerCell2`, then `PerCell4` → `PerCell`.
   GitHub redirects the old URLs, so nothing anywhere breaks. Afterwards, archive the three old repos
   (read-only, still visible). Nothing is deleted at any point.

3. **Versions live in three places:** annotated git tags (the source of truth), the version the
   package reports about itself (`pyproject.toml`), and a short `CHANGELOG.md`. GitHub Releases are
   optional and can be added later with one command per tag.

4. **No history rewriting.** No rebase, no filter-repo, no force-push. Old commits keep their hashes.

---

## Result

```
main:   ... PerCell4 commits ... ──M4  (tag v0.4.0, tree = PerCell4 tip)
                                    │
        ... PerCell3 commits ... ──M3  (tag v0.3.0, tree = PerCell3 tip)
                                    │
        ... PerCell  commits ... ──M2  (tag v0.2.0, tree = PerCell v2 tip)
                                    │
        ... microscopy-analysis-single-cell commits ...  (tag v0.1.0)
```

`M2`, `M3`, `M4` are merge commits whose tree is exactly the newer repo's tip. `git log --first-parent`
shows only PerCell4's history plus one merge; plain `git log` shows everything.
`git checkout v0.2.0` reproduces the v2 repo exactly.

---

## Phase 0 — Safety and inventory

Nothing in this phase changes anything.

### 0.1 Mirror backups

```bash
mkdir -p ~/percell-backups && cd ~/percell-backups
for r in microscopy-analysis-single-cell PerCell PerCell3 PerCell4; do
  git clone --mirror "https://github.com/<gh-user>/$r.git" "$r.git"
done
```

A mirror clone holds every ref of the repo. These are the rollback for everything below.

### 0.2 Fresh working clone of PerCell4

```bash
cd ~/percell-merge  # or wherever; not inside an existing clone
git clone "https://github.com/<gh-user>/PerCell4.git" PerCell
cd PerCell
git status            # must be clean
git branch --show-current   # note the default branch name; called "main" below
```

If Joshua has uncommitted work in an existing PerCell4 clone, commit or stash it there first;
this plan uses a fresh clone.

### 0.3 Add the old repos as remotes (no tags)

```bash
git remote add v1 "https://github.com/<gh-user>/microscopy-analysis-single-cell.git"
git remote add v2 "https://github.com/<gh-user>/PerCell.git"
git remote add v3 "https://github.com/<gh-user>/PerCell3.git"
git fetch --no-tags v1 v2 v3
```

`--no-tags` so any stray tags in the old repos don't collide with the ones we create.

### 0.4 Record each repo's default branch

```bash
for r in v1 v2 v3; do echo "$r: $(git remote show $r | sed -n 's/.*HEAD branch: //p')"; done
```

Substitute the answers for `<v1-branch>`, `<v2-branch>`, `<v3-branch>` below (likely `main` or
`master`).

### 0.5 Check whether the histories are related

```bash
git merge-base v1/<v1-branch> v2/<v2-branch> || echo "v1/v2 unrelated"
git merge-base v2/<v2-branch> v3/<v3-branch> || echo "v2/v3 unrelated"
git merge-base v3/<v3-branch> main            || echo "v3/v4 unrelated"
```

- **Unrelated** (expected if each version started as a fresh copy): Phase 1 uses
  `--allow-unrelated-histories`.
- **Related** (a newer repo was cloned from the older one with history): drop
  `--allow-unrelated-histories` for that pair. If the older tip is already an ancestor of the newer
  tip (`git merge-base --is-ancestor A B`), the supersede merge is unnecessary for that pair — just
  tag the older tip and continue.

Report which case applies to Joshua before continuing.

### 0.6 Check for large files

```bash
git rev-list --objects --all \
  | git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' \
  | awk '$1=="blob" && $3 > 10000000' | sort -k3 -n -r | head
```

GitHub rejects files over 100 MB and warns over 50 MB. If anything appears here (committed test
images, model weights), stop and ask Joshua — the fix (Git LFS or leaving that version's history out)
is a decision, not a step.

**Verification for Phase 0:** four mirror backups exist; working tree clean; three remotes fetched;
branch names recorded; related/unrelated known for each pair; no large-file surprises.

---

## Phase 1 — Build the lineage

All local. Nothing is pushed until Phase 3.

### 1.1 Tag v1

```bash
git tag -a v0.1.0 v1/<v1-branch> -m "PerCell 0.1.0 (microscopy-analysis-single-cell)"
```

### 1.2 v2 supersedes v1

```bash
git checkout -b lineage v2/<v2-branch>
git merge --allow-unrelated-histories -s ours v0.1.0 \
  -m "Supersede v0.1.0 with PerCell v2

Tree is the tip of the PerCell (v2) repository. Second parent is the full
history of microscopy-analysis-single-cell (v0.1.0)."
git tag -a v0.2.0 -m "PerCell 0.2.0"
```

`-s ours` is the merge *strategy* named "ours": the result's tree is exactly the current branch's
tree, and the merged-in history becomes the second parent. This is what "supersede" means here.
(Not `-X ours`, which is a conflict-resolution option and would attempt a real content merge.)

### 1.3 v3 supersedes v2

```bash
git checkout -B lineage v3/<v3-branch>
git merge --allow-unrelated-histories -s ours v0.2.0 \
  -m "Supersede v0.2.0 with PerCell3

Tree is the tip of the PerCell3 repository. Second parent carries v0.2.0 and v0.1.0."
git tag -a v0.3.0 -m "PerCell 0.3.0"
```

### 1.4 v4 (main) supersedes v3

```bash
git checkout main
git merge --allow-unrelated-histories -s ours v0.3.0 \
  -m "Supersede v0.3.0 with PerCell4

Tree is unchanged from PerCell4. Second parent carries v0.3.0, v0.2.0 and v0.1.0."
git tag -a v0.4.0 -m "PerCell 0.4.0"
git branch -D lineage
```

### 1.5 Verify

Each of these must hold:

```bash
# Tags exist and are in main's history
git tag -l 'v0.*'                                  # four tags
for t in v0.1.0 v0.2.0 v0.3.0; do git merge-base --is-ancestor $t main && echo "$t ok"; done

# Each tag's tree is byte-identical to the old repo's tip
git diff --stat v0.1.0 v1/<v1-branch>   # empty
git diff --stat v0.2.0 v2/<v2-branch>   # empty
git diff --stat v0.3.0 v3/<v3-branch>   # empty
git diff --stat v0.4.0 origin/main      # empty — main's tree unchanged by the merge

# The graph looks like the diagram above
git log --oneline --graph --first-parent -n 5
git log --oneline --graph --all | head -40

# Old versions check out cleanly
git checkout v0.2.0 && ls && git checkout main
```

If any diff is non-empty, stop: something merged content instead of superseding.

---

## Phase 2 — Version management in the code

Do this on `main` after Phase 1, as ordinary commits.

### 2.1 The package version

Inspect PerCell4 first: does it have a `pyproject.toml`, a `setup.py`, a `__version__` anywhere?
Report to Joshua, then apply the option he picks:

**Option A — static version (simplest).** In `pyproject.toml`, `version = "0.4.0"`. Bumped by hand
(or by Claude Code) whenever a version is cut. Risk: forgetting to bump, so the tag and the package
disagree.

**Option B — derived from tags (one source of truth).** With setuptools:

```toml
[build-system]
requires = ["setuptools>=68", "setuptools-scm>=8"]
build-backend = "setuptools.build_meta"

[project]
dynamic = ["version"]

[tool.setuptools_scm]
```

The installed package then reports `0.4.0` at tag `v0.4.0` and `0.4.1.devN+g<hash>` for commits
after it. Nothing to bump; tagging *is* releasing. Requires installing from a git checkout (not a
bare directory copy). If PerCell uses a different build backend, use its equivalent (`hatch-vcs`
for hatchling, `poetry-dynamic-versioning` for poetry).

If code needs the version at runtime, read it from the installed metadata rather than a hardcoded
string:

```python
from importlib.metadata import version
__version__ = version("percell")
```

Recommended: **Option B**, unless PerCell4 has no packaging yet — then Option A, and revisit.

### 2.2 CHANGELOG.md

Create `CHANGELOG.md` in "Keep a Changelog" form. Content for the old versions can be one line each
— Joshua fills in more if he wants:

```markdown
# Changelog

## [Unreleased]

## [0.4.0] — <date of PerCell4's last commit before the merge>
PerCell4. Hexagonal (ports-and-adapters) refactor in progress.

## [0.3.0] — <date>
PerCell3.

## [0.2.0] — <date>
PerCell (v2).

## [0.1.0] — <date>
microscopy-analysis-single-cell. First version.
```

Dates: `git log -1 --format=%as <tag>`.

### 2.3 README note

Add a short "Versions" section to the README:

```markdown
## Versions

This repository contains the full history of PerCell. Earlier versions were separate repositories
and are available as tags:

    git checkout v0.1.0   # microscopy-analysis-single-cell
    git checkout v0.2.0   # PerCell (v2)
    git checkout v0.3.0   # PerCell3
    git checkout v0.4.0   # PerCell4, the base of current development
```

### 2.4 Commit

```bash
git add -A
git commit -m "Add version management: changelog, package version, README versions section"
```

**Verification for Phase 2:** `pip install -e .` succeeds; `python -c "import importlib.metadata as m; print(m.version('percell'))"` prints `0.4.0` (Option B, at the tag) or the static value (Option A). Adjust the distribution name if PerCell's isn't `percell`.

---

## Phase 3 — GitHub

Order matters: the name `PerCell` must be free before `PerCell4` can take it.

### 3.1 Rename the repositories

Using the GitHub CLI (`gh auth status` first):

```bash
gh repo rename PerCell2 -R <gh-user>/PerCell     # v2 out of the way
gh repo rename PerCell  -R <gh-user>/PerCell4    # v4 takes the name
```

Or via Settings → General → Repository name in the web UI, in the same order.

GitHub keeps redirects from `PerCell4` → `PerCell`, so existing clones keep working. The old
`PerCell` name now points to the merged repo, which is what we want.

### 3.2 Point the local clone at the new name and push

```bash
git remote set-url origin "https://github.com/<gh-user>/PerCell.git"
git push origin main
git push origin --tags
```

Plain push, not force. The merge commits are descendants of PerCell4's existing `main`, so this is a
fast-forward.

### 3.3 Archive the old repositories

```bash
gh repo archive <gh-user>/microscopy-analysis-single-cell -y
gh repo archive <gh-user>/PerCell2 -y
gh repo archive <gh-user>/PerCell3 -y
```

Archived repos are read-only and clearly labelled. Optionally edit each one's description to
"Superseded — see <gh-user>/PerCell (tag v0.N.0)".

### 3.4 Optional: GitHub Releases

```bash
for t in v0.1.0 v0.2.0 v0.3.0 v0.4.0; do
  gh release create "$t" --title "PerCell $t" --notes "See CHANGELOG.md"
done
```

Skip unless Joshua wants a downloadable zip per version on the GitHub page.

**Verification for Phase 3:** `https://github.com/<gh-user>/PerCell` shows PerCell4's code, four
tags, and the full commit count; `https://github.com/<gh-user>/PerCell4` redirects there; the three
old repos show the "archived" banner.

---

## Phase 4 — Cleanup

```bash
git remote remove v1
git remote remove v2
git remote remove v3
```

Any other local clone of PerCell4 on Joshua's machines: `git remote set-url origin <new url>` then
`git pull`. The redirect means this is optional, but explicit is better.

Keep `~/percell-backups` for a few weeks, then delete.

---

## Going forward: cutting a version

1. Update `CHANGELOG.md`: move `[Unreleased]` items under a new `[0.5.0] — date` heading.
2. Option A only: bump `version` in `pyproject.toml`.
3. `git commit -am "Release 0.5.0"`
4. `git tag -a v0.5.0 -m "PerCell 0.5.0"`
5. `git push origin main --follow-tags`
6. Optional: `gh release create v0.5.0 --notes-file <notes>`

---

## Rules for Claude Code

- Never `push --force`, never rebase or rewrite existing commits, never delete a repository.
- Stop and ask if: a supersede diff is non-empty, a large file turns up, a merge-base exists where
  none was expected, or a GitHub rename fails.
- Do Phase 0 completely before any of Phase 1; nothing is pushed before Phase 3.
- Report the Phase 0 findings (branch names, related/unrelated, sizes) before starting Phase 1.

## Rollback

- Phases 0–2 are local: delete the clone and start over from the mirrors.
- Phase 3.1 renames are reversible with the same command in reverse order.
- Phase 3.2 adds commits and tags to `PerCell4`'s history; it never removes anything. Worst case,
  `git push origin --delete <tag>` per tag and reset `main` to its pre-merge commit
  (recorded in `~/percell-backups/PerCell4.git`) — the only step that would need a force-push, and
  only if Joshua decides to abandon the merge entirely.
- Phase 3.3 archiving is undone with `gh repo unarchive`.
