# 06: `jobtracker audit-pins`

**Branch:** `uq/06-audit-pins` · **Depends on:** 01, 02, 05 · **Blocks:** 23 (operationally)

## Why

The live database holds `decided_by='llm'` overrides made before readings were grounded
(chunk 01). 212 of them are `entry` pins on Simplify rows whose "description" is metadata
only. Many are `not_entry` rejects, which are invisible because the dashboard never
renders a reject. This one-off, by-hand command removes the pins that cannot be justified.
It never touches a human ruling.

## Read first

- `jobtracker/store.py::set_override`, `clear_override`, the `overrides` schema
  (`decided_by`, `reason`)
- `jobtracker/tasks/level.py` (the reason formats from chunk 01: `llm:entry+…:<evidence>`
  and `llm:not_entry:<evidence>`; pre-01 `entry` pins have **no** evidence)
- `jobtracker/cli.py::cmd_rematch`, `cmd_eval` (output style)
- `postings.description_source` (chunk 02)
- `CLAUDE.md` § *The tuning loop*: overrides outrank rules, and `clear_override` is used
  only where a model guess contradicts the rules

## Do

1. Add a new module, `jobtracker/audit.py`, with a pure function
   `classify_pins(rows, criteria) -> list[PinFinding]`. Each row joins an `llm` override
   to its posting.

   | Pin | Keep if | Otherwise |
   |---|---|---|
   | `entry`, `description_source='feed'` | `match(title)` under the current criteria (with chunk 05's tokens) is MATCH | `clear` |
   | `not_entry`, any source | its stored evidence (after `llm:not_entry:`) occurs, normalized, in the title or description | `clear` |
   | `entry`, source `ats` or `prose`, with evidence | its evidence occurs in the title or description | `clear` |
   | `entry`, source `ats` or `prose`, no evidence (pre-01) | always kept: real prose was read | none |

2. Add a `cmd_audit_pins` command:
   - It is **dry by default**, and `--write` applies.
   - It prints counts per bucket ("kept", "cleared → will re-derive") and up to 20 sample
     titles each, `rematch`-style.
   - With `--write`, it calls `store.clear_override`, then re-derives the verdict with
     `match()` for each cleared row only, recording `decided_by='rules'`.
   - It logs the moved count at INFO. **Only `decided_by='llm'` rows are ever selected**,
     and the SQL says so.
3. It leaves `decisions`, `applications`, `rankings` and `level_readings` untouched. A
   cleared row with an application keeps it.
4. Add a `docs/tuning.md` section, plus a `CLAUDE.md` line under § *The tuning loop*:
   "`audit-pins` is the only bulk remover of model pins, and it can only see `llm` ones."

## Don't

- Don't schedule it or call it from `check`, `work` or `prepare`.
- Don't re-ask the model.

## Tests

- `test_audit_pins_never_touches_a_human_override`
- `test_audit_is_dry_by_default`
- `test_feed_entry_pin_kept_only_if_title_matches`
- `test_not_entry_pin_without_grounded_evidence_is_cleared`
- `test_cleared_row_rederives_and_keeps_its_application`

## Done when

Tests and lint pass. A dry run on a `$TMPDIR` copy of the laptop DB runs. It has no `llm`
pins, so it reports zeros, and that is expected. Include the output.

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
