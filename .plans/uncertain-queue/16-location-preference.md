# 16: Location preference, generalised

**Branch:** `uq/16-location-preference` · **Depends on:** 15 · **Blocks:** none

## Why

Location ranks and never filters (a standing rule), but the ranking is hardcoded to the
maintainer: the key is `locations_nyc`, the rank names are `nyc/us/unknown/non-us`, and an
"NYC" pin appears in the UI. Someone in Berlin, or someone who prefers Seattle, cannot
express their preference.

## Read first

- `criteria.yaml` (location lists and their comments, especially on check order and
  ambiguous state codes like "CA")
- `jobtracker/match.py::location_rank`, `LOCATION_RANKS`, `location_label`
- `jobtracker/dashboard.py`: `data-loc`, the location dropdown (which defaults to
  "Anywhere"), and the `pin` span
- `jobtracker/rank.py` (the location weight), `jobtracker/preferences.py` (chunk 15)
- `CLAUDE.md` § *Goals* (location ranks, never disqualifies) and § *The HTML dashboard*
  (location sorts, never filters)

## Do

1. In `criteria.yaml`, replace `locations_us` and `locations_non_us` with
   `countries: {us: [...], ca: [...], uk: [...], ...}`. Group the existing non-US tokens
   by country. Remove `locations_nyc`; city preference is personal.
2. Add to preferences:
   `location: {preferred: {label: str, tokens: [...]}, home_country: <key in countries>}`.
   Absent means no preferred place and `home_country` unset, so every known-country
   location is "elsewhere".
3. `location_rank`:
   - ranks are `0 preferred · 1 home country · 2 unknown · 3 abroad`;
   - the check order is preferred → abroad (any country other than home) → home →
     unknown, which keeps the existing ambiguity argument;
   - `LOCATION_RANKS = ("preferred", "home", "unknown", "abroad")`;
   - the UI label for rank 0 is the preferred label.
4. Rendering:
   - `data-loc` values and the dropdown options become `preferred|home|unknown|abroad`;
   - option text uses the label ("NYC first", "US", …);
   - the pin renders the label.
5. Old keys (`locations_nyc`, `locations_us`, `locations_non_us`) in `criteria.yaml` load
   with a WARNING naming the replacement, for one release. Don't crash.
6. The maintainer's preferences (`preferred: {label: NYC, tokens: [New York, NYC,
   Manhattan, Brooklyn]}`, `home_country: us`) must give **identical ranks** over the
   corpus.
   - Add a replay test.
   - Add the `location:` block to `tests/fixtures/preferences_equivalence.yaml`, and to
     the maintainer file text in your report.
7. Update `CLAUDE.md` § *Goals* and § *The HTML dashboard*, wherever NYC is named as
   mechanism rather than as the maintainer's choice.

## Don't

- Don't make location filter anything.
- Don't change the weights.

## Tests

- `test_location_rank_zero_diff_for_maintainer_preferences`
- `test_preferred_label_renders_in_pin_and_dropdown`
- `test_non_us_home_country_ranks_us_as_abroad`
- `test_old_location_keys_load_with_warning`
- `test_ambiguous_state_code_still_loses_to_country_name`

## Done when

Tests and lint pass, and the replay of ranks shows zero differences.

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
