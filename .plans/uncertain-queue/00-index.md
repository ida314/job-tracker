# Uncertain queue and preferences: work chunks

The spec `../uncertain-queue-and-preferences-spec.md` is split into 25 chunks. Each one is
sized for one coding agent working in one git worktree on its own branch, `uq/NN-slug`.
Each chunk file is self-contained, with context, scope, tests and the done condition, and
ends with the shared ground rules from `_ground-rules.md`.

## Before dispatching anything

1. **Base branch.** Branch from `main` once `ui-polish` is merged, and until then from
   `ui-polish`. Several chunks touch `dashboard.py` and the CSS that branch rewrote.
   Run `git fetch` first, because local `main` has lagged origin before.
2. **Commit the uncommitted work.** The main checkout currently has uncommitted changes to
   `CLAUDE.md`, `jobtracker/dashboard.py`, `tests/test_css.py` and `tests/test_dashboard.py`.
   A worktree is cut from a commit, so commit or stash these first. Otherwise every chunk
   starts without them, and merges conflict.
3. **Agents read these files from the main checkout.** `.plans/` is untracked, so a
   worktree will not contain it. Every chunk refers to it by absolute path. Commit
   `.plans/` if you want it versioned.

## Chunks

| # | Chunk | Depends on | Main files |
|---|---|---|---|
| **Phase 1: ground the model** ||||
| 01 | [Grounded level readings](01-level-grounding.md) | none | `tasks/level.py` |
| 02 | [Where a description came from](02-description-source.md) | none | `store.py`, `plugins/*` |
| 03 | [Record every level reading](03-level-readings.md) | 01, 02 | `tasks/level.py`, `store.py` |
| 04 | [`eval --replay-all`](04-replay-all.md) | none | `tuning.py`, `cli.py` |
| 05 | [Title-token fixes](05-title-tokens.md) | 04 | `criteria.yaml` |
| 06 | [`audit-pins`](06-audit-pins.md) | 01, 02, 05 | `cli.py`, new `audit.py` |
| **Phase 2: the short list** ||||
| 07 | [`board_scope: mixed`](07-board-scope.md) | none | `curation.py`, `config.py`, `companies.yaml` |
| 08 | [`queue.lane`](08-queue-lane.md) | 02, 03, 07 | new `queue.py` |
| 09 | [The `/review` page](09-review-page.md) | 08 | `server.py`, `dashboard.py` |
| 10 | [Ruling buttons](10-review-rulings.md) | 09 | `server.py` |
| 11 | [Lanes on the existing tables](11-dashboard-lanes.md) | 08 | `dashboard.py` |
| 12 | [Upgrade feed text to real prose](12-description-upgrade.md) | 02 | `cli.py`, `sources/*` |
| 13 | [Redo work when a description changes](13-rejudge-on-description.md) | none (03 optional) | `store.py`, `tasks/*` |
| 14 | [Thin rows: label and cap](14-thin-label.md) | 02 | `rank.py`, `dashboard.py`, `cli.py` |
| **Phase 3: preferences** ||||
| 15 | [Preferences core](15-preferences-core.md) | 04, 05 | `criteria.py`, `criteria.yaml`, new `preferences.py` |
| 16 | [Location preference](16-location-preference.md) | 15 | `match.py`, `dashboard.py` |
| 17 | [Missing-preferences guard](17-preferences-guard.md) | 15 | `cli.py`, `server.py`, `store.py` |
| 18 | [Settings card with preview](18-preferences-settings-card.md) | 15, 17 | `server.py` |
| 19 | [`profile.yaml` becomes personal](19-profile-overlay.md) | none | `Dockerfile*`, `.gitignore` |
| 20 | [`backend_fit` → `fit` on the wire](20-fit-rename.md) | none | `tasks/judge.py`, `rank.py` |
| 21 | [Review window and want-sort from preferences](21-review-window-from-preferences.md) | 09, 15 | `queue.py` |
| **Phase 4: levels and fields** ||||
| 22 | [Level bands in rules](22-level-bands.md) | 05, 15 | `criteria.yaml`, `match.py` |
| 23 | [Model reports bands; readings applied in caller path](23-band-readings.md) | 03, 06 (run on gx10), 22 | `tasks/level.py`, `cli.py`, `server.py` |
| 24 | [The field gate](24-field-gate.md) | 22 | `criteria.py`, `match.py`, prompts |
| 25 | [`entry_risk` → `level_risk`](25-level-risk.md) | 20, 22 | `tasks/judge.py`, `rank.py` |

## Waves you can run in parallel

| Wave | Chunks | Note |
|---|---|---|
| A | 01, 02, 04, 07, 13, 19, 20 | nothing depends on anything |
| B | 03, 05, 12, 14 | |
| C | 06, 08, 15 | |
| D | 09, 11, 16, 17 | 09 and 11 both edit `dashboard.py`; merge 09 first |
| E | 10, 18, 21, 22 | |
| F | 23, 24 | 24 rebases onto 22's `match.py` |
| G | 25 | |

**Where conflicts concentrate:**

- `store.py::_apply_column_migrations`: 02, 03, 12, 13, 17. Each adds a migration. Keep
  them as separate list entries and resolve by keeping all of them.
- `criteria.yaml`: 05, 15, 22, 24.
- `tasks/level.py`: 01, 03, 23, 24.
- `match.py`: 16, 22, 24.

## Steps only you can do (not agent chunks)

| When | Step |
|---|---|
| After 01, 02, 03, 05 and 06 are merged and the image is published | On gx10, run `jobtracker audit-pins` (dry), read the three buckets, then `--write`. The 05 tokens **must** already be live, or "Graduate …" pins are cleared for nothing. |
| After phase 2 merges | On gx10, `git pull` and `systemctl --user restart jobtracker-serve` (`serve` runs the working tree). Compare `/review`'s counts with the spec's §1 baseline. |
| Before the first phase 3 image | Write your `preferences.yaml` (chunk 15's maintainer file). Add `-v …/preferences.yaml:/app/preferences.yaml:ro` and the same for `profile.yaml` to both `~/jobtracker/run-*.sh` wrappers. Restart `jobtracker-serve` with `JOBTRACKER_PREFERENCES` set. |
| Four weeks after phase 2 | Re-measure against the spec's §8.5. |
