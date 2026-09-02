# A feed is not a board, and the difference is the whole design

Jobs reach this tracker from three kinds of place now. Sixty-eight ATS boards, which
answer "what is open at this company right now". Two community README feeds. And, since
this document, a Discord channel where a bot announces roles as it finds them.

The third kind broke an assumption the first two share, and finding out *why* is most of
what this subsystem is:

```
a board   is a complete statement   -> a posting missing from it has closed
a feed    is an incremental stream  -> a posting missing from it means nothing at all
```

`store.sync_postings` closes every posting absent from a fetch, and it is right to. A
Discord poll returns only what arrived since the last read, so on a normal night it
returns nothing — and routed through `sync_postings`, one quiet evening would close every
posting the feed had ever imported. That is why there is a `plugins/` package and a
`store.append_postings` rather than a fifth entry in `sources/`.

Everything else is an ordinary registry: one module, one import line, and the module is
pure.

---

## Two kinds of plugin

Everything above is about **import** plugins, and for a while they were the only kind. A
plugin now declares a `kind`, and there are two:

| kind | what it is | driven by |
|---|---|---|
| `import` | a feed of postings that is not a board (Discord) | `plugins/runner.collect`, from `cmd_check` |
| `task` | the switch for a bounded model role in `tasks/` | `tasks/runner`, from `jobtracker work` |

A task plugin implements **nothing**. It has no `page_url`, no cursor, no `parse_page` —
not stubbed out, genuinely absent, so handing one to the paging loop fails at the boundary
rather than three layers into a request. The model role stays implemented in `tasks/`,
identical to the ones that are not switchable; all the plugin adds is a line in
`plugins.yaml`. `plugins/roles.py` derives one switch per registered task, which is what
makes adding a role to the switchboard a change to a set of names rather than a module.

Two consequences worth stating:

- **`plugins/` may import `tasks/`; `tasks/` may not import `plugins/`.** `survey()` takes
  the enabled set as an argument, so a task module is pure and cannot tell whether it is
  switchable — and the queue does not depend on what is on disk.
- **`purge` is import-only.** It removes postings a feed imported, and a model role imports
  nothing; the postings it writes proposals *about* belong to whichever board owns them.

`level`, `judge` and `inbox` default to **on** — they predate the switch, and adding it was
not meant to change anyone's queue. `tailor` defaults to off, like any newly installed
plugin. A switched-off task is **absent** from `work --dry-run`, not listed as unavailable:
switched off is a decision you typed and there is nothing to go and fix, while a reason
printed beside it reads as a fault.

---

## Each plugin declares its own settings

There was one flat `DEFAULTS` dict here until the registry grew a second kind, and it did
not survive contact with one: every plugin's config surface was the union of every plugin's
keys, so `channel_id` was a valid setting on a model role and was `.isdigit()`-validated as
one.

Now `Plugin.defaults()` is the schema — an unknown key is refused, the **type** of a setting
is the type of its default, and semantic rules (`channel_id` is numeric, a day count is not
negative) live in `Plugin.validate()` on the plugin that owns them. `coerce` runs `validate`
too, so `backfill_days=-3` is still refused while you are standing there rather than three
layers down in `safewrite`.

A plugins.yaml written before any of this loads unchanged, key for key. There is a test.

---

## Running it

```bash
jobtracker plugins list                    # what exists, what is on, what it has imported
jobtracker plugins enable discord
jobtracker plugins set discord channel_id=123456789012345678 label=new-grad-jobs
jobtracker plugins set discord guild_id=987654321098765432
jobtracker plugins disable discord         # stops reading. Changes nothing else.
jobtracker plugins purge discord           # dry run: what it would remove
jobtracker plugins purge discord --write   # actually remove it
```

`jobtracker check` reads every enabled, available plugin as part of the nightly run.
There is no separate command, because a feed is a source of postings and belongs in the
pass that collects postings.

## Setting up Discord

The bot only needs to read one channel, and every step below has a failure mode that
looks like an empty channel — so verify at the end rather than trusting the clicks.

1. **Create an application** at <https://discord.com/developers/applications> → *New
   Application*.
2. **Get the token**: *Bot* tab → *Reset Token* → copy. It is shown once.
3. **Enable the MESSAGE CONTENT intent**: same tab → *Privileged Gateway Intents* →
   **MESSAGE CONTENT**. Under 100 servers you toggle it yourself.

   The naming is a trap worth knowing: it is listed as a *gateway* intent and this code
   opens no gateway — it is a plain REST GET. It gates the REST payload anyway. Without
   it every message arrives, correctly authored, with `content`, `embeds` and
   `attachments` blank.
4. **Invite the bot**: *OAuth2* → *URL Generator* → scope **`bot`** only → permissions
   **View Channel** (1024) and **Read Message History** (65536), which sum to 66560:

   ```
   https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=66560
   ```
5. **Check the channel's own permissions.** A server-level grant is not enough if the
   channel carries an explicit deny for the bot's role.
6. **Copy the ids**: *User Settings* → *Advanced* → *Developer Mode* on, then right-click
   the channel → *Copy Channel ID*, and the server name → *Copy Server ID*.
7. **Export the token**: `export JOBTRACKER_DISCORD_TOKEN='...'`
8. **Verify by hand before wiring anything.** Slugs are verified and never guessed here,
   and a channel is no different:

   ```bash
   curl -s -H "Authorization: Bot $JOBTRACKER_DISCORD_TOKEN" \
        -H 'User-Agent: DiscordBot (https://github.com/ida314/job-tracker, 0.1)' \
        'https://discord.com/api/v10/channels/<CHANNEL_ID>/messages?limit=1'
   ```

   Four distinguishable answers, and you want the fourth:

   | you see | it means |
   |---|---|
   | `401` | the token is wrong |
   | `403` | the bot cannot see the channel — View Channel |
   | `200` with `[]` | **no Read Message History.** Not a 403. See below |
   | `200` with `"content": ""` | the MESSAGE CONTENT intent is off |
   | `200` with real text | ready |

## The two failures that answer 200 with nothing

This vendor has two independent `greenhouse/hubspot`s — a reachable, authorized endpoint
returning nothing while everything looks fine. Both are named errors here, never zeroes:

- **Missing Read Message History** returns an empty list rather than a 403. Caught by
  `health.evaluate_plugin`, which treats an empty *first* read as suspect: a backfill
  reaching back two weeks that finds nothing at all is not a quiet channel.
- **Missing MESSAGE CONTENT** returns every message with its text stripped, so every
  format declines and the channel reports zero jobs while being full of them. Caught by
  `Discord.page_error`, which flags a page where *no* message has content. One blank
  message is ordinary — an attachment-only post. A whole page of them is configuration.

## Where each thing lives, and why it lives there

| | file | written by |
|---|---|---|
| Which feeds are on, where they point | `plugins.yaml` | a command you typed |
| The token | `$JOBTRACKER_DISCORD_TOKEN` | your shell |
| How far the reader got | `plugin_state` in `state.db` | the nightly run |

That split is DESIGN.md §3.3. The enabled flag is a decision you made; the cursor is
something a run found out. Keeping them apart is what lets you delete `state.db` and keep
your configuration, and what keeps a scheduled run from ever writing a curated file.

The token is in neither, and five rules come with it, because it is this repo's first
credential:

- **Env only, never `plugins.yaml`.** Gitignored is not the same protection as never on
  disk.
- **Never a build ARG** — `docker history` reads those back off a published image.
- **Never in a log line's `extra={}`** — the JSON formatter promotes those to top-level
  keys.
- **Never a span attribute, and therefore never a query parameter.** `fetch._request`
  records `url.full` on every request and logs the URL on every retry. Header only.
- **Never echoed by `plugins list`**, not even a prefix.

## Rules that are load-bearing

- **`append_postings` never closes by absence**, and its docstring says why at length.
  There is a test named `test_an_incremental_poll_never_closes_yesterdays_postings`.
- **It writes `description` at insert**, and this is not an optimization. Nothing else
  would ever write it: `_cache_descriptions` builds its work list inside `cmd_check`'s
  board loop, and a plugin group is not in it. A NULL description drops the row out of
  `level`'s queue *and* out of `matches_needing_judgment`, so it is never judged, never
  scored, and `rank.available` filters it out — present in the table, absent from the
  product. Plugin postings are deliberately kept *out* of `wanted` for the same reason:
  that pass writes `''` through a bare UPDATE and would erase the message text.
- **A verdict is recorded for every posting.** Every downstream query is
  `postings JOIN verdicts`.
- **Postings close by age, never by absence.** A channel cannot report that a req was
  filled, so `expire_after_days` (default 90) is the only signal available. Age is an
  honest thing to say because it is a statement about *our own observation* — "older than
  N days, and this feed has no way to tell us more" — rather than an inferred claim about
  the employer.
- **`health.evaluate_plugin` can never return `SUSPECT_EMPTY` for a routine poll**, and
  the docstring argues it. §7.1 reads an empty board as suspect because a board is a
  complete statement; a channel poll is not. Flagging it would put the feed on the Boards
  tab every single night — the dbt Labs mistake — and make the night the token expires
  look exactly like every healthy night. §7.3 is untouched: a 401 or a timeout is
  FETCH_FAILED, streaks, and degrades the run.
- **A failed read never advances the cursor.** A failure means we do not know what
  arrived; stamping it anyway would skip that window permanently with no error left
  behind. §7.3, applied to a cursor.
- **The cursor advances past messages we deliberately skipped.** It moves to the last
  message the poll *decided about*, imported or not. The two halves pull in opposite
  directions and both matter: a cursor that only moved for imported postings would stall
  forever on a channel whose recent traffic is all conversation.
- **A plugin's group is not curation.** It is never written to `companies.yaml` and never
  joined onto a `load_companies` result. Every consumer resolves companies with `.get`
  and degrades to tier `—`, so nothing breaks — and the absence is load-bearing in one
  place: `repair.detect` skips companies it cannot find, which is what stops a failing
  feed sending the slug-repair agent off to scrape a Discord careers page.
- **`purge` keeps your judgments and your applications.** `decisions` is the corpus
  `jobtracker eval` replays, `overrides` are your rulings, and "I applied here" stays
  true whatever happens to the posting row. It names what it is keeping rather than
  silently keeping it.

## Message formats

One bot's house style is not another's, so parsing is its own small registry under
`plugins/discord/formats/`. Two ship:

- **`cscareers`** reads the template this was built against:

  ```
  ## [Software Developer Associate @ Artera](<https://jobs.lever.co/artera-2/eae88c70-...>)
  ### Locations:  Seattle, WA
  ### Sponsorship: `Unknown`
  Posted on: July 31, 2026
  ```

  Role, employer, apply URL, location, sponsorship, and a real posted date — which beats
  the Discord timestamp, because the message reached the channel a month after the req
  opened and filing it under the message date would float stale reqs to the top of the
  ranking. The `<...>` around the URL is Discord's embed suppression and is stripped;
  left in, it would end up in `postings.url` and in every link on the dashboard.

- **`generic`** is the fallback: first link, first line as the title, message date. It
  **never guesses an employer**, because a guessed employer becomes a company name in a
  tracker whose whole discipline is that identity is verified rather than inferred.

Adding a third is one file plus one import line. Two things to know:

- Set `fallback = True` on a catch-all. Ordering is by that flag, not by import order —
  relying on the order of lines in `__init__.py` to keep `generic` last is exactly the
  action-at-a-distance breakage this codebase avoids.
- **Return `None` for a message you cannot read.** That is how it falls through to the
  next format. Formats are written not to raise, and the dispatcher catches anyway:
  "never raises" is a promise one malformed date can break inside `strptime`, and the
  cost of believing it would be a poll that dies half way through a channel.

Embeds need no format-side work. `flatten` renders an embed's title-and-url back as
`## [title](url)`, so a format written against markdown keeps working if the bot switches
to embeds — about fifteen lines that remove the whole class of "the bot changed shape and
the feed silently went to zero".

## Sponsorship

It rides along in the description, and goes no further. Not a criteria token — that would
be a gate applied before any title is read, which is the `locations_exclude` mistake that
discarded 390 postings unseen. Not in the title either: `match.py` reads titles only, and
"U.S. Citizenship is Required" would collide with `clearance` in `exclude_titles`.

If it earns a column later, motivate it with a count.

## Known blind spots

- **A message is an announcement, so nothing here knows when a job actually closed.**
  Age is a proxy and it is a poor one. The better mechanism is deferred, not rejected:
  group open plugin postings by the board their link points at — `dedupe.py` already
  extracts `ats:slug:id` — fetch that board once through the existing adapter, and close
  the ones whose id is gone. Three constraints when it is built: only ATS-shaped links
  qualify (a 404 probe lies, and Ashby serves a 200 shell for any URL); a failed or empty
  board fetch must close *nothing*; and it needs a per-run budget like
  `--max-descriptions`, oldest-checked first.
- **A captcha, a login, or a channel the bot cannot join** are all out of reach, and the
  honest answer is that this reads public channels in servers you administer.
- **One channel per plugin instance.** Several channels would want several entries, which
  the settings shape does not express yet.
- **`plugins.yaml` loses comments on a write**, because it round-trips through the YAML
  writer rather than the line-oriented splicer `companies.yaml` gets. Acceptable for four
  scalar keys with no prose; `plugins.example.yaml` is the documented copy.
