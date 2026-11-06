# 15: Preferences core: a family catalogue plus a personal overlay

**Branch:** `uq/15-preferences-core` · **Depends on:** 04, 05 · **Blocks:** 16, 17, 18, 21, 22, 24

## Why

The user wants the tracker usable by people with different engineering preferences, with
**nothing personal hardcoded**. Today `criteria.yaml` is tracked and baked into the image,
and its `role_type_include` / `role_type_exclude` lists are the maintainer's own taste:
backend wanted; frontend, mobile, ML, hardware, sales and design avoided.

This chunk splits it:

- **`criteria.yaml`** (tracked) holds mechanics and a **catalogue of named role
  families**;
- **`preferences.yaml`** (gitignored) holds each person's **stance** per family: `want`,
  `neutral` or `avoid`.

**Hard requirement: with the maintainer's preferences, every verdict is unchanged.**

## Read first

- `criteria.yaml` (all comments; they record why each token exists)
- `jobtracker/criteria.py` (`_LIST_KEYS`, `Criteria`, `load_criteria`), `jobtracker/match.py`
- every `load_criteria(` caller (14 call sites: `grep -rn "load_criteria(" jobtracker`)
- `jobtracker/server.py` lines ~150–230: the `/tuning` rules section, `_SUGGEST_TARGETS`,
  `/api/rule`
- `jobtracker/tasks/level.py::looks_engineering`, `verdict_from_level`, which read
  `role_type_include`
- `.gitignore` (see how `answers.yaml` and `plugins.yaml` are handled), `Dockerfile`,
  `Dockerfile.serve`, `config.py` (`CRITERIA_YAML`, `PROFILE_YAML` env pattern)
- `CLAUDE.md` § *The tuning loop*, § *Adding a company* (the list of files `serve` may
  write), § *Job boards* (`plugins.yaml`: strict load, a malformed file stops the feed)
- chunk 04's `eval --replay-all`

## Do

1. **`criteria.yaml`**:
   - Replace `role_type_include` and `role_type_exclude` with
     `families: {<name>: {tokens: [...], note: "..."}}`.
   - Every existing token goes into exactly one family. Suggested names: `backend`,
     `full-stack`, `infrastructure`, `data-engineering`, `sre`, `frontend`, `mobile`,
     `ml-research`, `design`, `product`, `sales-engineering`, `go-to-market`, `support`,
     `operations`, `hardware`. Choose sensibly and keep the existing comments with their
     tokens.
   - Token order inside a family is preserved, because `first_hit` returns the first.
   - Move `keywords_bonus` to preferences. It is personal and unused by code today (check
     with `grep`).
2. **`jobtracker/preferences.py`**:
   - `load_preferences(path) -> Preferences`, strict: unknown keys, unknown family names
     or a bad stance raise `ValueError` naming the path.
   - A missing file returns a neutral `Preferences` and logs a WARNING naming the path and
     `$JOBTRACKER_PREFERENCES`. Chunk 17 adds the refusal on top.
   - The schema:
     ```yaml
     families: {backend: want, frontend: avoid, ...}   # absent = neutral
     custom: {backend: [extra token, ...]}              # appended to that family's tokens
     keywords_bonus: [...]
     ```
   - Expose `Preferences.hash` (sha256 of a canonical JSON) for chunk 17.
3. **Merge in `load_criteria(criteria_path, preferences_path=None)`**:
   - It returns the **same `Criteria` dataclass**, so `match()` does not change.
   - `want` families' tokens → `role_type_include`, in catalogue order.
   - `avoid` families' tokens → `role_type_exclude`.
   - `neutral` contributes nothing.
   - The REJECT reason names the family, `excluded_role:<family>(<token>)`. Update every
     reader of that reason format.
   - Add `config.PREFERENCES_YAML` (env `JOBTRACKER_PREFERENCES`, default
     `ROOT/preferences.yaml`) and thread it to every caller.
4. **Files**:
   - Add `preferences.yaml` to `.gitignore`.
   - Track a **neutral** `preferences.example.yaml`, heavily commented the way
     `answers.example.yaml` is, with no personal values.
   - Don't copy any preferences file into the images. They are mounted at runtime.
5. **The maintainer's file**:
   - Write `preferences.yaml` in your worktree, untracked, with the stances that reproduce
     the current lists. **Paste its full contents in your final report**, so the user can
     put it in the main checkout and on gx10.
   - Put the same stances in `tests/fixtures/preferences_equivalence.yaml`, with a header
     saying it exists only to prove the refactor is behaviour-neutral.
6. **`/tuning`**:
   - The `role_type_*` rows render the **merged** lists read-only, with a line saying
     stances are set on Settings.
   - `_SUGGEST_TARGETS` becomes `("exclude_titles",)`. Adding tokens to a family is chunk
     18's.
   - `/api/rule` refuses `role_type_*` with a message pointing at Settings.
7. **The zero-diff proof**:
   - Add `test_maintainer_preferences_replay_zero_diff`: over a fixture corpus, the old
     inline lists and the catalogue with the equivalence preferences must give identical
     verdicts. Reasons may differ only in the new family-naming format.
   - Run `eval --replay-all` on a `$TMPDIR` copy of the laptop DB (`--against` the pre-15
     `criteria.yaml`, `--criteria` the new one, with `JOBTRACKER_PREFERENCES` set to your
     maintainer file). It must show zero verdict transitions. Put the output in the commit
     body and your report.
8. **Docs**:
   - `CLAUDE.md`: add a § *Preferences* covering the overlay, strict loading,
     `avoid`-wins, the zero-diff gate for any preferences refactor, and that
     `preferences.yaml` is a file `serve` may write (chunk 18).
   - Update § *Goals* to say these are the maintainer's preferences, living in
     `preferences.yaml` / `profile.yaml`, not in code.
   - Add `docs/preferences.md` (new).

## Don't

- Don't change the level gate, `engineering_terms`, `exclude_titles` or locations. Those
  are chunks 16, 22 and 24.
- Don't build the Settings card (18) or the missing-file refusal (17).

## Tests

- `test_maintainer_preferences_replay_zero_diff`
- `test_avoid_beats_want`
- `test_neutral_family_does_not_satisfy_engineering_gate`
- `test_unknown_family_in_preferences_is_an_error`
- `test_missing_preferences_is_neutral_with_warning`
- `test_every_catalogue_token_is_in_exactly_one_family`
- `test_tuning_refuses_role_type_rule_writes`

## Done when

Tests and lint pass, the replay output shows zero transitions, and the maintainer file's
contents are in your report.

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
