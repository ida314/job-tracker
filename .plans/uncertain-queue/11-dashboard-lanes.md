# 11: Lanes on the existing Uncertain tables

**Branch:** `uq/11-dashboard-lanes` · **Depends on:** 08 · **Blocks:** none · **Merge after 09** (both edit `dashboard.py`)

## Why

The "Why uncertain" column shows `no_level_token_in_title` on all 3,176 live rows, so it
carries no information. The user chose to keep the existing tables as the full record,
with no-signal rows folded.

## Read first

- `jobtracker/dashboard.py`:
  - `_table` (~line 2234) and `_board_table` (~line 2429), and the "Why uncertain" header
    and cell;
  - the grouping and collapsing JS: `tbody.closed`, `data-closed`;
  - `_split_by_origin`.
- `jobtracker/queue.py` (chunk 08)
- `CLAUDE.md` § *The HTML dashboard*: filtering owns `hidden`, collapsing owns `closed`,
  and any active filter force-expands matching groups. Rows render visible and JS
  collapses them on load, never the reverse. A data row carries `data-search`.

## Do

1. Rename the "Why uncertain" column to "Status". Each cell shows `queue.label(lane)`
   rather than the raw reason. Keep the raw reason in the cell's `title` attribute.
2. In both Uncertain tables (company pages, job boards), NO_SIGNAL and BOARD_NOISE rows
   render in a trailing group, "No field signal — N rows". It is a group head like a
   company group: visible in the HTML and collapsed by JS on load. An active filter
   force-expands it like any other group.
3. The tile count "Uncertain" stays the full count. A sub-line reads "M worth a read",
   M being lanes FEED, READ_UNCLEAR and SIGNAL.
4. It works identically in the static file and under `serve`. These tables have no
   buttons, so nothing interactive-only is involved.

## Don't

- Don't remove rows or change the partition.
- Don't touch `/review`.

## Tests

- `test_uncertain_status_column_shows_lane_label`
- `test_no_signal_group_renders_open_and_carries_no_data_search_on_its_head`
- `test_filter_expands_no_signal_group`, following the existing force-expand pattern.
- `test_uncertain_tile_count_unchanged`

## Done when

Tests and lint pass, and `/` and `/jobboards` render in a preview with the new group
collapsed.

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
