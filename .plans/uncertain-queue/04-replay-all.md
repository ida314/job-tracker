# 04: `jobtracker eval --replay-all`

**Branch:** `uq/04-replay-all` · **Depends on:** none · **Blocks:** 05, 15, 22, 24

## Why

`CLAUDE.md` § *The tuning loop*: "When `decisions` is empty and `eval` has no corpus,
replay `match()` over all stored postings under old and new criteria and diff every
verdict instead." No command does that, and live `decisions` is empty. Every criteria
change in this plan needs that diff as evidence, so it should be a tool rather than
something each agent improvises.

## Read first

- `jobtracker/tuning.py` (`evaluate`, `apply_override`), `jobtracker/cli.py::cmd_eval`
  (~line 1384), the `eval` parser (~line 2916) and `cmd_rematch` (~line 1012)
- `jobtracker/match.py`, `jobtracker/criteria.py::load_criteria`
- `docs/tuning.md`

## Do

1. Add `tuning.replay(postings, old: Criteria, new: Criteria, overrides) -> ReplayReport`.
   - It is pure: it takes rows, not a connection.
   - For each posting it computes the old and new rules verdicts and the effective
     verdict after overrides. A pinned row is reported as "held" when the rules moved but
     the override didn't.
   - It returns counts per `(old → new)` transition and up to N sample
     `(company, title, old_reason, new_reason)` per transition.
2. Add a `--replay-all` mode to `cmd_eval` with these flags:
   - `--against PATH`: the old criteria file; defaults to `git show HEAD:criteria.yaml`
     written to a temp file;
   - `--samples N` (default 20);
   - `--intended PATH`: optional YAML listing intended transitions, e.g.
     `{"uncertain->match": "graduate token", "match->reject": "…"}`;
   - `--db` and `--criteria` as for `eval`.
   - Postings are read **read-only**: open the DB URI with `mode=ro` and do not go through
     `store.connect()`, which migrates. It covers open and closed postings, all origins.
3. Exit codes:
   - 0 when there is no `match → reject` or `match → uncertain` transition, or when every
     such transition is listed in `--intended`;
   - 1 otherwise;
   - 2 when the criteria do not load.
   - Count and print `uncertain → reject` and `uncertain → match` transitions, but do not
     gate on them. Moving an `uncertain` row is not a regression; CLAUDE.md says so.
4. Use the same output style as `rematch`, with a delta table and then samples grouped by
   transition.
5. Document it in `docs/tuning.md`. Add one line to `CLAUDE.md` § *The tuning loop* naming
   the command as the way to do the empty-corpus replay.

## Don't

- Don't write to the DB, and don't change `match()`.
- Don't make `--replay-all` require `decisions`.

## Tests

- `test_replay_is_pure`: no connection argument, and the same inputs give the same report.
- `test_replay_reports_held_rows_under_overrides`
- `test_replay_gates_on_match_loss_only`
- `test_replay_intended_file_clears_the_gate`
- `test_replay_opens_db_read_only`: point it at a DB file with no write permission; it
  must succeed.

## Done when

Tests and lint pass. Running it against a `$TMPDIR` copy of the laptop `state.db`, with
`--against` set to the current `criteria.yaml`, prints all zeros and exits 0. Include that
output in your report.

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
