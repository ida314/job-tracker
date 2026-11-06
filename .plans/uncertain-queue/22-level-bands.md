# 22: Level bands in the rules

**Branch:** `uq/22-level-bands` · **Depends on:** 05, 15 · **Blocks:** 23, 24, 25

## Why

Experience level is hardcoded to new grad:

- `level_include` is the new-grad signal;
- `exclude_titles` mixes seniority (`senior`, `staff`, `ii`, `iii`, `lead`, `manager`)
  with shape (`contractor`, `N+ years`) and a personal constraint (`clearance`, a
  citizenship note).

A mid-level engineer cannot use the tracker. The user chose **absolute bands**: rules
classify a title's band, and preferences name the target bands.

## Read first

- `criteria.yaml`: `level_include` and `exclude_titles` with every comment, including the
  plural `internships` note and chunk 05's numbered tokens
- `jobtracker/match.py`, `jobtracker/criteria.py`, `jobtracker/preferences.py`
- `CLAUDE.md` § *The tuning loop* (plurals; employer names; `uncertain` is not a
  regression) and § *The ambiguity pass*, including the **known doc bug** about "Member of
  Technical Staff"
- `docs/llm.md` (the MTS correction) and `jobtracker/resolve.py`'s docstring, if
  `resolve.py` still exists

## Do

1. In `criteria.yaml`, define `bands:` in order. Each band is a token list:
   - `intern`: `intern`, `internship`, `internships`, `co-op`
   - `entry`: everything in `level_include`
   - `mid`: `ii`, `engineer 2`, … (from chunk 05)
   - `senior`: `senior`, `sr`, `sr.`, `iii`, `engineer 4`, …
   - `staff`: `staff`, `principal`
   - `management`: `manager`, `director`, `head of`, `vp`, `lead`

   Keep the existing comments with their tokens.
2. `exclude_titles` keeps only **shape** tokens that are not personal: `contractor`,
   `5+ years`, `3+ years`, and the `20xx grad` ones. Decide whether `2026 grad`/`2025 grad`
   are band tokens or shape; justify it in a comment.
   - `clearance` moves to preferences as a **constraint**: `constraints: [clearance]`,
     where each listed item is a token list that REJECTs. A person with a clearance
     leaves it out.
3. Add `level: {targets: [entry]}` to preferences. It is a list, so "entry or mid" can be
   expressed. Absent means `[entry]`, which is the current behaviour; say so in the
   example file.
4. New `match()` order. It stays pure and returns the same `Verdict` type:
   1. a constraint token → REJECT `constraint:<token>`;
   2. `exclude_titles` (shape) → REJECT;
   3. an avoided family → REJECT;
   4. band tokens:
      - tokens from a target band **and** a non-target band → UNCERTAIN `band_conflict`;
      - target band only → field gate as today → MATCH, or REJECT
        `non_engineering_role:band=<b>`;
      - non-target bands only → REJECT `band:<b>`;
   5. no band token → UNCERTAIN `no_level_token_in_title`.
5. **The zero-diff gate.** With `targets: [entry]` plus the maintainer's constraint list,
   `eval --replay-all` against the pre-22 head must show no transitions **except**
   `band_conflict` rows ("Senior … New Grad Program" was REJECT, now UNCERTAIN).
   - List every one of those in an `--intended` file.
   - Put the replay output in the commit body and your report.
   - Update `tests/fixtures/preferences_equivalence.yaml` and the maintainer file text.
6. **Fix the known doc bug.** "Member of Technical Staff" is in the `staff` band, so it is
   REJECT for an entry target and never reaches the model. Say exactly that, and remove
   the "Known doc bug" bullet, in `CLAUDE.md` § *The ambiguity pass*, `docs/llm.md`, and
   `resolve.py`'s docstring if present.
7. Update `CLAUDE.md` § *The tuning loop* and § *Preferences*: bands, targets, constraints,
   `band_conflict`. Update `docs/tuning.md` and `docs/preferences.md`.

## Don't

- Don't touch the `level` task or prompts (chunk 23) or `engineering_terms` (chunk 24).

## Tests

- `test_band_target_entry_reproduces_pre_band_verdicts` (fixture corpus)
- `test_mid_target_matches_engineer_2`
- `test_band_conflict_is_uncertain`
- `test_constraint_rejects_only_when_listed`
- `test_plural_internships_still_in_intern_band`
- `test_mts_rejected_by_staff_band_for_entry_target`

## Done when

Tests and lint pass, and the replay shows only the intended `band_conflict` moves.

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
