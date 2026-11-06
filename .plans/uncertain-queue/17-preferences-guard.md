# 17: Refuse to run when preferences went missing

**Branch:** `uq/17-preferences-guard` · **Depends on:** 15 · **Blocks:** 18

## Why

`check` re-derives every unpinned verdict nightly. If gx10's wrapper forgets to mount
`preferences.yaml`, chunk 15's neutral fallback quietly re-admits every avoided role
overnight, and `judge` spends its budget on them. The user chose this rule: **refuse when
preferences existed before; a fresh install runs neutral with a warning.**

## Read first

- `jobtracker/preferences.py` (`Preferences.hash`, neutral default), `jobtracker/cli.py`
  (`cmd_check`, `cmd_work`, `cmd_rematch`, `cmd_prepare`, exit codes 0/1/2,
  `health.is_degraded`)
- `jobtracker/server.py`: `/readyz`, the banner pattern used for a malformed
  `plugins.yaml` on Settings, and the rematch path
- `jobtracker/store.py` (`runs` table)
- `CLAUDE.md` § *Deployment* (`/readyz` semantics) and § *Operational notes* (exit codes)

## Do

1. Store `preferences_hash` in a one-row `settings_state(key TEXT PRIMARY KEY, value
   TEXT)` table, created with `CREATE TABLE IF NOT EXISTS`. It is written only after a
   **successful** `check` or `rematch`, never by `serve` merely starting.
2. Add `preferences.guard(conn, path) -> Optional[str]`, which returns a refusal message or
   `None`.

   | File | Stored hash | Result |
   |---|---|---|
   | missing | none | `None` (fresh install), WARNING |
   | missing | == neutral hash | `None` |
   | missing | non-neutral | refusal: names the path and `$JOBTRACKER_PREFERENCES`, and says `jobtracker preferences reset` |
   | malformed | any | refusal (the parse error) |
   | present | differs | `None`, with an INFO "preferences changed since last run" line |

3. `check`, `work`, `rematch` and `prepare` call the guard **before opening any write
   transaction**. On refusal they exit **1** ("could not run"), having changed nothing.
4. Under `serve`:
   - `/readyz` returns 503 with `checks.preferences` naming the reason;
   - every page renders a banner;
   - rematch endpoints (`/api/rule`, `/api/rematch`, and anything else that rewrites
     verdicts) refuse with `ok:false`.
   - Read-only pages still render, because that is where you go to find out what is wrong.
5. `jobtracker preferences reset` records the neutral hash. It is a deliberate act, and
   its message says what it means.
6. Add `docs/preferences.md` § *Missing file*, a `docs/deployment.md` note that both
   wrappers must mount it `:ro`, and a `CLAUDE.md` § *Preferences* bullet.

## Don't

- Don't auto-recover, guess a path, or read the file from somewhere else.

## Tests

- `test_missing_preferences_after_non_neutral_run_exits_1_before_any_verdict`: check row
  counts in `verdicts` before and after.
- `test_fresh_install_runs_neutral`
- `test_malformed_preferences_exits_1`
- `test_readyz_names_preferences`
- `test_reset_records_neutral_hash`
- `test_serve_rematch_refuses_under_guard`

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
