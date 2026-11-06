# 09: The `/review` page

**Branch:** `uq/09-review-page` · **Depends on:** 08 · **Blocks:** 10, 21

## Why

The user wants uncertain to be a short list of possible misses that they skim weekly.
Measured live: about 134 promising rows in the last 14 days, against 3,176 in total. That
list lives on its own `serve` page. It is a **read across** the Company pages / Job boards
partition, not a third home for postings.

## Read first

- `CLAUDE.md` § *The HTML dashboard*. All of it binds. In particular:
  - rows render server-side and JS only toggles `[hidden]` / `tbody.closed`;
  - filtering owns `hidden` and collapsing owns `closed`;
  - a filter bar drives the panel that renders it via `[data-filter-scope]`;
  - every `var(--x)` must resolve;
  - `:hover` goes inside `@media (hover: hover)`;
  - escape everything, and check URL schemes;
  - the emitted script must parse (`\\n`, not `\n`).
- `jobtracker/server.py`: `_NAV` (~line 5743), how `/jobboards` is routed and rendered
  (`render_*` connection-in, string-out functions), `_JS`
- `jobtracker/dashboard.py`: `_CSS`, `_employer_of`, the Today drawer's flat-row rendering
  (`.cobadge`, `srctag`), `_posting_attrs` / `_posting_href`, `_picks`
- `jobtracker/queue.py` (chunk 08)
- The project skill
  `/home/dylan/code/projects/job-tracker/.claude/skills/run-job-tracker/SKILL.md`, to
  preview on private copies of the data. Never run `serve` against real files.
- `tests/test_css.py`, `tests/test_dashboard.py`, `tests/test_server.py` for existing
  parity tests

## Do

1. Add `render_review(conn, criteria, companies, coverage_by_origin, today) -> str`, a
   pure read. Route it at `GET /review` and add **Review** to `_NAV` after Job boards.
2. Layout:
   - Heading "Review" with the count of the default view.
   - A `[data-filter-scope]` with a search box only. No tier chips and no ATS select,
     because those filter fields many rows leave empty.
   - **One flat table.** The columns are Role (`_posting_attrs` link + `srctag` if it came
     from a feed), Employer (`.cobadge`, from `_employer_of`), Location (with pin), Lane
     label, and First seen. There is no actions column yet; chunk 10 adds it.
   - The rows are lanes FEED, READ_UNCLEAR and SIGNAL with `first_seen` within
     `REVIEW_WINDOW_DAYS = 14`. That is a module constant in `queue.py`; chunk 21 moves it
     to preferences. They sort by `queue.sort_key`.
   - Then a closed `tbody`, "N older", holding the same lanes outside the window.
   - Then a closed `tbody`, "N with no field signal", holding NO_SIGNAL rows. Inside it is
     a nested closed group, "N from boards that list many non-field roles: <company
     names>", holding BOARD_NOISE rows.
   - A data row carries `data-search`, and group heads do not.
3. **Not a `table[data-filterable]`** shared with any other panel. Give it its own scope.
4. On Today, add one line under the picks: "N to review" (lanes FEED, READ_UNCLEAR and
   SIGNAL within the window, from `queue`). Under `serve` it links to `/review`. In the
   static file it is the count only, with no link, because the static file has no
   `/review`.
5. Add a `docs/queue.md` section for the page. In `CLAUDE.md` § *The review queue*
   (created by chunk 08), add these bullets:
   - `/review` is serve-only;
   - it is a read across the partition;
   - it is never `data-filterable` beside another panel;
   - Today shows the count.

## Don't

- Don't add buttons (chunk 10).
- Don't change the existing Uncertain tables (chunk 11).
- Don't add `/review` to the static dashboard.

## Tests

- `test_review_rows_are_each_on_exactly_one_surface`: every `/review` row is in exactly
  one of the company-page and job-board renderings.
- `test_review_default_view_respects_window`, `test_review_folds_render_closed`
- `test_review_is_not_data_filterable`
- `test_review_escapes_titles_and_checks_url_schemes`
- `test_today_review_count_links_only_under_serve`
- Extend the existing CSS-variable test so every `var(--x)` on the new page resolves.
- The existing script-parses test covers any JS you emit.

## Done when

Tests and lint pass. A preview via the run-job-tracker skill shows the page at phone
width with no horizontal scroll. Attach a screenshot path from the scratchpad, using the
headless approach in the skill.

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
