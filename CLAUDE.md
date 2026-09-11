# CLAUDE.md

Working notes for any agent operating on this repo.

**This file is rules only.** The reasoning, dates and measurements behind them are in
`docs/notes-archive.md`; per-subsystem guides are in `docs/`. Per-company reasoning lives in
`companies.yaml` `notes:`, and the match rules live in `criteria.yaml` — don't duplicate
either here.

**Keep this file current.** When a rule changes, change it in the same commit.

---

## Goals

- **Backend new-grad, 2027 graduation.** Distributed systems, infrastructure, platform, data
  engineering, SRE. Not frontend, not ML-first.
- **Skill and career growth over pay or prestige.** Tier 1 (backend scale-ups) and Tier 2
  (infra/devtools) are the anchor; Tier 4 (Big Tech) is applied to, not anchored on.
- **Tiers rank growth opportunity, not product category.** Observability being the product is
  not a reason to promote a PE-owned take-private.
- **Location ranks, it does not disqualify.** NYC first, then elsewhere in the US, then
  unspecified, then outside the US. `match.location_rank()` only sorts. A geography gate
  firing before the level gate once discarded 390 postings unread — don't reintroduce one.
- **A near-empty board is the expected state on tiers 5–7.** Don't flag those `SUSPECT_EMPTY`.

---

## Field schema

Every company is an `### Name` heading followed by a flat `- key: value` list. Keep the field
order; the parser and every `awk`/`grep` sweep depends on it.

| Field | Meaning |
|---|---|
| `ats` | `greenhouse`, `lever`, `ashby`, `workday`, `gem`, `bespoke`, `aggregator`, `unknown` |
| `slug` | Board identifier. A Workday slug is the *triple* `tenant/dc/site` (`redhat/wd5/jobs`) — the data centre is part of the hostname and is not derivable from the tenant. Empty for `bespoke`. |
| `board_url` | Full JSON API URL for `api`; raw README URL for `aggregator`. Empty for `manual`. |
| `careers_page` | Human-facing careers URL. The fallback when the API breaks. |
| `category` | Free-text bucket, e.g. `data-infra`, `fintech-backend`. |
| `check_method` | `api`, `manual`, or `aggregator`. Governs what the agent may do. |
| `status` | `not-open` / `open`. |
| `last_checked` | Date of the last check. Set on every run, including no-match runs. |
| `last_posting_seen` | Appended title + date when a matching role is found. |
| `notes` | Free text. Record *why* a field is what it is, especially after a fix. |

```
greenhouse  https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
lever       https://api.lever.co/v0/postings/{slug}?mode=json
ashby       https://api.ashbyhq.com/posting-api/job-board/{slug}
workday     POST https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
                 {"appliedFacets":{},"limit":20,"offset":N,"searchText":""}
```

**The tracker markdown is a stale mirror** — v2 keeps run state in `state.db` and never writes
back. Query `state.db` or open the dashboard; don't read the markdown to learn what happened.

---

## Rule: slugs are verified, never guessed

A slug is correct only once a live fetch confirms it belongs to the right company.

- **A 200 with jobs does not mean the slug is right.** `ashby/cedar` serves an unrelated Cedar.
  Confirm identity, not reachability: Greenhouse `GET /v1/boards/{slug}` returns `.name`; Ashby,
  read `.jobs[0].jobUrl` and a few titles.
- **A 200 with zero jobs does not mean "no openings."** `greenhouse/hubspot` is a real, always
  empty board; the live one is `hubspotjobs`. An empty board is `ATS-check-failed`, never
  "no matches".
- **Never verify an Ashby slug by fetching `jobs.ashbyhq.com/{slug}`** — that host 200s a SPA
  shell for any string. Use the posting-api or the `ApiJobBoardWithTeams` GraphQL operation.
- **Ashby, Lever and Workday identity is tautological** — it is derived from a URL restating the
  slug we asked for. Those boards are only ever evidenced as *reachable* or by *provenance*
  (a careers page served the link), never as verified identity.
- **Lever slugs are case-sensitive** (`lever/Onehouse`, not `lever/onehouse`).

When a slug fails, read the real one off the careers page — the Greenhouse embed
(`job_board?for=X`), or a link to `job-boards.greenhouse.io/X`, `jobs.lever.co/X`,
`jobs.ashbyhq.com/X` — then update `ats`, `slug`, `board_url` and `notes` together. That
procedure is `jobtracker repair` (`docs/repair.md`); do it by hand when the page is a
JavaScript shell, the documented blind spot.

**Valid but currently empty — do not "fix":** `greenhouse/root`. (`greenhouse/dbtlabsinc`
404s, so it is `FETCH_FAILED`, not empty; its slug needs reading off a JS-shell page by hand.)

---

## Rule: `manual` companies are never scraped

If `check_method: manual`, do not fetch, scrape, or reverse-engineer a portal. The agent adds
them to a "check these by hand" list in the daily report, at most once per week per company,
rate-limited via `last_checked`. Never let a `manual` entry silently report zero — surfacing a
company for manual review is correct, pretending to have checked it is not.

Two clarifications, both from an audit that found the premise wrong for six entries:

- **Never scrape a company whose portal has no public JSON board.** Absolute.
- **`manual` is a finding, not a category.** It records that nobody found an endpoint. The right
  response to "is there one?" is to look — at the network the page makes, not its DOM. Finding
  one is a reclassification. A found endpoint is *verified before it is written*, same as a slug.
- **A JSON board is not automatically usable.** Amazon's is keyless and still unreadable: offset
  paging is hard-capped and a run takes ~9 minutes before failing. A board that ends
  `FETCH_FAILED` every night is not coverage.

Recon shortcuts worth not repeating: Snowflake is Phenom People (`/api/apply/v2/jobs`, tenant
param unknown; `ats: unknown` is known to be wrong). Retool's Gem endpoint answers **403**, so
it is real and gated. Epic (Avature) and YC Work at a Startup are login-walled; Coralogix's
Comeet token is minted client-side. SAP's board is server-rendered HTML. For Workday guesses, a
wrong tenant **422**s and a right tenant with a wrong site **401**s — `intuit.wd1` 401s, so that
tenant exists. A 401 or 422 is FETCH_FAILED, never "no openings". Full table in the archive.

---

## The tuning loop

`criteria.yaml` is easy to edit and hard to edit safely: a token added to stop one bad match
silently changes the verdict on thousands of postings already judged. See `docs/tuning.md`.

- **Never hand-edit `criteria.yaml` without running `jobtracker eval`.** It replays the current
  rules against every recorded judgment and exits 1 on a regression. When `decisions` is empty
  and `eval` has no corpus, replay `match()` over all stored postings under old and new criteria
  and diff every verdict instead. A criteria edit with no regression evidence is the thing this
  loop exists to prevent.
- **`uncertain` is not a regression.** Only active contradiction blocks. Counting it as failure
  pushes toward rules that guess level from titles.
- **The admission test for a token is not "does it catch the bad thing", it is "what else does
  it catch"** — and aggregator titles are `"Employer — Role"`, so **every token also reads the
  company name**. `cadence` rejects *"Cadence Design Systems — Software Engineer New Grad"*;
  `analog` and `semiconductor` were refused for the same latent reason. Bare `verification` and
  bare `hardware` are refused too: formal verification is backend work and *"Software Engineer —
  Hardware Tools"* is a software job.
- **`_compile` anchors on alphanumeric boundaries, so a plural walks through an exclusion** —
  `internship` does not fire on *"2027 Internships"*. Sweep `exclude_titles` for escaping
  plurals before trusting the list.
- **The engineering gate can be satisfied by a non-engineering role.** `role_type_include` tokens
  like `systems` fire on operations titles; that is why `role_type_include` and
  `engineering_terms` are separate lists, and why `role_type_exclude` exists.
- **Overrides outrank rules** and survive rematch, carrying `decided_by`. They are applied in the
  caller path (`cmd_check`, `cmd_rematch`, `serve`), never inside `match()` — that function's
  purity is load-bearing for the tests. A model pass may never displace a human ruling; the
  reverse is allowed. A rules change does not move a posting an override pins, so a criteria edit
  may need `store.clear_override` — **only** where a model guess contradicts the new rules.
- **`check` re-derives a rules verdict from the title for every posting it fetches**, so an
  unpinned model verdict lasts one night. That is why `resolve`/`level` pins.
- **Rejects are kept.** Do not replace them with a hash set of "seen and rejected": `cmd_check`
  re-deriving `match()` over every posting is what makes a criteria edit reclassify all of
  history with no backfill, and `eval` replays against that same corpus. The fetch unit is the
  board, not the posting, so filtering at ingest saves no network either.
- **Suggestions are string counting, not a model**, scoped to rejects the rules do not already
  handle. Do not feed them to an LLM.
- **`decisions.title` is denormalized on purpose** — joining to `postings` would shrink the
  corpus every time a req closed.
- **`/tuning` has no delete control**, and adding one is not a small feature: removing a token
  silently re-admits everything it was rejecting. A suggestion may only target a *reject* list
  (`_SUGGEST_TARGETS`). Location lists are deliberately absent — they rank, they never gate.

---

## Operational notes

- Fetching `boards-api.greenhouse.io` ~12-way parallel gets egress throttled; curl then returns
  `http=000` for *every* host. It looks exactly like mass breakage. Pace requests and re-check
  sequentially before believing a failure.
- Drop `?content=true` when you only need job counts.
- `check` exits 0 (clean), 2 (a board needs attention), or 1 (could not run). Exit 2 is narrower
  than `status != OK` — see `health.is_degraded()`.
- **Containers must set `TZ`.** The image is UTC; `date.today()` drives `first_seen`, the report
  window and `manual_due()`.

### Observability

`docs/observability.md`. Progress goes to **stderr** via `logging`; the report goes to
**stdout**, so `check > out.md` stays clean.

- **Log format follows the terminal** — TTY gets human lines, a pipe gets one JSON object per
  line. `JOBTRACKER_LOG_FORMAT=json|text|auto`. Still stderr-only.
- **`fetch_all` uses `as_completed` but reassembles into input order.** Don't simplify back to
  `pool.map`; downstream reproducibility depends on it.
- Retries are logged **including ones that succeed** — a board that quietly needs two tries every
  day is degrading. The end-of-fetch summary reports failures, retries and cumulative time asleep
  in the per-host limiter; near-zero pacing plus a fast run means something returned early.
- **`jobtracker/telemetry.py` is the only file allowed to import `opentelemetry.sdk`.**
  Instrumented modules import the API only, which is a no-op with no provider configured.
- **Worker threads do not inherit OTel context** — `fetch_all` captures it and `_fetch_timed`
  attaches it. Remove that and you get 56 orphan traces.
- **Span names stay low-cardinality**; identifiers go in attributes, and never posting ids or
  URLs. Metric attributes stay bounded — `ats` and `outcome` yes, `company` no.
- **Metrics are pushed, never scraped** (a 30-second daily process is never running when a scrape
  fires), and **counters are exported DELTA**, reassembled by the collector's
  `deltatocumulative`. Cumulative would reset to zero every run.
- **`service.instance.id` is pinned to the hostname**, so **containerized runs MUST set
  `JOBTRACKER_INSTANCE_ID`** — inside a container the nodename is the container id, which does
  the opposite of the pinning's intent.
- **Prometheus sees monotonic counters**, so `last_over_time` on one is the all-time total. Use
  `increase(...[24h])`. "How long since the last run" is
  `time() - max_over_time(timestamp(jobtracker_run_duration_seconds_count)[24h:1m])` — the
  obvious `timestamp(last_over_time(...))` form always returns 0.
- `otel/grafana-dashboard.json` is the source of truth; datasource `uid`s are pinned because the
  dashboard binds by uid. **Panel descriptions carry the why** — keep them current.
- `otel/stack.sh run` mounts the repo's `./data` — a real run against real `state.db`.

---

## The HTML dashboard

`jobtracker dashboard` renders `state.db` to a single self-contained HTML file. Four tabs;
**Today is the landing screen**, Applications second — work already committed to outranks the
raw corpus.

- **It is a pure read.** Unlike `report`, it never marks manual companies as surfaced.
- **No network at view time, ever.** No CDN, no chart library — the one chart is CSS. That is
  what makes the file mailable and openable offline.
- **Rows render server-side; JS only hides them.** Panels are server-rendered and the script only
  toggles `[hidden]`; `.tabs` is `display:none` until JS confirms it is running. Don't "improve"
  this into client-side rendering from embedded JSON.
- **Escape everything.** Titles and locations come from third-party APIs; URLs get a scheme check
  too, or a `javascript:` href executes on click.
- **Location sorts, never filters.** The dropdown defaults to "Anywhere".
- **Tier color is three bands, not seven steps**, and the number is always printed — color is
  reinforcement, never the encoding.
- **The postings tables group by company.** Rows render visible and JS collapses on load, never
  the reverse. **Filtering owns `hidden`; collapsing owns `tbody.closed`** — two owners of one
  property is how they drift. Any active filter force-expands matching groups, with `data-closed`
  remembering the choice. **A data row is one carrying `data-search`**; group heads do not, which
  keeps them out of "N of M shown".
- **The picks must never be a `table[data-filterable]`**, and neither must the applications panel
  — a filter set on another tab would silently empty a curated list. Both have tests.
- **Today's `<details>` drawer** is built from `rank.available(...)[3:]`, never raw
  `ranked_matches`, or a job you applied to this morning reappears. It carries **no `data-act`**
  in either mode: that attribute must keep meaning exactly the three disposition buttons.
- **Disposition buttons only exist under `serve`** (`interactive=True`). A dead button in a
  mailed file is worse than no button. Counts render in both modes.
- **A button's handler lives in the file that renders the button.** `server._JS` and
  `dashboard._JS` are two scripts on several pages; tests assert the button exists *and* that the
  page's script listens for it.
- **The emitted script has to parse.** In a `"""` block, `\n` is a real newline inside a JS string
  literal and kills the whole script — every handler on every page emitting it — with no symptom,
  because a page whose script never ran still renders perfectly. Use `\\n`. Parity tests assert a
  handler was *written*, not that it parses;
  `test_no_emitted_script_carries_a_newline_inside_a_string` is the one that can see it.

### The actions cell

One cell on every posting row that is not a pick, carrying **+ tracker** and, when `tailor` has
proposed something, a chip and a way to get the compiled PDF.

- **`interactive`-only, whole column** — no `<th>`, no `<td>`, nothing in the static file. The
  header and the group head's colspan come out of the same two flags in one place.
- **The button is `button.track` and carries no `data-act`.**
- **It reuses `/api/disposition` with `action: "applied"`** — no new write path.
- **A row already in the tracker renders a state, not a control.**
- **The handler swaps in place; it does not reload** — a reload discards the typed filter and
  scroll position. Both handlers are **delegated**, which is also what keeps them working after a
  button is replaced by a chip.
- **The tailor chip is absent when nothing was proposed** — `tailor` ships off, and a permanent
  "0 edits" on thousands of rows is noise.
- **`resume.tailored_stem` / `tailored_path` are the single derivation.** Nothing stores the
  path; the file's existence is what "built" means. A second copy of that expression is how the
  button and the terminal come to mean different files.
- **`tex` copies the source the `↓` compiles** — `GET /api/tailored-tex`, a pure read through
  `server._tailored_source`, the one derivation `/api/tailor-build` also calls. It needs no TeX
  engine. `serve` over tailnet http is not a secure context, so the handler falls back to
  `execCommand('copy')`.
- **`/api/tailor-build` is start and poll at once**, idempotent, one build per posting, and a
  failure is reported once then cleared. Everything knowable is decided before the thread exists,
  and a refusal **names the missing engine** — an exception on a daemon thread reaches the log
  and nowhere else. A success drops its `_BUILDS` entry rather than writing "ready".

---

## Applications: the outer loop

`jobtracker applications`, `/applications` under `serve`, and a read-only fourth tab in the
static dashboard. `docs/applications.md`.

- **Two writers, deliberately.** `record_application` sets state and logs nothing;
  `add_application_event` appends and changes nothing; `advance_application` does both.
  Folding the append into the upsert would either duplicate an event on a note edit or suppress a
  second interview round as a no-op — and from inside an upsert those look identical.
- **`interview` is one repeatable status, not numbered rounds.** Rounds are repeated events.
- **There is no `ghosted`.** Silence is derived from `updated_at` (30 days). A status only the
  user can set is one they will not set.
- **Optional columns update through `COALESCE(excluded.col, applications.col)`** — a plain
  assignment blanks a URL set from the web page on the next status change. At the API layer:
  absent/null = leave it, `""` = clear it. Same rule as `sync_postings` and `posted_on`.
- **`source` is nullable and read as `COALESCE(source, 'tracked')`.** A `NOT NULL DEFAULT` fires
  only when the INSERT omits the column, and this one is always bound.
- **Manual ids are minted and deterministic** (`manual:<slug of title>`). Manual rows have no
  `postings` row, which is why `all_applications` reads the table directly — join it and they
  vanish.
- **The static tab has no buttons and no `interactive` flag.** A test asserts the rendered button
  set and the handler set match exactly.
- **A refused write writes nothing** — `{"ok": false}` at HTTP 200, both tables untouched. A date
  that does not parse is a refusal, never stored raw, inside the one feature whose job is to
  remind you.
- **`rank.is_available` excludes on the presence of a status, not on which one.** A rejection does
  not put the job back in tomorrow's top 3. A test walks all seven.
- **`applied_at`/`updated_at` are timestamps; `next_action` is a day.** Every comparison goes
  through `applications.day_of`; `days_since` returns None, never 0, and an unreadable
  `updated_at` sorts last.

---

## Reading the mailbox

`jobtracker mail`, the `inbox` task, and a review list on `/applications`. `docs/mail.md`. The
first thing other than the user to touch the outer loop, which is why it may only propose.

- **`mail` is to `inbox` what `check` is to `level`.** The deterministic pass does the I/O and
  caches into `mail_candidates`; the task is a pure read whose only socket is the router. Do not
  give the task a mailbox.
- **Nothing writes to the maildir.** `mailbox.Maildir(path, factory=None, create=False)` —
  `create` defaults to **True**. `keys()` + `get_bytes()` and nothing else. Two tests.
- **`Message-ID` is the identity; the maildir filename is not** (a client renames it the moment
  you read the message). No header → `synth:<digest>`.
- **The narrower is built from `applications`,** so a message can never be a candidate for a
  company you never applied to. Domains are *read* off URLs you applied at, never synthesized
  from a name; ATS relay domains identify the ATS, never the company. Names match on whole tokens
  and only in the From display name; a name in the subject additionally needs an
  application-shaped word; a name in the body alone is never enough.
- **An unresolved job is asked about, not guessed** — `ats_job_id=''` plus a `choices` list.
- **`read_at` NULL is the queue**; non-NULL with no proposal means "read, and not application
  news". Rejected messages are deliberately not stored, so a message predating an application can
  become a candidate later.
- **"Nothing here" is an answer and is written**, unlike `level`'s `unclear` — copying `level`
  would fill the blocked-unit count with healthy readings. Only a transport failure leaves a
  message unread.
- **The model's quote must appear in the message verbatim.** It is the only free text it
  produces, and grounding it is what keeps a fabricated rejection off the list.
- **`unit_key` is the message id**, or two ambiguous messages at one company collapse onto one
  answer.
- **Accepting is the only path into `applications`,** and the event note is composed by Python.
  Dismissed is a resolution, never a delete.
- **No scan endpoint, ever.** Walking a mailbox on a single-threaded `HTTPServer` blocks every
  other request. A test asserts `server.py` imports neither `maildir` nor `mailbox`.
- **The banner is a derived count, not a `seen` flag** — a flag would have to be written by a GET
  or by JS.
- **`state.db` holds the text of personal mail.** No log line or span attribute may carry a
  subject or a body.

---

## Import plugins

`jobtracker plugins`, `jobtracker/plugins/`, a card on `/settings`, one extra loop in
`cmd_check`. `docs/plugins.md`. One module plus one import line; module pure, `runner.py` owns
the socket.

**A plugin has a `kind`.** `import` is a feed of postings. `task` is the switch for a bounded
model role in `tasks/`; it implements nothing and has no `page_url`/cursor/`parse_page`
(genuinely absent, so the paging loop fails at the boundary rather than three layers in).

- **`plugins/` may import `tasks/`; `tasks/` may not import `plugins/`.** `survey()` takes the
  enabled set as an argument, so a task module cannot tell whether it is switchable. Tested off
  the source.
- **A switched-off task is absent from the survey, not unavailable** — switched off is a decision
  you typed, and a reason printed beside it reads as a fault. `work --task <off>` says so and
  exits **0**. The switch comes **before** the query: a disabled task is never asked for
  `unavailable_reason` and never asked for `pending()`.
- **`level`, `judge` and `inbox` default to on**; new plugins default to off.
- **`purge` is import-only.** A model role imports nothing.
- **Each plugin declares its own settings** (`defaults()`); the type of a setting is the type of
  its default. Semantic rules live in `validate()` on the owning plugin, and **`coerce` runs
  `validate` too**. **A cursor is described by the plugin that minted it** (`describe_cursor`).
- **A feed is not a board.** `sync_postings` closes every posting absent from a fetch — right for
  a board, which is a complete statement; a poll returns only what arrived since the last read,
  so one quiet evening would close everything the feed ever imported. `store.append_postings`
  never closes by absence. There is a test named after it.
- **`append_postings` writes `description` at insert**, and that is not an optimization: nothing
  else would write it, and a NULL description drops the row out of `level.pending()` **and**
  `store.matches_needing_judgment` — present in the table, absent from the product. Plugin
  postings are kept out of `_cache_descriptions`' `wanted`, whose no-detail-endpoint branch would
  erase the text.
- **A verdict is recorded for every plugin posting** — every downstream query is
  `postings JOIN verdicts`.
- **Postings close by age, never by absence** (`expire_after_days`, default 90). A channel cannot
  report that a req was filled; age is honest because it is a statement about *our observation*.
- **`health.evaluate_plugin` can never return `SUSPECT_EMPTY` for a routine poll**, and it lives
  in `health.py` because a second health policy in `cli.py` is what that module prevents. **The
  one exception is an empty *first* read** — a backfill that finds nothing is very likely a
  missing Read Message History permission, which answers **200 with `[]`, not 403**.
- **Discord has two silent-empty modes**: that permission, and a missing MESSAGE CONTENT intent,
  which returns every message with `content`, `embeds` and `attachments` blank. `page_error` flags
  a page where *no* message has content; one blank message is ordinary, a whole page is
  configuration.
- **A failed read never advances the cursor**, and **the cursor advances past messages we
  deliberately skipped** — stamping on failure loses the window, moving only for imported postings
  stalls forever on a chatty channel. `page_cursor` reads the **raw** page.
- **A plugin's group is never curation.** Not written to `companies.yaml`, never joined onto a
  `load_companies` result; every consumer uses `.get` and degrades to tier `—`. The absence is
  load-bearing in `repair.detect`, which skips companies it cannot find — that is what stops a
  failing feed sending the repair agent to scrape a Discord careers page.
- **`purge` keeps `decisions`, `overrides`, `applications` and `application_events`.** Dry by
  default, `--write` applies.
- **The one credential is env-only.** `$JOBTRACKER_DISCORD_TOKEN`: never `plugins.yaml`, never a
  build ARG, never in `extra={}`, **never a query parameter** (spans and retry logs record URLs),
  never echoed by `plugins list`. Header only, per-request — a *session* header would carry it to
  every board in `companies.yaml`. Two tests.
- **`plugins.yaml` is curation, `plugin_state` is observation.** `load_settings` is strict, and a
  malformed file **stops the feed** rather than reading as "no plugins". On the Settings page it
  instead degrades to a banner so the page you would open to fix it still renders — and it may not
  offer a one-click fix, since `set_options` reads before it merges.
- **The Settings card carries the switch and nothing else** — no `set`, no `purge` beside a
  toggle, and never the token. **Every registered plugin gets a card, on or off**, or the page
  cannot answer the question you open it with. The request carries the state it wants, not
  "toggle", so a double click lands as one flip. `POST /api/plugin` calls `settings.set_enabled`,
  the same and only writer the CLI uses.
- **Formats are their own registry**, ordered by a declared `fallback` flag rather than import
  order. A format returns `None` to fall through; the dispatcher catches exceptions anyway.
  **`generic` never guesses an employer.** Sponsorship rides in the description and goes no
  further — a criteria token would be a gate applied before any title is read.
- **`prepare` has a third outcome**: a pick whose source could never publish or learn a form is
  "apply on the employer's own page" and does not count against `ready`.

---

## URL dedupe

`jobtracker/dedupe.py`, three columns on `postings`, two calls in `cmd_check`. `docs/dedupe.md`.
Runs across **every** source, not just plugins.

- **The key is not the URL string.** ATS identity is extracted from the path first
  (`lever:artera-2:<uuid>`), and **before the query is dropped** — Greenhouse's
  `embed/job_app?for=X&token=Y` carries its identity in the query. Normalized URL is the fallback.
- **`key_from_identity` is the primary rule for api rows.** Most stored URLs are on careers sites,
  not the ATS's host, so a URL-keyed corpus would be largely unreachable by a feed link.
- **The path is not lowercased** (paths are case-sensitive) **but the slug and id inside an ATS
  key are** (case-insensitive identifiers).
- **`gh_jid` is identity and must survive normalization.** Drop it and every board linking its
  reqs to one careers page collapses onto a handful of keys. `t` and `utm_*` are tracking and are
  dropped.
- **Precedence: only a feed is closed by a peer** — not "unless it is api". An uncurated company
  has no `check_method`; ranking it below a feed would make forgetting an entry in
  `companies.yaml` a way to close live rows. Unknown loses to a board, outranks every feed, and is
  never peer-closed. Two board rows sharing a key are logged at WARNING with neither touched —
  the only way a too-coarse key becomes visible.
- **The index cannot live in `_SCHEMA`.** `connect()` runs `executescript(_SCHEMA)` *before*
  `_apply_column_migrations`, so an index on `postings(dedupe_key)` raises `no such column` on any
  pre-existing database — and passes on every freshly built one, which is every database in the
  test suite. `_ADDED_INDEXES` is applied after the columns.
- **`sync_postings` must not reopen a dedupe closure.** The reopen is conditional on
  `closed_reason IS NULL`, i.e. on the closure having come from absence.
- **`dedupe_key` is a plain assignment, not COALESCE** — the key is derived from the same
  statement's URL, and a URL that moves must take its key with it.
- **`close_duplicates` runs once per check, after every board has synced**, never inside the board
  loop, or which row survives depends on the ordering of a curated file.
- **The reason lives in `closed_reason`/`duplicate_of_url`**, not in `verdicts` (rewritten nightly)
  and not in `overrides` (which mean "I ruled on this role", while a duplicate is a fact about the
  *row*).
- **Blind spot, documented not hidden:** a `simplify.jobs/p/<uuid>` row and its direct twin do not
  dedupe. A test asserts the miss. Workday is deliberately out of the URL extractor and covered by
  `key_from_identity`.
- **The first run that closes anything must say how many in the run log**, or a legitimate cleanup
  reads as a regression at 2am.

---

## The task queue

`jobtracker work`, `docs/tasks.md`. Where all model work lives: `level`, `judge`, `inbox`,
`tailor`. The scheduler polls tasks by priority and runs the first with work.

- **A task only ever runs when a model is reachable** (`cmd_work` returns early with no router;
  `_work` bails on a failed `probe()`). Correct for a model pass, silently wrong for anything
  deterministic — which is why `prefill` left the queue. **Anything that needs no model and must
  always run stays out of the queue**, including scoring.
- **`work` rescores after every run.** `judge` writes a ranking with a NULL score, and both
  `today` and `prefill` only consider scored postings, so without it the score stays NULL.
- **Priority is the pipeline's dependency chain, not a preference**: level → judge, each producing
  what the next consumes. **`inbox` (40) is outside that chain** and is last on a starvation
  argument — its queue refills from an external stream. **`tailor` (50)** consumes what `judge`
  produces and feeds nothing; it is behind `inbox` because its unit key hashes the resume text, so
  one edit re-keys every posting at once. Tested.
- **The queue is derived, never stored.** Each task's `pending()` is a SQL read over existing
  tables. `task_attempts` is a *failure ledger* — three consecutive failures set a unit aside —
  not a work table.
- **Every unit commits on its own.** A task that raises while writing is rolled back to the last
  committed unit; an interrupted run still wrote everything before it.
- **`unit_key` is the question, not the posting.** Change the question and every unit is new with
  its retry count reset. It is also the router's idempotency key.
- **`pending()` must only return work the task can actually do** — `level` excludes postings with
  no cached description. Counting what it cannot reach overstates a backlog and sends a budgeted
  run to guaranteed no-ops.
- **Task modules are pure**; `runner.py` owns every socket, transaction and clock. Adding a task
  is one module plus one import line.
- **`jobtracker prepare` is the nightly "is tomorrow useful?" check** — rescore, take what `today`
  will surface, exit 2 if any has no plan. **Gaps never cause exit 2**: an unanswered question is
  the normal state, and failing on it makes the unit permanently red for something only the user
  can clear.

---

## The ambiguity pass

The `level` task, `docs/llm.md`. **Local only** — the model is an address, and there is no
API-key handling anywhere in `jobtracker/llm/`. Do not add a hosted provider.

- **Transport is the `sir-client` SDK.** The `Provider` interface and its registry were deleted;
  the router is that indirection now. `llm/` is `wire.py` (pure) and `client.py` (the only module
  that opens a socket). **The SDK is async-only**, which is why `tasks/runner.py` is async and why
  `browser.py` — Playwright's sync API — is a separate module. Do not merge them.
- **`sir` forwards the body untouched**, so routing through a router guarantees nothing: the
  parsers are still the only thing between a backend that ignores the schema and a fabricated
  verdict.
- **The schema request is `response_format`, not `guided_json`.** vLLM accepts a body carrying the
  old pair, *ignores* it, and answers in prose — which made the whole pass a silent no-op.
  **Diagnostic: `work` reporting ~zero applied while the server is up means the wire format, not
  the model.** Verify against the server you actually run; a mock backend that ignores the schema
  reproduces it exactly.
- **Scope is level extraction only.** The model never decides that a role is on-target; an `entry`
  reading still has to pass the rules' engineering gate.
- **Every failure path must leave the posting UNCERTAIN.** Unreachable, timeout, malformed,
  unsure. Nothing here may raise for a down server, and a code path that can produce a verdict from
  a failed call is a bug.
- **The `level` task is a pure read** — `check` cached the descriptions, so a throttled board can
  no longer shrink the queue it considers.
- The queue is scoped to titles with an engineering signal. Do not "fix" the no-signal blind spot
  by rejecting no-signal titles.
- **Known doc bug:** this file, `docs/llm.md` and `resolve.py`'s docstring have claimed
  *"Member of Technical Staff"* stays UNCERTAIN. It does not — `staff` is in `exclude_titles`, so
  `match()` rejects it long before the model could look. Either make the title reachable or stop
  saying it is, and decide with `jobtracker eval`, not a bare YAML edit.

---

## Descriptions are cached by `check`

`cmd_check` stores a description for every posting whose verdict is `match` or `uncertain`. That
is what makes the model passes offline with respect to the ATSes.

- **Scoped, deliberately.** Open rejects are excluded. Self-healing: retune criteria so a former
  reject becomes a match, and the next `check` fills it in.
- **Write-once.** `NULL` = never fetched, `''` = fetched and genuinely empty. Only NULL is retried.
- **`--max-descriptions` (default 400)** caps requests per run so a bad night cannot turn a
  30-second job into a 40-minute one.
- **A description failure is invisible to board health** and must never produce `EXIT_DEGRADED`.
  A 500 on one job detail is not a broken board.
- Ashby and Lever ship `descriptionPlain` in the bulk payload; only Greenhouse needs a per-posting
  fetch, and its `content` is HTML-escaped *inside* the JSON string — unescape before stripping
  tags. Fetches go through `Fetcher._request_json` to inherit the per-host limiter: the ATS is the
  scarce resource, not the local model.

---

## Posted dates

`postings.posted_at` is the vendor's raw value and is **five mutually incomparable formats**
(Greenhouse ISO-with-offset, Ashby ISO-UTC-with-millis, Lever epoch-millis as a string, aggregator
a relative age, Workday relative English prose). As text an epoch string collates before every ISO
timestamp. **Nothing may sort on it.**

`postings.posted_on` is that value normalized to a plain ISO day, and the only date anything may
compare. Conversion lives in `Source.normalize_posted_at(raw, today)`; `today` is a **parameter,
not a clock read**, because adapters are pure and three sources date relatively.

- **Greenhouse's bulk field is `updated_at`, which is not a posted date** — it moves whenever
  anyone edits the req. The real value is `first_published`, on the detail payload, so it arrives
  with the description fetch. Workday's `startDate` upgrades its relative prose the same way.
- **`sync_postings` writes `posted_on` with COALESCE** — a bulk pass with no date must not erase
  one already stored.
- **Unparseable input is NULL, never today.** A missing date reading as "posted now" would invert
  the ranking it exists to inform. `first_seen` is not a substitute.

---

## The ranking pass

`jobtracker rank` and `jobtracker today`, `docs/ranking.md`. Where `level` decides whether a
posting is *on-target*, this decides which is *urgent*. Judging is the `judge` task; **scoring
deliberately is not a task** — it needs no model and must run regardless.

- **The model judges one posting; Python does the ordering.** It returns three labelled ordinals
  (`backend_fit`, `growth`, `entry_risk`) plus a sentence — never a score, never a comparison, and
  it never sees another posting.
- **Ordinals, not 0–100.** LLM numeric scores cluster narrowly and shift with any prompt change,
  silently re-ranking everything.
- **Scores are absolute**, so a new posting lands in its slot without disturbing anything else. Do
  not replace this with pairwise insertion.
- **`profile.yaml` splits prose from weights, and `prose_hash` covers only the prose.** Change a
  weight and every cached judgment stands; change the prose and judgments are re-taken.
- **Two absences that must not be "simplified":** an unjudged posting scores `None`, not `0.0`
  (zero buries a model failure at the bottom); an undated one scores mid-scale, not "today" (that
  floats stale reqs to the top). Both have tests.
- **Rankings live in their own table, never in `verdicts`** — `check` rewrites `verdicts` nightly,
  which is what erased the model's work before.
- **`rank` never fails for want of a model**; yesterday's order beats nothing. A large "still
  unranked" count while the model is up usually means the description backfill is draining.

---

## Prefilled applications, the answer bank, and the mirrored form

**Switched off since 2026-09-10** — `config.PREFILL_ENABLED`, default False, env
`JOBTRACKER_PREFILL=1`. Applications are typed by hand. `jobtracker/browser.py`,
`jobtracker/live.py`, `jobtracker/prefill.py`, the answer bank and `/apply` are **mothballed, not
deleted**; `docs/prefill.md` is the full guide and the archive carries the rest of the rules.
What matters while it is off:

- **The hard guard is `browser.fill_application`,** not the UI — a switch enforced only at the
  callers is one the next caller walks past. `unavailable_reason()` reports the switch *before* the
  Playwright import, because "switched off" and "not installed" call for opposite responses.
- **Refusals name the switch first**, before anything else that is missing. A reason printed beside
  a decision you typed points at the wrong thing to fix.
- **`prepare` still rescores and still exits 0.** `prefill` and `apply-to` say so and exit 0 —
  a feature you switched off is the system doing what you said, never an error.
- **The picks render no prefill line at all**, not "prefill: off" and not the absence message.
  `build_dashboard` passes `plans=None`, so both worlds look the same from below.
- **Stored plans, gaps and learned question keys are kept.** Nothing sweeps them; this is a
  decision that reverses.
- **The tests describe the on world**; an autouse fixture in `tests/conftest.py` forces it, so
  mothballed code keeps its coverage. `tests/test_prefill_switch.py` is the off world.
- **The serve image keeps Playwright and tectonic**, and CI sets `JOBTRACKER_PREFILL=1` so its
  capability assertions still mean something. Turning a feature off is not a deployment change.

The rules that still bind whenever this code is touched:

- **The browser never submits except through `_submit`'s one gated click**, and
  `requestSubmit` / `form.submit` / `dispatchEvent` / `keyboard.press` stay banned. A real press of
  the employer's own control runs their validation and captcha hooks, which a programmatic send
  skips. The test scans the source with docstrings stripped.
- **Nothing may put a value in a field the user did not give for it.** A value reaches a field
  because a canonical ATS name matched, or because the user attached this wording to this answer.
  **A dropdown that does not offer our answer is a gap, not a fill.**
- **A handle is only valid for the discovery that minted it**; commands carry an `epoch` and are
  dropped on a mismatch, in the drain, where nothing can bypass it.
- **`live.py` is pure** (no Playwright, no HTTP, no SQLite) and the command vocabulary is seven
  names that point at a field handle — never a selector, never anything evaluated in the browser.
- **Playwright objects belong to the thread that made them.** Writes are queued and drained in the
  hold loop's existing tick.
- **Zero fields discovered is "no application form found", never "0/0 filled, nothing left to do".**
- **Greenhouse gets `embed/job_app?for={slug}&token={id}`** — the form itself, keyless, never
  redirected. 25 of 45 tracked Greenhouse boards redirect `absolute_url` to a JS careers shell.
- **`page.evaluate` only ever sees the main frame**; `_discover` falls back to the frames and
  returns the **surface** the fields came off.
- **`answers.yaml` is gitignored**; `answers.example.yaml` is tracked. Everything above the
  unanswered-questions marker is the user's and is never parsed or rewritten. Writes go through
  `safewrite.py`, and adding an answer is **text surgery, not a YAML round trip** — a round trip
  deletes every comment, including the stubs the user is working through.
- **Uploads are named by us and validated by content** (`resumes.validate_upload`,
  `resumes.stored_name`); `_UPLOAD_ROUTES` is a set, and a file route left out of it reads its body
  as `{}` and reports "no file". **The cap on the body is not the cap on the file** — base64 is
  ~4/3 — and over-length is answered **413 in words**.
- **No endpoint on this server may block on the network** except `/api/company`'s bounded
  verification; `HTTPServer` runs one request at a time.

---

## Tailoring a resume

`jobtracker/tasks/tailor.py`, `jobtracker/resume/`, `jobtracker tailor build`. `docs/tailor.md`.
The first model role that composes prose, so the bound is not the shape of the answer.

- **Your resume's source is LaTeX** (`$JOBTRACKER_RESUME_TEX`). A `.tex` file is already text; a
  PDF extractor's idea of a line is a column-layout accident, and the output is a diff.
  `validate_tex_upload` checks it decodes as **UTF-8** and declares a **`\documentclass`**, and
  names a PDF or DOCX as such *before* either check.
- **Both anchors are verbatim quotes.** `evidence` must occur in the description and
  `current_line` in the resume: one keeps an invented requirement off a resume, the other keeps
  the page from attributing a line to you that you never wrote. Grounding is checked
  whitespace-normalized but `apply_edits` replaces exactly, so **an edit must pass both**.
- **The LaTeX guard is a security control** — a resume is compiled, so a suggestion is a program
  about to be run. An **allowlist** of control sequences, because a blocklist is a guess and
  `\csname` composes command names out of characters. It runs inside parsing, not at assembly.
- **`sanitize(suggestion, context)` widens the allowlist with the commands *that line already
  runs*,** never with the document's. Without that the fixed list refused every bullet rewrite a
  real resume has — a guard that cannot pass the only line shape its input has is not strict, it
  is off, and it fails as silent absence.
- **`NEVER_ALLOWED` is a blocklist inside the allowlist.** For `\input`, `\write`, `\def`,
  `\csname` and friends the **argument is the whole risk**, so they are never picked up from a
  line however the document uses them. Tested by name.
- **The skeleton must come back unchanged** — same commands, same order, same count, same number
  of argument groups; only the prose between them may move. That is where "do not restyle my
  resume" is enforced.
- **`apply_edits` replaces a line it was handed verbatim, and does nothing else.** No search, no
  fuzzy match, no insertion — which puts the preamble out of reach by construction. The cost is
  that it cannot add a bullet.
- **Two keyword lists pull in opposite directions** (`keywords.yaml`). `allowed` goes into the
  prompt verbatim; `denied` is a refusal applied in `parse_edits`. **A prompt is a request, not a
  bound** — the list that widens is prose a model reads, the list that narrows is code it cannot
  argue with. The denied list is in the prompt too, which is what stops it re-proposing a rejected
  word nightly.
  - **Empty `allowed` means unrestricted, never "nothing allowed."** Reading an absence as a
    decision would silently switch tailoring off on install. There is a test named after it.
  - **Both lists are in the unit key** as `keywords_hash`, a second column beside `resume_hash`.
  - **An undecided term holds an edit rather than dropping it** — holding is what makes Include
    cost zero model calls. `keywords.split_edits` is the single derivation, shared by the CLI and
    the endpoint. Render paths call `blocking_terms`, which marks rows and decides nothing.
  - **`blocking_terms` asks `allows`, not `known`.** The edit sitting in the table when you press
    Exclude is by construction the one that made you press it; `known` would let it compile —
    "never write this again, except here".
  - **Held and excluded are labelled differently on every surface** (`describe_blocked`): one is a
    question, the other a decision you made.
  - **A drop for a denied term is still written down** — an empty `Suggestions`, not None, or the
    unit stays pending and re-asks nightly forever. A genuinely empty answer still returns None.
  - **`flagged` is grounded at both ends** like an edit. Never flagged: a term already in the
    resume, or already ruled on either way. `MAX_FLAGGED` is 4.
  - **Settings groups by term, not by posting** — the ruling is about the technology. Finish is
    scoped to proposals that mentioned a flagged term and bounded at `FINISH_MAX`, naming what it
    left; it is the only caller passing `force`, because a PDF built before the ruling is not the
    document the page now describes.
  - **The guard is not a hallucination detector.** `denied` is exact; `allowed` is a prompt, and
    nothing here knows which strings are technologies. What the pair gives you is a growing
    *decided* set. Do not claim more for it.
- **`resume_suggestions` has exactly one reader**, the page that shows it to you. Nothing joins it
  into prefill, ranking or matching. Ask where an answer is stored, not just where it is produced.
- **Assembly is not a task** — it needs no model, so `jobtracker tailor build` always runs.
- **Nothing writes bytes to your resume.** Edits apply to a copy in memory; the result is a new
  file under `$JOBTRACKER_TAILORED`. A test reads the no-write rule off the source.
- **`assemble.py` is the only subprocess in this repo**, with a test keeping it that way. List
  argv, `shell=False`, a scratch directory, a timeout (TeX loops rather than erroring), and
  `--untrusted`. **Exit 0 with no PDF is a failure and is named.**
- **A missing toolchain is not an `unavailable_reason` for the task** — suggestions are text.
  `tailor build` reports it once and exits **0**, and must never make `prepare` exit 2. Tectonic is
  in the serve image only.
- **`dismissed` is reachable and kept** (`jobtracker tailor dismiss`); deleting would let the next
  run re-propose. It reopens when the resume hash moves.
- **`keywords.yaml` is curated**, written by `POST /api/keyword` and your editor, both deliberate
  acts. The writer is line-oriented text surgery — the file is mostly the comments explaining the
  two lists, and `yaml.safe_dump` deletes all of them.
- **Nothing anywhere accepts an edit on a click.** Attaching is `tailor build --attach`, after
  reading the diff at `/apply`. **Building and downloading are not accepting** and are allowed from
  the actions cell and, under `serve`, from a Today card's documents line (`_docs_line`), which
  renders the actions cell's own `↓`/`✉` through `_tailor_control`/`_letter_control` with **no
  `data-act`** — that is what keeps `.pick [data-act]` meaning the three disposition buttons. The
  card's tailor line stays a count with no control, and `/apply` carries no control of any kind,
  which keeps `.lf` selecting exactly what it did.

---

## Cover letters

`jobtracker/letter.py`, `jobtracker/tasks/coverletter.py`, `jobtracker coverletter build`.
`docs/coverletter.md`. The second model role that composes prose and the first that
composes a whole document, so `tailor`'s guard does not transfer — it is replaced.

- **The model never writes LaTeX.** Not under an allowlist: `escape_text` escapes every
  reserved character, **backslash first**, so nothing it returns can be a control
  sequence. Stronger than `resume/latex.py` and it can afford to be — `tailor` must pass
  `\resumeItem{...}` through, nothing here needs a macro to survive. Do not "reuse
  `sanitize`" here; it would be a weakening.
- **`COMPANY`, `JOB TITLE` and `DATE` come from the record**, substituted by `fill`. The
  model is never asked — a letter addressed to the wrong employer is the one mistake a
  cover letter does not survive, and the database already knows.
- **The substitution runs on the template's text only, never on a composed paragraph.**
  `COMPANY` is an ordinary English word, so walking the whole document once after
  splicing — the obvious implementation — lets a letter saying *"we discussed COMPANY at
  length"* be rewritten by a step the model is not part of. Segments, not one pass.
- **A slot is a `%` comment plus prose carrying a CAPS placeholder**, inside
  `\begin{document}`. The comment is the brief, handed to the model verbatim, so **the
  template is the prompt** — there is no prompt file to edit. **Prose with no placeholder
  is not a slot** and comes through byte for byte, which is how fixed sentences stay fixed.
- **`\begin{document}` is found in code, never in a comment.** A template that explains
  its own safety property in a comment would otherwise have that sentence taken as the
  document start, dragging the preamble into slot range. The tracked example did exactly
  this on its first parse; there is a test named after it.
- **`parse_letter` is all-or-nothing**, the opposite of `parse_edits`. `fill` leaves an
  unanswered slot as its placeholder, so a partial letter is a PDF with SHOUTING CAPS in
  it — and writing the row would drain the unit, so it would never be re-asked. Same
  reason a denied term discards the whole letter where in `tailor` it drops one edit and
  is written as an empty proposal: here there is no rest to compile.
- **`evidence` may be grounded in the description *or* the resume** — one document per
  kind of paragraph — and which one is stored as `source`.
- **`cover_letters` has no `resolution`, no dismiss and no attach path.** A suggestion is
  a proposal about a document you wrote, so refusing it is a state worth keeping; a letter
  is a draft for one posting with no meaning at another. Deleting the row is "no thanks".
- **`letter_stem` is the single derivation** and carries a `_letter` suffix — without it a
  build would overwrite the tailored resume for the same posting, both being minted from
  the same `resumes.stored_name` pair.
- **The actions cell's letter control is `✉`, not a second `↓`.** Two identical glyphs on
  one row is a cell you have to hover to read, and they fetch different documents.
  `interactive`-only, absent until a letter exists, and it carries no `data-act`.
- **`_LETTER_BUILDS` is its own dict, not a namespaced key in `_BUILDS`.** Both are keyed
  by posting and a posting can have both compiling at once; shared, the second start reads
  the first's "building" and the first success drops the entry, so a poll reports ready
  about a file that is not there.
- **Priority 60, last.** Re-keyed by two documents you edit rather than one, and a page of
  prose per unit. It consumes nothing `tailor` produces — the order between them is cost.
- **Guard `\pdfgentounicode`.** `\input{glyphtounicode}` is a pdfTeX primitive XeTeX
  lacks, so unguarded it kills *every* tectonic build with no PDF. `\ifdefined` both
  lines; text still extracts, so ATS parsability is not lost. Same fix `data/resume.tex`
  carries.
- **The guard is not a fact checker.** Grounding proves a paragraph can quote a real
  sentence, never that its claims follow from it. `denied` is the only bound; the rest is
  a prompt. Do not claim more for it.

---

## Slug repair

`jobtracker repair`, `docs/repair.md`. The only bounded model role where the model is a
*fallback*: deterministic regexes read the careers page first, and the model is asked only about
pages they could not parse.

- **The trigger set is narrower than `is_degraded()` on one axis and wider on another.**
  `IDENTITY_DRIFT` fires immediately. `FETCH_FAILED` needs `REPAIR_FAILURE_THRESHOLD` = 2
  consecutive *nights* — `fetch.py` already burned `MAX_RETRIES` inside the run, and rewriting a
  hand-verified slug because a CDN hiccuped is the expensive mistake. Alerting `SUSPECT_EMPTY` is
  included because a dead board never presents as `FETCH_FAILED`.
- **Non-alerting `SUSPECT_EMPTY` never triggers**, or the permanently-empty boards get "repaired"
  nightly. A test is named after them.
- **`manual` and `aggregator` companies are never targets.** A careers page is a scrape.
- **Nothing is proposed on the strength of a string.** Every candidate — regex or model — is
  fetched through the real `Source` adapter and rejected if the board is empty (`hubspot`) or its
  identity does not match (`cedar`). Both have regression tests; do not weaken them.
- **`no_identity` is checked before `wrong_company`.** `health.identity_matches` returns True when
  either side is empty — right for the nightly loop, catastrophic here, where it reads "the
  identity endpoint 500'd" as "verified".
- **A model slug must appear on the page it was shown**, after a `/` or `=`, case-sensitively. A
  bare substring test grounds every invented slug, since the company name is all over its own page.
- **`repair` never writes without `--write`,** and `--write` moves `expected_board_name` along with
  the slug, or the repaired board drifts on the very next run.
- **Known blind spot, do not "fix" it in the regexes:** a careers page that renders its board link
  in JavaScript often contains no identifier at all. Those report `no_candidates` and stay visible.

---

## Adding a company

`jobtracker add-company` and `/companies` under `serve`. `docs/companies.md`. Both doors share
`jobtracker/curation.py` — one appender and one validator, because two implementations of "append
a curated entry" is how the button and the terminal disagree about the file they both own.

- **`companies.yaml` has five writers, and every one is something you did on purpose**: `migrate`,
  `add-company`, `verify-slugs --write`, `repair --write`, `POST /api/company`. **No scheduled run
  writes it** — the invariant DESIGN.md §2.3 protects. The same standing covers `criteria.yaml`
  via `/tuning`, `keywords.yaml` and `plugins.yaml` via `/settings`: `serve` is a process you
  started and a click is something you did.
- **The click is allowed here and still refused on a repair proposal.** Adding appends an entry
  that did not exist, from values you typed, and renders the exact diff it applied. Applying a
  repair rewrites a hand-verified slug on the machine's say-so, where the diff has to come *before*
  the write. `dashboard._proposal_cell` has no apply button and must not grow one.
- **The writer is line-oriented, not a YAML round-trip.** PyYAML re-folds long strings to its own
  width, so a round-trip to change one `slug:` re-wraps `notes:` prose on unrelated entries — and
  the deliverable is a diff somebody reads. Appending is a **third** operation: `_edit_entry`
  cannot create a `- name:` block, so `insert_entry` renders it alone and splices it in, with a
  test asserting every pre-existing line survives byte-for-byte.
- **Placement is "before the first entry that sorts after me"**, not "after the last entry with my
  tier" — the second has no last entry for a tier the file does not use yet. Untiered entries sort
  last.
- **`has_inline_comments` deliberately does not guard an append** — it exists for a round trip that
  discards comments, and an append rewrites no line.
- **`validate_new` is stricter than `load_companies`**, because the loader must keep loading
  whatever is on disk while a new entry gets the strict pass. But **stricter than the live file is
  a rule that gets deleted the first time it fires**: a `slug` on a `manual` entry is documentation,
  and an aggregator with no `board_url` is parked on purpose.
- **A typed slug is labelled `reachable`, never `provenance`.** `judge_board` takes the
  weak-evidence label as a *parameter* because its two callers claim different things. Nothing
  served a slug you typed.
- **"Not verified" never renders as "verified".** A skipped check writes
  `expected_board_name: null` and says so; writing the typed name would either drift-alert on a
  name nobody checked or silently pass. A verified save seeds it from **the name the ATS returned**.
- **`/api/company` is the one endpoint that opens a socket, and it is bounded**
  (`Fetcher(max_workers=1, timeout=8, max_retries=1)`, at most two requests, `min_interval` **not**
  overridden). It cannot go on a thread — it decides whether the write happens. A second
  verification is **refused, not queued**. A test pins the bound; widen it there first.
- **`ok` and `saved` are two axes.** A refused *board* is `ok:true, saved:false` with the evidence
  attached, because every `_JS` handler here opens `if (!res.ok) alert()`. `ok:false` is for a
  refused *request*.
- **The file is re-read after verification, not before** — a `repair --write` landing in that
  window would be clobbered by a splice computed against stale text.
- **Both buttons are rendered server-side**, `co-force` merely `hidden` until the script reveals
  it. A button the JS mints is one nothing checks has a handler.

---

## Paged boards

`jobtracker/sources/workday.py`. The first source whose board does not arrive in one call, which
is why `Source` grew `page_size`, `jobs_page_url`, `jobs_body` and `jobs_page_error`, and
`fetch.py` grew `_fetch_paged`. The hooks are on the base class because these are properties of
paged boards, not of Workday.

**Every trap below returns something that looks like an empty board.** None errors, none 404s, and
`sync_postings` would read each as "every posting closed". Each has a test named after it.

- **A page over the vendor's cap is not an empty page.** Workday caps at 20: `limit: 50` returns
  HTTP 200, valid JSON, and **no `jobPostings` key at all**. Both parse to zero rows under the
  obvious `.get(..., [])`, which is what `jobs_page_error` refuses. **Zero rows on a well-formed
  page is still allowed through** — a genuinely empty board belongs to `health.py`.
- **`total` is only populated on page one** (0 on every non-zero offset). A loop bounded by `total`
  keeps 20 of 2,000 reqs, silently.
- **An offset past the end wraps to the beginning**, with `total` repopulated — no short page and
  no error, ever. So `_fetch_paged` has **two** stopping rules: a short page, and *a page that adds
  no posting id we do not already hold*. Ids are the check because they identify a posting.
- **`postedOn` is relative prose** resolved against `today`. `"30+ Days Ago"` is floored at exactly
  30 — a bound rather than a date.
- **A Workday slug is a triple**, `tenant/dc/site`; the data centre has no relationship to the
  tenant name. `curation.validate_new` checks the shape via the adapter's own `parse_slug`, so the
  validator and the fetcher cannot disagree.
- **`parse_jobs` cannot build a Workday URL** — the row carries a site-relative `externalPath` and
  the payload names no host. `Source.posting_url` fills it in from `fetch_company`, guarded on
  emptiness so it is a no-op for every other adapter.
- **`externalPath` does not follow a rename**, so the report will show paths disagreeing with
  titles. That is Workday's data, and it is also why the path is the right `ats_job_id`: stable
  across renames that would otherwise churn a posting as closed-and-new.
- **`appliedFacets` stays empty.** Server-side filtering would be a role or location gate applied
  before any title is read.
- **Workday can never claim identity** — nothing in either payload names the employer.
  `identity_from_jobs` returns `None` on purpose and `expected_board_name` stays null.
- **The cost is latency and it will not tune away**: ~2.4s per 20 rows, so a 2,000-req board is
  ~4 minutes. A nightly run that was ~29s is now several minutes.

---

## Aggregator sources

`jobtracker/sources/aggregator.py`. Community new-grad list repos are the highest-yield source for
new-grad roles specifically, including companies not on our list. One `check_method: aggregator`
entry with a `board_url` = one feed; the adapter parses the README's HTML `<table>` and it flows
through the same health/`sync_postings`/`match` loop as any board.

- **The feed is the `company`, the employer is in the title** (`"Employer — Role"`). One stable
  diff namespace per feed, and the employer stays visible with no schema change. Caveat: an
  employer name containing a title-shaped exclude token is conservatively rejected.
- **`ats_job_id` is the Simplify `/p/<uuid>` when present, else a hash of employer+role** — stable
  across runs so `sync_postings` recognizes the same row.
- **Closed rows (`🔒`) are skipped** — a filled req is not an opening.
- **A missing `board_url` skips the feed** rather than failing the run.
- **The Ouckah/CVrve feed is unwired** — its 2026/2027 repo URL is unconfirmed, so its entry has
  no `board_url` and is skipped at no cost. Simplify is wired.
- **Parsing tolerates garbage** (`[]` on any unexpected shape) — these repos restyle the table
  every cycle; an empty feed is a visible SUSPECT_EMPTY, never a crash.

---

## Deployment

`docs/deployment.md`. **This repo does not know about orchestrators** — no Kubernetes manifests, no
systemd units in-tree. The deliverable is a container plus a documented contract.

- **CD publishes; it does not deploy.** Green `main` pushes `ghcr.io/ida314/job-tracker:{sha-…,
  latest}`, arm64 only. **The host pulls**; nothing in CI holds credentials to a machine.
- **There are two images.** `Dockerfile.serve` builds `job-tracker-serve` from the Playwright base;
  the batch image is 177MB against ~1.9GB, and folding them would multiply the nightly pull tenfold
  for browsers `check`, `work` and `prepare` never open. The bases differ (trixie does not support
  `playwright install --with-deps`).
- **The base tag and the pinned `playwright` version are one unit** — bump one without the other
  and the driver looks for a browser that is not there.
- **`JOBTRACKER_BROWSER_PROFILE` and `JOBTRACKER_RESUMES` must point into `/data`**, or the
  persistent Chromium profile and every uploaded resume are written into a deleted layer.
- **CI asserts capabilities on the published image, not imports** —
  `browser.unavailable_reason() is None`, `latex.unavailable_reason() is None`, `import
  sir_client`. `serve` reports a missing browser as a message on a card and carries on, so an image
  that lost one looks exactly like a working deployment. The PR-time `docker` job repeats them on
  the image it just built.
- **The test job runs without `sir-client`** (not on PyPI), so no test may assume the SDK imports.
  Force the world you mean with the `sdk_installed` fixture.
- **`needs:` skips silently** — a job that never ran reads as ordinary gating. Check the registry,
  not the workflow file, when asking whether CD works.
- **A credential must never be a build arg** (`docker history`). The Dockerfile takes an optional
  BuildKit secret and wipes `/root/.gitconfig` in the same layer. `python:*-slim` carries no git.
- **Every run names its own build**: `jobtracker 0.1.0+<sha12>` from `JOBTRACKER_REVISION`, logged
  by `main()` and set as `service.version`. A bare `0.1.0` means not running from a published image.
  A host that failed to pull is otherwise indistinguishable from one that succeeded.
- **Rollback is by tag.** Forward updates are safe unattended: `store.py` does
  `CREATE TABLE IF NOT EXISTS` plus additive column migrations on connect.
- **`serve` is the one long-running process**, so it carries `/healthz` (a constant, never touches
  the DB), `/readyz` (503 until `state.db` opens *and* `criteria.yaml` parses, payload names which
  failed), and it drains the in-flight request and exits 0 on SIGTERM/SIGINT.

---

## Repo conventions

- Each change is its own commit, grouped by *failure class*, so one class of mistake can be
  reverted without losing the rest.
- The commit before any correction is the `pre-audit baseline`, a revert target.
- **No web framework in `server.py`**, and no dependency for something hand-rollable in a few
  lines — the JSON log formatter and the base64 upload path follow the same rule.
