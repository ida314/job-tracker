# 18: The Settings card, which previews before it applies

**Branch:** `uq/18-preferences-settings-card` · **Depends on:** 15, 17 · **Blocks:** none

## Why

A stance flip can move hundreds of verdicts at once; hardware alone is about 194 live
Simplify rows. Adding a token on `/tuning` writes, then rematches, then reports. The user
chose instead that a family stance change **shows a dry-run diff first**, and Apply does
the write.

## Read first

- `jobtracker/server.py`:
  - `render_settings` (~line 316) and the existing cards (plugins, downloads);
  - `_api_plugin` (~line 3819): the request carries the desired state, not "toggle";
  - the downloads card's one-Save-per-field pattern;
  - `/api/rematch`.
- `jobtracker/safewrite.py`, `jobtracker/keywords.py`'s line-oriented `edit` (text surgery
  instead of `yaml.safe_dump`, which deletes comments)
- `jobtracker/tuning.py::replay` (chunk 04), `jobtracker/preferences.py` (chunk 15),
  `preferences.guard` (chunk 17)
- `CLAUDE.md`:
  - § *The HTML dashboard*: handlers live in the file that renders the button; delegated;
    no `data-act`; the script must parse;
  - § *Where saved documents go*: one Save per field; a malformed file refuses the save;
    the card shows what the file says, never a fallback;
  - § *Adding a company*: the list of files `serve` may write.

## Do

1. **Settings card, "What you're looking for".**
   - One row per catalogue family: its name, its tokens (custom ones marked), and a
     three-way radio group (`want` / `neutral` / `avoid`), checked from **the file's
     values**. An absent family shows neutral checked with "(not set)".
   - Below the families, an "Add a token to <family>" box per family. There is no delete
     control, the same as `/tuning`; to remove a token, edit the file.
2. **`POST /api/preferences/preview`** `{family, stance}`, a **pure read**.
   - It merges the proposed stance into the current preferences and replays `match()` over
     **open** postings with overrides applied.
   - It returns the delta per transition, up to 10 sample titles each, and the count of
     rows "held by your rulings".
   - The card renders that inline under the row, with an Apply button.
3. **`POST /api/preferences`** `{family, stance}` or `{family, add_token}`.
   - The request carries the desired state.
   - It writes `preferences.yaml` through `safewrite.py` as line-oriented text surgery,
     with comments surviving.
   - A missing file is created from `preferences.example.yaml`'s comments with no values.
   - A malformed file refuses; it does not overwrite.
   - Then it rematches and returns the real delta, which the card shows.
   - The guard (chunk 17) runs first.
4. Every control is server-rendered. Handlers are delegated in the Settings page's script.
   No `data-act`.
5. In `CLAUDE.md` § *Adding a company*, add `preferences.yaml` to the files `serve` may
   write on a click. In § *Preferences*, add: preview before apply; the preview is a pure
   read; one apply per family; no delete control.

## Don't

- Don't add a "save all" button, and don't render a fallback value into a control.

## Tests

- `test_preview_is_a_pure_read`: no write to `verdicts`, `preferences.yaml` or
  `overrides`.
- `test_apply_carries_desired_state`: a double-POST lands as one change.
- `test_preferences_write_preserves_comments`
- `test_malformed_preferences_refuses_save_and_page_still_renders`
- `test_settings_family_controls_have_handlers`, the parity test.
- `test_add_token_has_no_delete_counterpart`

## Done when

Tests and lint pass. In a preview with private copies (the run-job-tracker skill), flip
`hardware` to neutral: the preview shows a delta and Apply writes it. Report the delta you
saw.

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
