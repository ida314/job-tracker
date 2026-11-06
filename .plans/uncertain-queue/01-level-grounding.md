# 01: Grounded level readings

**Branch:** `uq/01-level-grounding` · **Depends on:** none · **Blocks:** 03, 06

## Why

`jobtracker/tasks/level.py` asks a local model what experience level a posting's
description requires, and pins the answer as an override (`decided_by='llm'`). Two defects
were found on live data (2026-11-05):

1. **The quoted evidence is never checked against the text.** `parse_verdict` accepts any
   string as `evidence`. A model that invents a phrase still produces a pinned MATCH or
   REJECT.
2. **An `entry` verdict stores no evidence at all.** `verdict_from_level` writes
   `llm:entry+eng:generic` (no quote), while `not_entry` writes
   `llm:not_entry:<evidence[:80]>`. 212 live `entry` pins therefore cannot be audited.

## Read first

- `jobtracker/tasks/level.py` (whole file)
- `jobtracker/resume/` grounding helpers. `tailor` already checks quotes
  whitespace-normalized, so reuse its normalizer instead of writing a second one
  (search for `normal` / `ground` under `jobtracker/resume/` and `jobtracker/tasks/tailor.py`).
- `CLAUDE.md` § *The ambiguity pass* and § *The task queue*
- `tests/test_tasks.py` and `tests/test_llm.py` for existing level tests

## Do

1. Change `parse_verdict(text)` to `parse_verdict(text, shown: str)`, where `shown` is
   exactly what the model saw: `f"Job title: {title}\n\nJob description:\n{description[:MAX_DESCRIPTION_CHARS]}"`.
   - For a reading to be `usable` (`entry` or `not_entry`), its non-empty `evidence` must
     occur in `shown` after whitespace normalization, case-insensitive. Use the shared
     normalizer.
   - An ungrounded or empty-evidence `entry`/`not_entry` returns `None`, the same as
     malformed JSON. `unclear` with empty evidence is still valid.
   - Build `shown` once in `run()` and pass that same string to both `client.complete`
     and `parse_verdict`, so the prompt and the check cannot drift apart.
2. `verdict_from_level` stores the evidence on `entry` too:
   `f"llm:entry+{detail}:{evidence[:80]}"`.
   - Check every reader of the reason string before changing its shape. Grep for
     `llm:entry`, which includes the `/tuning` "Fired" column and the dashboard.
   - Anything that parses it must keep working. Old rows without the suffix must still
     render.
3. Update `CLAUDE.md` § *The ambiguity pass* with one bullet: **the evidence must occur in
   the text the model was shown, or the reading is no answer**. Make the matching change
   in `docs/llm.md`.

## Don't

- Don't change what `pending()` returns (that is chunk 03), the schema enum, or the prompt
  wording beyond what grounding needs.
- Don't clear or rewrite any existing override (that is chunk 06).

## Tests

- `test_ungrounded_level_reading_is_no_answer`: evidence not in the text gives `None`,
  and the unit outcome is `no_answer`.
- `test_grounding_ignores_whitespace_and_case`
- `test_evidence_may_come_from_the_title`: "Graduate" quoted from the title is grounded.
- `test_entry_reason_carries_evidence`
- `test_old_entry_reason_without_evidence_still_renders`

## Done when

All tests and lint pass, and `grep -n "parse_verdict(" -r jobtracker tests` shows no
caller left on the one-argument form.

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
