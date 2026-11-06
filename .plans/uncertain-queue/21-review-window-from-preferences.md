# 21: Review window and want-family sort come from preferences

**Branch:** `uq/21-review-window-from-preferences` · **Depends on:** 09, 15 · **Blocks:** none

## Why

Phase 2 shipped `/review` with a constant 14-day window and no role preference in its
sort. With preferences (chunk 15), both become personal settings: someone who checks
monthly wants 30 days, and a `want` family should sort ahead of a `neutral` one.

## Read first

- `jobtracker/queue.py` (`REVIEW_WINDOW_DAYS`, `sort_key`), `render_review` (chunk 09)
- `jobtracker/preferences.py` (chunk 15)

## Do

1. Preferences gain `review_window_days: int` (default 14, valid 1–365). It is validated
   by the strict loader.
2. `queue.sort_key` takes the merged `Criteria` and orders by:
   1. lane;
   2. whether the title hits a `want` family's token (yes first);
   3. location rank;
   4. `-first_seen`.
   `queue.py` stays pure; pass the values in.
3. `render_review` and Today's count read the window from preferences.
4. Add `review_window_days` to `preferences.example.yaml` with a comment.

## Tests

- `test_review_window_from_preferences`
- `test_want_family_sorts_first_within_lane`
- `test_queue_still_pure`

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
