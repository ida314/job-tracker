# 05: Title-token fixes

**Branch:** `uq/05-title-tokens` · **Depends on:** 04 · **Blocks:** 06, 15, 22

## Why

These gaps were found by replaying live titles on 2026-11-05.

| Gap | Cause | Size (live) |
|---|---|---|
| "Graduate Software Engineer" is uncertain | `grad` is anchored on alphanumeric boundaries, so it does not fire inside "Graduate" | many Simplify rows, 212 of them currently pinned by the model |
| "Software Engineer 1" is uncertain | only the Roman `Software Engineer I` is a token | about 150 engineering rows |
| "Full-stack Engineer 4" is uncertain | `ii`/`iii` are excluded but Arabic numerals are not | 36 rows |
| "Sr Accounting Specialist" is uncertain | `sr.` needs its dot | 46 rows |

**Hardware tokens are deliberately out of scope.** The user does not want their personal
preferences hardcoded, and hardware becomes a role family in chunk 15.

## Read first

- `criteria.yaml` (its comments explain every past token decision)
- `jobtracker/criteria.py::_compile`, `jobtracker/match.py`
- `CLAUDE.md` § *The tuning loop*. In particular, **every token also reads the employer
  name in `"Employer — Role"` titles**, and plurals walk through exclusions.
- `docs/tuning.md` and the `eval --replay-all` command from chunk 04

## Do

Each item is its own commit, and each commit body contains the `--replay-all` delta
against the previous commit, run on a `$TMPDIR` copy of the laptop DB.

1. `level_include` += `graduate`. Check what else it catches: "Commercial Graduate
   Trainee" should become a REJECT through the non-engineering gate, which is correct.
   Look for any employer name containing "graduate".
2. `level_include` += `engineer 1`, `developer 1`, `engineer i`.
   - `engineer i` must not fire on "Engineer II" or "Engineer III". `_compile`'s lookahead
     should handle this, and a test must prove it.
   - Also consider `analyst 1` (no: it has no engineering signal, so it changes nothing)
     and `sde 1` (yes, if it occurs in the corpus).
3. `exclude_titles` += Arabic seniority levels **only as they occur in the corpus**, found
   by grepping the copied DB's titles for `(engineer|developer|researcher|scientist) [2-9]`.
   Add the exact tokens (`engineer 2` … `engineer 5`, `researcher 4`, `researcher 5`, …).
   - Do not add bare digits.
   - Check that none of them fires on an employer name or on a year (`2027`).
4. `exclude_titles` += `sr`. Prove it does not fire inside `sre`, `SRE` or `Sr.`. The last
   is already covered and must still be. Check employer names that are or contain the
   whole word "Sr".
5. Write a comment beside each new token in `criteria.yaml`, in the file's existing style:
   why it exists, what was checked and the measured count.

## Don't

- Don't add hardware, frontend or any role-family token.
- Don't remove or reorder existing tokens.
- Don't touch `role_type_*` lists.

## Tests (`tests/test_match.py`)

- `test_graduate_is_a_level_token`
- `test_engineer_i_does_not_fire_on_engineer_ii`
- `test_engineer_4_is_excluded`
- `test_sr_does_not_fire_inside_sre`
- `test_numbered_tokens_do_not_fire_on_years`

## Done when

Tests and lint pass. Each commit body carries its replay delta, and no `match → reject`
transition appears without being named in the body.

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
