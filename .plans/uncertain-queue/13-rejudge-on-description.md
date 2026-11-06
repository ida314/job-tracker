# 13: Redo downstream work when a description changes

**Branch:** `uq/13-rejudge-on-description` · **Depends on:** none (works without 03; coordinate if 03 is in flight) · **Blocks:** none

## Why

Chunk 12 lets a description change once (`feed` → `ats`). Downstream caches do not notice
it:

- `judge` is keyed on `profile.prose_hash` only (`tasks/judge.py`, `unit_key=…prose_hash`);
- `tailor` is keyed on `resume_hash` and `keywords_hash`;
- `coverletter` is keyed on its own `_unit_key(ctx)`.

So a posting judged on metadata keeps that judgment after real prose arrives.

## Read first

- `jobtracker/tasks/judge.py`, `jobtracker/store.py::matches_needing_judgment` (~line
  1857), the `rankings` schema (~line 164)
- `jobtracker/tasks/tailor.py` (`_unit_key`, `pending`), `jobtracker/tasks/coverletter.py`
  (`_unit_key`, ~line 369)
- `CLAUDE.md` § *The task queue* (`unit_key` is the question) and § *The ranking pass*

## Do

1. Add `store.description_hash(text) -> str` (`sha256[:16]`, `''` for NULL). This one
   function is used by everything below.
2. Add a migration `rankings.description_hash TEXT`, through `_apply_column_migrations`.
   - `judge`'s writer stores it.
   - `matches_needing_judgment` also selects rows where
     `COALESCE(r.description_hash,'') != <current hash>`.
   - Compute the hash in SQL if a deterministic SQL expression exists; otherwise filter in
     Python. Document which.
   - Existing rows with a NULL hash must **not** all re-judge at once on upgrade. Backfill
     the hash from the current description on first connect, logging the count, so only
     later changes re-ask.
3. The per-posting units of `tailor` and `coverletter` include the description hash: in
   each unit's identity if a unit is one posting, else in its payload check. Read how each
   builds its key first. The goal is that a changed description makes that posting's unit
   pending again without re-keying every other posting. Same backfill rule: no mass
   re-ask on upgrade.
4. Update `CLAUDE.md` § *The task queue*: "a description change re-asks `judge`, `tailor`
   and `coverletter` for that posting only".

## Don't

- Don't change scoring, prompts or `level` (chunk 03 handles its own hash).

## Tests

- `test_description_upgrade_rejudges`
- `test_upgrade_does_not_mass_rejudge_existing_rankings`
- `test_tailor_unit_pending_again_after_description_change`
- `test_other_postings_not_rekeyed`

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
