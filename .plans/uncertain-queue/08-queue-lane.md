# 08: `queue.lane`, the one classification of an uncertain row

**Branch:** `uq/08-queue-lane` · **Depends on:** 02, 03, 07 · **Blocks:** 09, 11, 21

## Why

The live uncertain queue has 3,176 rows, of which 2,227 have no engineering word in the
title. The user chose to fold, not reject, that part. It becomes a short list of possible
misses, sorted by how promising they are.

Several surfaces need to agree on which lane a row is in: `/review`, the dashboard tables,
the Today count and the folds. So the lane is **one pure function**. Two copies are how
two pages come to disagree about what is on a list.

## Read first

- `jobtracker/match.py`, `jobtracker/tasks/level.py::looks_engineering`
- `jobtracker/plugins/base.py::Plugin.coverage` (chunk 02), the registry lookup by plugin
  name, and `postings.origin`
- `store.level_reading` (chunk 03), `Company.board_scope` (chunk 07)
- `CLAUDE.md` § *The HTML dashboard* and § *Job boards*: `.get` on plugin groups, which
  have no curation

## Do

Create `jobtracker/queue.py`, pure: no I/O, no SQLite, no clock.

```python
class Lane(str, Enum):
    FEED = "feed"            # feed declares a band we target, and the title has a field signal
    READ_UNCLEAR = "read"    # level read real prose; it does not state a level
    SIGNAL = "signal"        # field signal, not read yet
    NO_SIGNAL = "no_signal"  # folded
    BOARD_NOISE = "noise"    # NO_SIGNAL on a board_scope: mixed company; folded inside the fold

def lane(row, criteria, companies_by_name, coverage_by_origin, reading, targets=("entry",)) -> Lane
def label(lane: Lane, coverage_band: str | None = None) -> str
def sort_key(row, lane, criteria) -> tuple   # (lane order, location_rank, -first_seen)
```

1. The order of checks: field signal first, using `looks_engineering` and nothing else.
   With no signal, the lane is `BOARD_NOISE` if the company has `board_scope == "mixed"`,
   else `NO_SIGNAL`. With a signal, the lane is `FEED` if the row's origin declares a band
   in `targets`, `READ_UNCLEAR` if `reading.band == "unclear"`, and `SIGNAL` otherwise.
2. Labels:
   - `FEED` → "listed on a new-grad feed". Derive the wording from the band name; do not
     hardcode "new-grad" in more than one table.
   - `READ_UNCLEAR` → "read — the description does not say"
   - `SIGNAL` → "not read yet"
   - `NO_SIGNAL` → "no field signal in the title"
   - `BOARD_NOISE` → "board lists many non-field roles"
3. `targets` is a parameter with a phase-2 default of `("entry",)`. Chunk 21 and chunk 23
   feed it from preferences. Do not read config from inside `queue.py`.
4. Add a store helper, `store.uncertain_with_context(conn)`: one query returning open
   uncertain rows joined to their `level_readings` row, if any, with `origin`, `employer`
   and `first_seen`. Callers build the lanes from it.
5. Add a `docs/queue.md` page (new, short). Add a `CLAUDE.md` section, **"The review
   queue"**:
   - `queue.lane` is the single derivation;
   - a lane is derived when a page renders and is never stored;
   - folding is display, never a verdict;
   - `board_scope` is read here only.

## Don't

- Don't render anything. Chunks 09 and 11 are the consumers.
- Don't change any verdict.

## Tests

- One test per lane, plus precedence: a feed row with an unclear reading is `FEED`.
- `test_lane_is_pure`: a source scan for `sqlite3`, `open(` or `datetime.now` in
  `queue.py`.
- `test_lane_is_the_only_derivation`: lane label strings appear nowhere outside
  `queue.py`. Grep the package.
- `test_plugin_group_with_no_curation_does_not_crash`, using `.get`.
- `test_feed_lane_requires_target_band`: with `targets=("mid",)`, a Simplify row is
  `SIGNAL`.

## Done when

Tests and lint pass. A short script over a `$TMPDIR` copy of the laptop DB prints lane
counts; put them in your report.

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
