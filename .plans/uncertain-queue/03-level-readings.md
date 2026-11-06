# 03: Record every level reading, and stop reading metadata

**Branch:** `uq/03-level-readings` · **Depends on:** 01, 02 · **Blocks:** 08, 23

## Why

- **`unclear` is not recorded.** The runner counts it as `no_answer`, so each unclear unit
  uses up `MAX_ATTEMPTS` (3) model calls and is then set aside as "blocked". From then on
  it cannot be told apart from a transport failure, and nothing records that the model
  actually read it.
- **`level` still reads Simplify's metadata-only descriptions.** That is not work the task
  can do, and CLAUDE.md requires `pending()` to return only work the task can do.

## Read first

- `jobtracker/tasks/level.py`, `jobtracker/tasks/runner.py` (`_finish`, `blocked_units`,
  the `result is None` branch), `jobtracker/tasks/base.py`
- `jobtracker/store.py::uncertain_for_resolution`, `record_attempt`, `blocked_units`
- `CLAUDE.md` § *The task queue* and § *The ambiguity pass*
- Chunk 02's `postings.description_source` (already merged into your base)

## Do

1. **Add the table** `level_readings(company, ats_job_id, band TEXT NOT NULL, evidence
   TEXT NOT NULL, source_hash TEXT NOT NULL, read_at TEXT NOT NULL, PRIMARY KEY(company,
   ats_job_id))`, using `CREATE TABLE IF NOT EXISTS` in `_SCHEMA` (a new table is safe
   there, unlike a new column).
   - `band` takes the values `entry`, `not_entry` and `unclear` for now. Chunk 23 widens
     them without changing the table's shape.
   - `source_hash` is `sha256(shown)[:16]`, where `shown` is the string chunk 01 builds.
2. **Every grounded answer writes a reading, `unclear` included.**
   - A run returning `unclear` must reach `apply()` rather than returning `None`.
   - Introduce a small result type, or another explicit way, so `apply()` writes the
     reading and, only for `entry`/`not_entry`, the override and verdict as today.
   - The unit is then `ok` and its consecutive-failure count resets.
   - Ungrounded or malformed answers stay `None`, so transport failures and junk still
     count as failures.
3. **`pending()` excludes:**
   - rows with `description_source = 'feed'`;
   - rows that already have a reading whose `source_hash` matches the current `shown`.
     Compute the hash in Python. This is what makes a description upgrade (chunk 12)
     re-ask automatically.
4. **Add `store.level_reading(conn, company, ats_job_id)`** as a reader for chunk 08. No
   page shows readings yet.
5. Update `CLAUDE.md` § *The ambiguity pass* and § *The task queue*:
   - `unclear` is an answer and is written, like `inbox`'s "nothing here". Rewrite the
     existing sentence that contrasts `inbox` with `level`'s `unclear`.
   - Prose-less feed rows are not `level` work.
   - Update `docs/llm.md` and `docs/tasks.md` to match.

## Don't

- Don't change verdict logic, the prompt or bands (chunk 23).
- Don't touch existing overrides (chunk 06).

## Tests

- `test_unclear_is_recorded_and_not_retried`: `pending()` no longer returns the unit,
  and `task_attempts` shows it `ok`.
- `test_prose_less_feed_row_is_not_level_work`
- `test_changed_description_is_asked_again`: a different `source_hash` puts the unit back
  in `pending()`.
- `test_ungrounded_answer_still_counts_as_failure`
- `test_reading_written_even_when_override_is_yours`: when a human override exists,
  `apply` returns `"yours"`, the verdict is untouched, and the reading row is still
  written.

## Done when

Tests and lint pass. `pending_count` on a fixture with 3 unclear, 2 feed and 1 fresh row
returns 1.

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
