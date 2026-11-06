# 19: `profile.yaml` becomes personal

**Branch:** `uq/19-profile-overlay` · **Depends on:** none · **Blocks:** none (touches `.gitignore`/Dockerfiles like 15; trivial merge)

## Why

`profile.yaml` is prose about the maintainer ("Graduating Spring 2027 … NYC is preferred"),
and it is tracked and baked into both images. Under the no-hardcoded-preferences rule it
becomes a gitignored personal file with a tracked example. `config.PROFILE_YAML` already
honours `$JOBTRACKER_PROFILE`.

## Read first

- `profile.yaml`, `jobtracker/profile.py` (`load_profile` **raises** on a missing file,
  and `prose_hash` covers only prose), and its callers `cli.py` ~line 1143 and ~line 1441
- `Dockerfile` line ~54 and `Dockerfile.serve` line ~76 (`COPY companies.yaml
  criteria.yaml profile.yaml ./`)
- `.gitignore`, `answers.example.yaml` (the example-file pattern)
- `docs/ranking.md`, `docs/deployment.md`, and CI's image capability assertions
  (`.github/workflows/`)
- `CLAUDE.md` § *The ranking pass* and § *Deployment*

## Do

1. `git mv profile.yaml profile.example.yaml`, then replace the personal prose with a
   neutral, commented example that keeps the same keys and weights block. Add
   `profile.yaml` to `.gitignore`.
   - **Report the original file's exact bytes** in your final report, so the user can
     restore them as their untracked `profile.yaml`.
   - The bytes must be identical so that `prose_hash` does not move and no judgment is
     redone.
2. A missing profile file:
   - `rank` still scores, with weights from the example's defaults (decide where those
     live, and say so);
   - `judge` reports `unavailable_reason` "no profile.yaml — copy profile.example.yaml";
   - nothing raises out of `work`.
   - A missing profile is not an error exit, the same as a switched-off task.
3. Dockerfiles stop copying `profile.yaml`. `docs/deployment.md` says both wrappers must
   mount it `:ro`, the same as `preferences.yaml`.
4. CI: if any job reads `profile.yaml`, point it at the example.
5. Update `CLAUDE.md` § *The ranking pass*: `profile.yaml` is personal and gitignored, and
   the example is tracked. Update `docs/ranking.md` to match.

## Don't

- Don't change the profile schema, the weights or `prose_hash`.

## Tests

- `test_missing_profile_judge_unavailable_rank_still_runs`
- `test_example_profile_loads`
- `test_prose_hash_unchanged_for_identical_bytes`

## Done when

Tests and lint pass, `docker build` succeeds if Docker is available (say if it isn't), and
the original bytes are in your report.

---

## Ground rules (same for every chunk)

- **Read `CLAUDE.md` in your worktree first.** It is the rules file, and it overrides
  anything here that it contradicts. If it does, stop and report rather than picking one.
  The source spec is
  `/home/dylan/code/projects/job-tracker/.plans/uncertain-queue-and-preferences-spec.md`.
  This chunk is self-contained, so read the spec only for background.
- **Tests:** a worktree has no `.venv`, and the package is not installed. From the
  worktree root, run
  `/home/dylan/code/projects/job-tracker/.venv/bin/python -m pytest tests -q`. That
  imports the worktree's own `jobtracker/`. Never use the system `python`, which has no
  pytest.
- **Lint:** use the same gate as CI, `uvx ruff check --select F,E9 jobtracker/ tests/`.
- **Data:**
  - Never open `/home/dylan/code/projects/job-tracker/data/state.db` through
    `store.connect()`, because connecting migrates it.
  - For corpus evidence, copy it to `$TMPDIR` and use the copy. It is stale (it stops on
    2026-07-23) and is evidence only, not live state.
  - The live database is on gx10. Do not try ssh, and do not touch gx10 at all.
- **No network in tests.** `sir-client` is not installed, so no test may assume it
  imports.
- **Commits:**
  - Make one commit per failure class.
  - Change `CLAUDE.md` and the relevant `docs/*.md` in the same commit as any rule they
    state.
  - End every message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - Do not push, merge or rebase onto other chunk branches.
- **Scope:** stay inside **Do**. If something outside it looks wrong, write it under
  "Found, not fixed" in your final report.
- **Final report:**
  - what changed, with commits;
  - test and lint output;
  - every place you departed from this chunk, and why;
  - anything in "Found, not fixed".
