# 25: `entry_risk` → `level_risk`

**Branch:** `uq/25-level-risk` · **Depends on:** 20, 22 · **Blocks:** none

## Why

The judge's third ordinal is `entry_risk`, "the risk that this is NOT actually open to a
new graduate". With target bands (chunk 22), that becomes "the risk that this is not in
the candidate's target band". The prompt may name the target band here, unlike `level`'s,
because `judge` already reads personal `profile.yaml` prose and judges suitability.

## Read first

- `jobtracker/tasks/judge.py` (schema, prompt, parser), `jobtracker/rank.py`
  (`RISK_VALUE`, `score_posting`, the `entry_risk` weight)
- `profile.yaml` / `profile.example.yaml` weights (`entry_risk: -15`)
- the `rankings` schema (`entry_risk` column) and chunk 20's accessor pattern
- `CLAUDE.md` § *The ranking pass*

## Do

1. On the wire, use `level_risk`. Its description reads the target band names from
   preferences ("not in: entry"). The parser accepts the legacy `entry_risk`.
2. Keep the column `rankings.entry_risk` (additive migrations, rollback by tag) and read
   it through an accessor, as chunk 20 did.
3. The profile weight key becomes `level_risk`, and `entry_risk` is accepted with a
   WARNING for one release.
4. Bump `JUDGE_SCHEMA_VERSION` (chunk 20), and **fold the target-band list into the
   judge's unit key**, because the question now depends on it. Name the cost: changing
   targets re-judges matches. Note in `docs/ranking.md` that this, unlike `level`, does
   cost model calls, and why that is acceptable: only matches are judged.
5. Update `CLAUDE.md` § *The ranking pass* and `docs/ranking.md`.

## Tests

- `test_judge_prompt_names_target_bands`
- `test_parser_accepts_legacy_entry_risk`
- `test_target_change_rekeys_judge_units`
- `test_profile_entry_risk_weight_still_loads`

## Done when

Tests and lint pass.

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
