# 20: `backend_fit` → `fit` on the wire

**Branch:** `uq/20-fit-rename` · **Depends on:** none · **Blocks:** 25

## Why

The judge's JSON schema field is named `backend_fit`, which hardcodes the maintainer's
target into the model contract. The target belongs in `profile.yaml` prose, which the
prompt already reads.

## Read first

- `jobtracker/tasks/judge.py` (schema ~line 34, prompt ~line 46, parsing ~line 108)
- `jobtracker/rank.py` (~line 135), `jobtracker/store.py` (`rankings` schema ~line 164,
  readers ~1767, writer ~1835)
- `jobtracker/profile.py::prose_hash`
- `CLAUDE.md` § *The ranking pass* and § *Deployment* (migrations are additive; rollback
  is by tag)

## Do

1. In the schema, prompt and parser, use `fit`: "how well the actual work matches the
   candidate's target roles". The parser still accepts `backend_fit` from a model that
   answers with the old name.
2. **The DB column `rankings.backend_fit` keeps its name.** Renaming it would break an
   older image reading a newer DB. Read it through one accessor (`rank.fit_of(row)`) and
   put a schema comment saying why.
3. A schema change is a change to the question. Fold a `JUDGE_SCHEMA_VERSION = 2` into
   the judge's unit key alongside `prose_hash`, so every posting is re-judged once. Name
   that cost in the commit body.
4. Grep for every `backend_fit` in `jobtracker/` and `tests/` and route each through the
   accessor or the new name. The dashboard text "fit strong" stays.
5. Update `CLAUDE.md` § *The ranking pass* and `docs/ranking.md`.

## Don't

- Don't rename the column or change weights.

## Tests

- `test_judge_schema_names_fit`
- `test_parser_accepts_legacy_backend_fit`
- `test_schema_version_rekeys_judge_units`
- `test_rankings_column_still_backend_fit`

## Done when

Tests and lint pass, and `grep -rn backend_fit jobtracker` shows only the schema, the
accessor and the legacy-parse line.

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
