# 24: The field gate (`engineering_terms` → `field_terms`)

**Branch:** `uq/24-field-gate` · **Depends on:** 22 (rebase onto its `match.py`) · **Blocks:** none

## Why

`engineering_terms` is the gate that keeps "Finance Associate" out of a backend tracker,
and it also defines the review fold (no-signal rows). It assumes the user is an engineer.
The user wants the tracker to serve non-engineering careers too. They chose **one flat
`field_terms` list with shipped presets**, where an empty gate means **no gate**, logged
at WARNING. That follows the `keywords.yaml` rule that empty means unrestricted, never
nothing allowed.

## Read first

- `criteria.yaml` (`engineering_terms` and its comment), `jobtracker/match.py`,
  `jobtracker/criteria.py`, `jobtracker/preferences.py`
- `jobtracker/tasks/level.py::looks_engineering` and its docstring (the 674-of-1,537
  scoping argument), `jobtracker/queue.py`
- the `level` and `judge` prompts (`tasks/level.py::SYSTEM_PROMPT`,
  `tasks/judge.py::RANK_SYSTEM_PROMPT`)
- `CLAUDE.md` § *The ambiguity pass* ("the queue is scoped to titles with an engineering
  signal…"), and the `keywords.yaml` "empty means unrestricted" rule under § *Tailoring a
  resume*

## Do

1. In `criteria.yaml`, rename `engineering_terms` to
   `field_presets: {engineering: [...current list...], data: [...], design: [...]}`.
   - Keep the presets small and commented, and **never** let a preset token also be in an
     `avoid`-by-default family.
   - Prove the `data` and `design` presets on the corpus: list their hits in your report.
2. Add to preferences: `field: <preset name>`, or `field_terms: [...]` to list them
   directly. Both present is an error. Absent means `engineering`.
3. Rename `looks_engineering` → `has_field_signal(title, criteria)`. It is the one
   function `level.pending()`, `queue.lane` and `match()`'s gate all call. Search for any
   other copy and remove it.
4. **An empty gate means no gate.** `match()` treats every title as passing it,
   `queue.lane` never yields NO_SIGNAL or BOARD_NOISE, and every `check`/`work`/`serve`
   start logs a WARNING saying the field gate is off.
5. Prompts: `level` and `judge` speak of "this job", and take the field name from
   preferences ("a {field} job", omitted when there is no gate).
   - A prompt change re-keys units. Version them as chunks 20 and 23 did, and name the
     cost.
6. Reason strings: `non_engineering_role:…` becomes `non_field_role:…`.
   - Update every reader.
   - Old stored reasons still render.
   - `tuning.evaluate` treats the two as the same rule.
7. **Zero-diff gate:** with `field: engineering`, the replay against the pre-24 head
   shows zero verdict transitions.
8. Update `CLAUDE.md` § *The ambiguity pass*, § *The review queue* and § *Preferences*,
   plus `docs/llm.md` and `docs/preferences.md`.

## Don't

- Don't let the model decide the field. The model never decides that a role is
  on-target.

## Tests

- `test_field_engineering_zero_diff`
- `test_empty_field_terms_means_no_gate_and_warns`
- `test_has_field_signal_is_the_only_gate_function` (a source scan)
- `test_design_preset_matches_product_designer_new_grad`
- `test_field_and_field_terms_together_is_an_error`
- `test_old_non_engineering_reason_still_renders`

## Done when

Tests and lint pass, and the zero-diff replay output is in your report.

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
