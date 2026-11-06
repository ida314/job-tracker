---
name: run-job-tracker
description: Start job-tracker's web UI (`jobtracker serve`) as a safe local preview, on private copies of every file it can write, then smoke-test it and hand back the URL. Use this whenever the user wants to run, start, launch, preview, open or look at the app, dashboard, or any of its pages (Today, /posting, /companies, /applications, /tuning, /settings), or asks to spin up a preview or see a change working, even if they don't say "serve". Not for the nightly pipeline (`check`, `work`, `prepare`), the test suite, or deploying to gx10.
---

# Run job-tracker

`serve` has live buttons, and by default they write to the real `data/state.db`, `companies.yaml`,
`data/submissions` and your download folders. Merely opening the database migrates it. So don't run
`jobtracker.cli serve` directly for a preview: `scripts/preview.py` gives it private copies instead,
and a click there can't reach your real files.

## Start it

Start the server as a long-running background command, from the repo root:

```bash
python3 .claude/skills/run-job-tracker/scripts/preview.py serve
```

Then, once it's started, smoke-test it in the foreground. This waits for the server by itself, so no
sleep is needed:

```bash
python3 .claude/skills/run-job-tracker/scripts/preview.py check
```

Give both commands the same `--dir` if you pass one; without it, `check` tests the preview in the
default folder, which may not be yours.

`check` requests every page, including one real `/posting` link, and confirms the real files haven't
changed. It exits non-zero if a page fails or a real file changed. If it fails, read the server's
output before retrying; a failure there is usually the actual bug. A pass only means the pages load:
it passes with no picks and no applications, so read the counts it prints. If the user cares about
something that shows 0, that count is your answer.

Use the URL `check` prints. The port moves if 8791 is taken, and a preview already on 8791 may be
running older code, because the server doesn't reload.

It runs whichever branch is checked out. To pick up code changes, stop the preview and run `serve` again.

## Choosing the data

The laptop's `data/state.db` is stale and has no rankings or applications, so by default the script
adds clearly labelled samples to its copy. Without them the Today tab and application cards are empty.

| The user wants | Use |
| --- | --- |
| To see the UI or check a change | `serve` (the default) |
| The laptop's data, no samples | `serve --no-seed --dir <new path>` |
| A specific database | `serve --db PATH --dir <new path>` |
| A clean restart | `serve --dir <new path>` |

The default folder is `~/.cache/job-tracker-preview`. Running `serve` again on a folder reuses what's
in it, including anything clicked earlier, and ignores `--db` and `--no-seed`. That's why the other
rows need a new `--dir`.

Samples are only added to empty tables, so a database with real rankings or applications never gets
mixed with fake ones. They don't cover flagged tailor terms, mail proposals or generated documents, so
controls that depend on those won't appear. If the user is checking one of those, tell them.

**If the user asks for their real data or applications, `--no-seed` alone can't give it to them.** The
laptop's copy can be empty, so say that up front rather than presenting an empty page as the answer. The
live data is on gx10: point them to its dashboard (the `deploy-target-gx10` memory note has the URL and
says ssh from the laptop is rejected), and warn them that clicks there change real data. To preview real
data safely, they need to copy gx10's database to the laptop themselves, then you can use `--db`.

## Seeing a page

If you need to confirm how something looks, take a screenshot and look at it. A 200 only shows that the
page was generated. A screenshot can't show hover or anything else that takes a pointer, so either drive
it with Playwright from a throwaway environment (`uv run --no-project --with playwright`, launched with
this same binary as `executable_path`) or tell the user you didn't check that:

```bash
~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux-arm64/chrome-headless-shell \
  --no-sandbox --disable-gpu --hide-scrollbars --virtual-time-budget=2500 \
  --window-size=1280,1400 --screenshot=<out.png> '<url>'
```

## Tell the user

Give them the URL and the branch, then say plainly:

- whether the data includes samples, so they judge the layout and not the made-up rankings
- that clicks only reach the preview's copies. It still runs the repo's code, and "Verify and add" on
  `/companies` still makes real read-only requests to the job board
- that the server only listens on this machine
- how to stop it: they can ask you, or run the `kill` line that `check` prints

To stop it yourself, stop the background task. If you started it in a terminal, Ctrl-C works too. Don't
use a `pkill -f` pattern that also appears in your own command, because it kills its own shell.

## If it refuses to start

- **`.venv` is missing:** it's untracked and nothing recreates it. The error gives the rebuild command.
- **"settings this script does not redirect":** `config.py` gained a setting that defaults into the
  repo. Add it to `COPIED` or `EMPTY_DIRS` in `scripts/preview.py`. Don't work around this, because that
  setting would write to the real file from inside the preview.
