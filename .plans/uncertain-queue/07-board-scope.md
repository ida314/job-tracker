# 07: `board_scope: mixed` on a company

**Branch:** `uq/07-board-scope` · **Depends on:** none · **Blocks:** 08

## Why

Target Tech's Workday slug `target/wd5/targetcareers` is all of Target's careers, so it
includes Starbucks Barista, stocking and store security. It contributes 511 of the 1,773
live company-page uncertain rows. Fetch-side filtering is forbidden (`appliedFacets` stays
empty), and a global token per retail job family would grow without end. The user chose a
curated per-company flag. It means only "this board serves many roles outside the field",
and **only the review queue reads it** (chunk 08).

## Read first

- `jobtracker/curation.py` (`FIELD_ORDER`, `validate_new`, `insert_entry`, `_edit_entry`)
- `jobtracker/config.py::load_companies`, `jobtracker/models.py::Company`
- `/companies` under `serve`: search `server.py` for `/api/company` and the companies form
- `CLAUDE.md` § *Adding a company* and § *Field schema* (field order is load-bearing)
- `companies.yaml`, the `Target Tech` entry (~line 720)

## Do

1. Add `Company.board_scope: Optional[str] = None`. The only valid value is `mixed`.
2. Add `board_scope` to `FIELD_ORDER` directly after `category`.
3. `load_companies` loads any value, logs a WARNING for an unknown one, and keeps loading.
   The loader must keep loading whatever is on disk.
4. `validate_new` accepts absent or `mixed` and refuses anything else.
5. The `/companies` add form gets a checkbox, "Board serves many non-field roles (folds
   them in Review)", that writes `board_scope: mixed`. This is the only UI.
6. In `companies.yaml`, set `board_scope: mixed` on Target Tech, using the line-oriented
   writer's ordering by hand. Append to its `notes:` a dated sentence giving the reason
   and the 511 count.
7. Update `CLAUDE.md` § *Field schema*: add a table row for `board_scope`, stating that it
   affects review folding only, and never `match()`, health or `SUSPECT_EMPTY`. Make the
   matching change in `docs/companies.md`.

## Don't

- Don't read the field anywhere except where you are told to.
- Don't add other companies; Capital One is the user's call later.

## Tests

- `test_board_scope_loads_unknown_value_with_warning`
- `test_validate_new_refuses_unknown_board_scope`
- `test_insert_entry_places_board_scope_after_category`, and every existing line survives
  byte-for-byte (an existing test pattern).
- `test_board_scope_never_reaches_match`: a source scan showing that `match.py` and
  `health.py` do not mention it.

## Done when

Tests and lint pass, and `load_companies()` on the real `companies.yaml` returns Target
Tech with `board_scope == "mixed"`.

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
