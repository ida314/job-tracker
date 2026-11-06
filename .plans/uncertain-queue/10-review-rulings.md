# 10: Ruling buttons on `/review`

**Branch:** `uq/10-review-rulings` · **Depends on:** 09 · **Blocks:** none

## Why

There is no one-click way to rule on an uncertain row. That is a large part of why live
`decisions` is empty and `jobtracker eval` has no corpus to protect criteria edits with.
The user chose two buttons:

- **Worth a look** writes a `match` decision plus a pin;
- **Not for me** writes a `reject` decision plus a pin.

Both reuse the existing write path.

## Read first

- `jobtracker/server.py::_api_decision` (~line 3254). It already records the decision,
  pins `override:manual` and writes the verdict. Also read `store.record_decision`,
  `store.set_override` and `store.clear_override`.
- `CLAUDE.md` § *The HTML dashboard* (buttons and handlers live in the file that renders
  them; no `data-act` except on the three disposition buttons; delegated handlers swap in
  place and never reload) and § *The actions cell* (for the pattern)
- `render_review` (chunk 09)

## Do

1. Each default-view and "older" row on `/review` gets a final actions cell with
   `button.rv-yes` ("Worth a look") and `button.rv-no` ("Not for me"). Each carries
   `data-company` and `data-job` and **no `data-act`**. NO_SIGNAL and BOARD_NOISE rows get
   the buttons too; folding is not a judgement.
2. Add a delegated click handler **in the script `/review` emits**:
   - it POSTs `/api/decision` with `{company, ats_job_id, decision}`;
   - on `ok`, it replaces the cell with a chip, "✓ worth a look · undo" or "✓ not for me ·
     undo", where undo is `button.rv-undo`;
   - on `!ok`, it shows an alert with the error;
   - it never reloads.
3. Add `POST /api/decision/clear` with `{company, ats_job_id}`.
   - It deletes that posting's `decisions` row and calls `clear_override` **only if the
     override's `decided_by` is the human one**, then re-derives the verdict with `match()`.
   - It refuses (`ok:false`, both tables untouched) when there is no decision for the
     posting.
   - Undo restores the two buttons in place.
4. The row stays where it is after a ruling, so a misclick is fixable. It drops off the
   list at the next render, because it is no longer uncertain.
5. In `CLAUDE.md` § *The review queue*, add:
   - `.rv-yes`, `.rv-no` and `.rv-undo` carry no `data-act`;
   - they reuse `/api/decision`;
   - `/api/decision/clear` only removes a human decision;
   - a match ruling makes the row eligible for picks on the next `rank`.

## Don't

- Don't add buttons to the existing Uncertain tables or to the static file.
- Don't add a bulk "rule all like this" control.

## Tests

- `test_review_buttons_have_handlers_and_no_data_act`, the parity test: the rendered
  button classes equal the set of handlers in the page's script.
- `test_decision_clear_refuses_when_nothing_to_clear`
- `test_decision_clear_never_clears_an_llm_override`
- `test_ruled_row_leaves_review_on_next_render`
- `test_match_ruling_feeds_eval`: after a ruling, `tuning.evaluate` sees one decision.

## Done when

Tests and lint pass. In a preview with private data copies (the run-job-tracker skill),
clicking a button, then undo, then the other button leaves exactly one `decisions` row.
Report how you checked.

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
