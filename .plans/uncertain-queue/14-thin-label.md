# 14: Thin rows: label and cap

**Branch:** `uq/14-thin-label` · **Depends on:** 02 · **Blocks:** none · **Merge after 09 and 11** (dashboard)

## Why

A row whose description is still feed metadata (`description_source='feed'`) is judged on
almost nothing, yet it can score "fit strong" and outrank a row with real prose. The user
chose to label such rows **thin** and cap their score, in Python, never through the
model.

## Read first

- `jobtracker/rank.py`: `score_posting` (~line 122), `ORDINAL_VALUE`, `todays_picks`
- `jobtracker/cli.py::cmd_prepare` (~line 1826), and the CLAUDE.md "third outcome" rule
  for `prepare`
- `jobtracker/dashboard.py`: `_pick`, `_table`, `_board_table` (chip patterns such as
  `srctag` and "on company page")
- `CLAUDE.md` § *The ranking pass*: the model never sees a score; ordinals are applied in
  Python; an unjudged posting scores `None`.

## Do

1. `score_posting` takes `thin: bool`. When it is true, `fit` and `growth` are capped at
   `ORDINAL_VALUE["moderate"]` before weighting.
   - `None` stays `None`. An unjudged row is not made thin-scored.
   - `ranked_matches` selects `description_source` so the caller can pass the flag.
2. Render a `thin` chip (`span.thin`, `title="judged from the feed's listing fields, not
   the posting's text"`) beside the role on picks, the drawer, the matches tables and
   `/review`, if chunk 09 is merged. Add its CSS to `dashboard._CSS` with resolved tokens
   only.
3. `prepare` lists thin picks by name in its output and does **not** count them against
   `ready`, so it never exits 2 because of them.
4. Add one bullet to `CLAUDE.md` § *The ranking pass*: "a thin row's fit and growth are
   capped at moderate in Python; the model is never told", and make the matching change
   in `docs/ranking.md`.

## Don't

- Don't change the judge prompt or weights in `profile.yaml`.

## Tests

- `test_thin_row_score_is_capped`
- `test_unjudged_thin_row_still_scores_none`
- `test_thin_chip_renders_on_picks_and_tables`
- `test_prepare_does_not_exit_2_for_thin_picks`

## Done when

Tests and lint pass, and the CSS-variable test passes.

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
