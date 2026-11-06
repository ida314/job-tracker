# 23: The model reports bands, and readings apply in the caller path

**Branch:** `uq/23-band-readings` · **Depends on:** 03, 22 · **Operational prerequisite:** the user has run `audit-pins --write` on gx10 (chunk 06) before this ships · **Blocks:** none

## Why

The model answers `entry`/`not_entry`, which is a judgement against one target, and its
answers are pinned as overrides. The user chose:

- **the model reports a posting's absolute band**, and the prompt never mentions the
  target;
- **the reading is stored** (`level_readings`, from chunk 03);
- **Python compares it with the target**, so changing `level.targets` re-derives every
  verdict with zero model calls. This is the same contract as weights against prose in
  `profile.yaml`.

## Read first

- `jobtracker/tasks/level.py` (after 01 and 03), `store.level_readings`
- `jobtracker/cli.py::cmd_check`, `cmd_rematch`, and the serve rematch path. Grep
  `apply_override`; that is where overrides are applied today, outside `match()`.
- `jobtracker/tuning.py::apply_override`, `jobtracker/match.py` (chunk 22's bands),
  `queue.lane` (chunk 08, which takes `targets`)
- `CLAUDE.md` § *The tuning loop* (overrides outrank rules, applied in the caller path,
  `match()` purity) and § *The ambiguity pass*

## Do

1. **Prompt and schema.**
   - The enum becomes `intern|entry|mid|senior|staff|management|unclear`.
   - The prompt describes each band neutrally and says "report the level the posting
     requires; do not judge whether it suits anyone".
   - "An internship is not entry-level" becomes "report `intern`".
   - The word "software" is removed (chunk 24 adds the field name).
   - Grounding (chunk 01) is unchanged.
   - Changing the question re-keys units. Bump `unit_key` from `"level"` to `"level:v2"`
     and name the cost (every row is re-read once) in the commit body.
2. **No more pins from `level`.** `apply()` writes the reading only. Delete the
   `set_override(..., decided_by="llm")` call.
3. **Apply readings in the caller path.** Add one function,
   `tuning.apply_reading(verdict, reading, criteria, targets) -> Verdict`, and use it
   everywhere overrides are applied, in the order **human override > reading > rules**.
   - A reading applies only when the rules verdict is `no_level_token_in_title`
     UNCERTAIN.
   - A target band → field gate → MATCH `reading:<band>`; otherwise REJECT.
   - A non-target band → REJECT `reading:<band>`.
   - `unclear` → unchanged.
   - `match()` stays pure and does not change.
4. **Migration, on connect, logging the count.**
   - Every remaining `decided_by='llm'` override becomes a reading (`entry`→`entry`,
     `not_entry`→`senior`, evidence from its reason, `source_hash='migrated'`) and is
     then cleared.
   - Human overrides are untouched.
   - This is why chunk 06's audit must have run first.
5. `queue.lane` and `/review` take `targets` from preferences, so the FEED lane fires only
   for a target band.
6. Update `CLAUDE.md` § *The ambiguity pass* and § *The tuning loop*:
   - the model reports bands and never targets;
   - readings are not overrides;
   - precedence is human > reading > rules;
   - changing the target costs zero model calls.
   - Update `docs/llm.md` to match.

## Don't

- Don't let any model reading displace a human override.
- Don't put the target in any prompt.

## Tests

- `test_prompt_never_names_target`: render the prompt with two different target sets; it
  is byte-identical.
- `test_target_change_needs_no_model_call`: flip `targets` and rematch; the fake router
  sees zero calls and verdicts move.
- `test_reading_never_displaces_human_override`
- `test_llm_overrides_migrate_to_readings`
- `test_level_no_longer_writes_overrides`: a source scan for `set_override` in
  `tasks/level.py`.

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
