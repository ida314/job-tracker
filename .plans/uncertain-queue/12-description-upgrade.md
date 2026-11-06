# 12: Upgrade feed metadata to real prose

**Branch:** `uq/12-description-upgrade` · **Depends on:** 02 · **Blocks:** 14 (soft)

## Why

766 live Simplify matches, and any row the user rules "worth a look", carry
metadata-only descriptions. `judge`, `tailor` and `coverletter` then work from almost
nothing. Many Simplify links point straight at a Greenhouse, Lever or Ashby posting whose
public JSON has the real prose. This chunk lets `check` fetch it once and upgrade the
description.

## Read first

- `jobtracker/cli.py::_cache_descriptions` (~line 223) and `cmd_check`'s `wanted` list
  (~689–745). **Plugin postings are kept out of `wanted` on purpose**: its
  no-detail-endpoint branch would erase the text.
- `jobtracker/dedupe.py` (the ATS path extractors used for keys), `jobtracker/sources/`
  (Greenhouse, Lever and Ashby adapters: detail fetch, `descriptionPlain`),
  `jobtracker/fetch.py::Fetcher._request_json` (the per-host limiter)
- `CLAUDE.md`:
  - § *Descriptions are cached by `check`*: write-once, NULL vs `''`, the budget, and that
    a failure is invisible to health;
  - § *Rule: slugs are verified, never guessed*: Lever slugs are case-sensitive, and a
    link a page served is provenance;
  - § *URL dedupe*: the slug inside an ATS key is **lowercased**.

## Do

1. Add a function `upgrade_target(url) -> (ats, slug, job_id) | None`, reading **the URL
   with its case preserved**. Reuse `dedupe.py`'s path patterns, but never parse
   `dedupe_key`, which lowercases the slug and breaks Lever.
   - Supported: Greenhouse (`job-boards.greenhouse.io/X/jobs/N`, `boards.greenhouse.io/X/jobs/N`,
     and `embed/job_app?for=X&token=N`), Lever (`jobs.lever.co/X/<uuid>`) and Ashby
     (`jobs.ashbyhq.com/X/<uuid>`).
   - Anything else returns `None`.
2. `cmd_check` builds a separate list, `wanted_upgrade`: open feed rows
   (`description_source='feed'`) whose verdict is `match` or `uncertain` and whose URL
   resolves.
   - It has its own budget, `--max-upgrades` (default 100), and runs after the board
     pass.
   - Each fetch goes through the existing adapter and `Fetcher` (same limiter).
   - Ashby: fetch the board once per slug per run, then pick the job.
3. **The one permitted upgrade.** If the fetch returns non-empty prose, call
   `set_description(..., source="ats")`, overwriting the feed text. This is the **only**
   case where write-once is relaxed, and only from `feed` to `ats`. A failure, a 404 or
   empty prose leaves the row untouched and counts in a summary log line, like the
   description pass. It never affects health and never produces `EXIT_DEGRADED`.
4. Label the provenance in the log as `provenance` (the feed served the link) and never
   as verified identity.
5. Update `CLAUDE.md` § *Descriptions are cached by `check`*:
   - the `feed` → `ats` upgrade is the single exception to write-once;
   - it is resolved from the URL, never the key;
   - it has its own budget.
   - Make the matching change in `docs/plugins.md`.

## Don't

- Don't add rows to `wanted`.
- Don't fetch anything for a URL that does not resolve.
- Don't scrape HTML. Public JSON only, through the existing adapters.

## Tests (the fetcher faked; no network)

- `test_upgrade_resolves_lever_slug_from_url_case_preserved`
- `test_upgrade_never_enters_wanted`
- `test_upgrade_overwrites_only_feed_text`: an `ats` row is never re-fetched.
- `test_upgrade_failure_is_invisible_to_health`
- `test_upgrade_respects_its_budget`
- `test_ashby_board_fetched_once_per_slug`

## Done when

Tests and lint pass. A dry count over a `$TMPDIR` copy of the laptop DB (no fetching)
reports how many Simplify URLs resolve per ATS; put it in your report.

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
