# 02: Record where a description came from

**Branch:** `uq/02-description-source` · **Depends on:** none · **Blocks:** 03, 06, 08, 12, 14

## Why

`postings.description` holds two different kinds of text:

- **prose**, from an ATS detail payload, a Discord message or YC's listing;
- **feed metadata**, which Simplify assembles from its JSON fields in
  `plugins/simplify.py::_description`: `Category: … / Locations: … / Sponsorship: … /
  Degrees: …`.

Nothing records the difference. So the `level` model was asked to read metadata, and it
pinned 212 live matches from it. Later chunks need to tell the two apart.

## Read first

- `jobtracker/store.py`: `_SCHEMA`, `_apply_column_migrations` (line ~678),
  `_ADDED_INDEXES`, `sync_postings`, `append_postings`, `set_description`
- `jobtracker/plugins/base.py` (the `Plugin` class), `plugins/simplify.py`,
  `plugins/ycombinator.py`, `plugins/discord/`
- `jobtracker/cli.py::_cache_descriptions` and the `wanted` list in `cmd_check`
  (~line 689–745)
- `CLAUDE.md` § *Descriptions are cached by `check`*, § *Job boards (import plugins)*, and
  § *URL dedupe* (the `_SCHEMA` / `_ADDED_INDEXES` trap)

## Do

1. **Add the column** `postings.description_source TEXT`, with values `ats`, `prose` or
   `feed`, through `_apply_column_migrations`. Never put it in `_SCHEMA`, for the same
   reason the dedupe index cannot live there.
2. **Backfill once on connect, logging the count**, as `store.py`'s logger does for other
   migrations:
   - rows with `origin IS NULL` and a non-NULL description → `ats`;
   - plugin rows → whatever their plugin's `coverage()["prose"]` says (`prose` if True,
     `feed` if False);
   - rows with a NULL description stay NULL.
   - Do the backfill in Python after connect, using a mapping passed in or read from the
     registry. `store` must not import `plugins`, so check the import direction rules in
     CLAUDE.md and pass the mapping in from the caller if needed.
3. **Add `Plugin.coverage(self) -> dict`**, defaulting to `{"bands": [], "prose": True}`:
   - Simplify returns `{"bands": ["entry"], "prose": False}`;
   - YC returns `{"bands": [], "prose": True}`. Its `minExperience` text is prose;
   - Discord uses the default;
   - task-kind plugins do not need it, and `coverage()` must not be called on them.
4. **Write the column at insert.**
   - `append_postings` sets it from the plugin's coverage.
   - `_cache_descriptions` (the board path) sets `ats` alongside `set_description`.
   - `set_description` takes a `source` argument, required with no default, so a future
     caller cannot forget it.
5. Update `CLAUDE.md` § *Descriptions are cached by `check`* and `docs/plugins.md` to say
   what the column means and that `coverage()` exists.

## Don't

- Don't change what any task reads (chunks 03, 12 and 14 do that).
- Don't break write-once. This chunk adds no upgrade path; that is chunk 12.

## Tests

- `test_description_source_migrates_an_existing_db`: build a pre-migration DB by hand,
  connect, and check the column exists and the backfill ran. This catches the
  `_SCHEMA`-before-migration trap.
- `test_simplify_rows_are_feed`, `test_discord_and_yc_rows_are_prose`,
  `test_board_rows_are_ats`
- `test_set_description_requires_a_source`
- `test_store_does_not_import_plugins` (if one does not exist already)

## Done when

Tests and lint pass. A copy of the laptop `state.db` connects cleanly, and the log line
reports the backfill count.

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
