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
| `ats` | `greenhouse`, `lever`, `ashby`, `workday`, `gem`, `bespoke`, `aggregator`, `unknown`. `aggregator` now only ever pairs with `check_method: manual` — a job board nothing may read automatically (Wellfound). |
| `slug` | Board identifier. A Workday slug is the *triple* `tenant/dc/site` (`redhat/wd5/jobs`) — the data centre is part of the hostname and is not derivable from the tenant. Empty for `bespoke`. |
| `board_url` | Full JSON API URL for `api`. On a `manual` entry it is documentation — nothing fetches it. A listings feed that *is* fetched is a job board plugin and carries its own URL in `plugins.yaml`. |
| `careers_page` | Human-facing careers URL. The fallback when the API breaks. |
| `category` | Free-text bucket, e.g. `data-infra`, `fintech-backend`. |
| `check_method` | `api` or `manual`. Governs what the agent may do. **`aggregator` was retired 2026-09-15**: community listings are job boards now. `curation.validate_new` refuses the value and `/companies` no longer offers it, while `load_companies` still loads a leftover entry and names it at WARNING — the loader must keep loading whatever is on disk. |
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

- **Never scrape a company whose portal has no public JSON board.** Absolute, with one
  scoped exception below.
- **The exception, taken by the user on 2026-09-15: YC's jobs board** (`plugins/ycombinator.py`).
  YC publishes no JSON board, but its listing pages are server-rendered with the whole job list
  in one `data-page` attribute — Inertia's payload, parsed as *data*. The scope is what keeps it
  an exception: a fixed set of listing paths capped at six, no link followed off the page, no DOM
  walk (a test reads that off the source), and `robots.txt` allows `/jobs`. Do not widen it into
  a crawl, and do not cite it for any other portal.
- **`manual` is a finding, not a category.** It records that nobody found an endpoint. The right
  response to "is there one?" is to look — at the network the page makes, not its DOM. Finding
  one is a reclassification. A found endpoint is *verified before it is written*, same as a slug.
- **A JSON board is not automatically usable.** Amazon's is keyless and still unreadable: offset
  paging is hard-capped and a run takes ~9 minutes before failing. A board that ends
  `FETCH_FAILED` every night is not coverage.

Recon shortcuts worth not repeating: Snowflake is Phenom People (`/api/apply/v2/jobs`, tenant
param unknown; `ats: unknown` is known to be wrong). Retool's Gem endpoint answers **403**, so
it is real and gated. Epic (Avature) is login-walled, and so is *applying* through YC — its listing reads fine, every
`applyUrl` is one shared login link; Coralogix's
Comeet token is minted client-side. SAP's board is server-rendered HTML. **Wellfound is refused
and stays refused**: a plain GET returns Cloudflare's challenge script, `/graphql` answers 403,
robots disallows the `?role=`/`?jobId=` forms, and page two needs a login — it is a check-by-hand
job board, and working around bot protection is not on the table. For Workday guesses, a
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

`jobtracker dashboard` renders `state.db` to a single self-contained HTML file. Five tabs;
**Today is the landing screen**, Applications second — work already committed to outranks the
raw corpus.

**Postings live on two surfaces, split by `postings.origin`.** No origin is a curated company
board (Company pages); an origin is a job board (Job boards). The two **partition** the corpus —
a row on both, or on neither, is the failure worth catching, and neither looks like anything from
outside. Under `serve` the job boards are their own page at `/jobboards`, in `_NAV`, and the
dashboard drops that tab (`include_job_boards=False`); the static file keeps both, having nowhere
else to put them. One renderer serves both, because two copies of a list are how they come to
disagree about what is on it.

- **It is a pure read.** Unlike `report`, it never marks manual companies as surfaced.
- **No network at view time, ever.** No CDN, no chart library — the one chart is CSS. That is
  what makes the file mailable and openable offline.
- **Rows render server-side; JS only hides them.** Panels are server-rendered and the script only
  toggles `[hidden]`; `.tabs` is `display:none` until JS confirms it is running. Don't "improve"
  this into client-side rendering from embedded JSON.
- **Escape everything.** Titles and locations come from third-party APIs; URLs get a scheme check
  too, or a `javascript:` href executes on click.
- **Location sorts, never filters.** The dropdown defaults to "Anywhere".
- **A filter bar drives the panel that renders it**, found through `[data-filter-scope]` and
  `data-f="q|ats|loc"` — never by a global id, or two postings panels in one file would leave one
  bar silently driving the other's tables. A chip names the row attribute it reads
  (`data-attr`/`data-val`), which is what lets one JS block serve tier chips and source chips.
- **The job boards table is flat, and that is deliberate.** Grouping by company is right where the
  group is the employer; here it would be the feed — one heading over thousands of rows from
  hundreds of employers. Employer is its own column, the board is a `srctag` beside the role, and
  the filter bar offers search, which board, and the location sort. No tier chips and no ATS
  select: a control that filters on a field every row leaves empty only ever hides everything.
- **A duplicate is shown, never hidden.** A job board row sharing a `dedupe_key` with an open
  company-page row carries an "on company page" chip linking to it; a row whose key matches an
  application renders "applied · \<status\>" where `+ tracker` would be. A live-looking control
  there would record a second application to one req.
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
- **What you *sent* is a separate table** (`application_submissions`), frozen on the write that
  creates the application and read back on the posting page. See *The posting page* below; the
  card's title links there, with a small `↗` keeping the employer one click away.

---

## The posting page

`/posting?company=&job=` under `serve`, `jobtracker/questions.py`, `jobtracker/submissions.py`.
`docs/posting.md`. Where you go *before* the employer's form: the posting, the two documents,
the questions it asks beyond them, and — after applying — what you actually sent.

- **Every posting title under `serve` links here; the static file keeps its external links.**
  `dashboard._posting_href` / `_posting_attrs` are the one place that decision is made, and
  the attrs helper carries `target`/`rel` with the href — the internal link must **not** open
  a new tab, the external one must keep `rel="noopener"`. The pick's button reads `Prepare →`
  under `serve` and `Apply` in the file. It stays an anchor with **no `data-act`**.
- **`/posting` is not in `_NAV`; the page still renders `_NAV`.** A destination you reach from
  a posting is not a tab, and a detail page with no way back is where that would be felt.
- **It never joins `postings`.** A manual application has no posting row, and it is the one you
  most want a record of. Read `applications` first, `store.posting_detail` second, and 404 only
  when both are empty.
- **The question template seeds; it does not own.** `questions.merge` renders
  `question_template` beside `posting_answers` **without writing** — a GET that materialized
  rows would turn opening a page into a claim you had answered something. Saving one copies it
  to this posting, with **the wording as rendered**: re-deriving it would let a later template
  edit rewrite an answer you already gave.
- **`qid` is minted once and never re-derived.** The opposite of `manual_job_id`, whose
  determinism is the feature. Minting once is what lets you fix a question's wording without
  orphaning its answers.
- **A template default you never saved, and a saved answer with no text, are not recorded as
  submitted** (`questions.answered`). The first is "nothing may put a value in a field the user
  did not give for it"; the second would read afterwards as "I left this blank".
- **The page and the freeze ask the same function which document is in effect**
  (`submissions.effective_resume` / `effective_letter`). Resume: upload → the bank's. Letter:
  upload → the written one **while `letter.is_current`** → the bank's. **The tailored PDF is
  not a term** — `tailor build --attach` writes it into `posting_resumes`, and that is the only
  way it becomes this posting's resume. No attach control on this page, ever.
- **An uploaded letter goes in `RESUMES_DIR`** (`resumes.letter_upload_name`), never
  `LETTERS_DIR`: that directory is rebuildable generated state, `letter_path` already owns the
  `.pdf` in it, and `Dockerfile.serve` does not set `JOBTRACKER_LETTERS`.
- **The archive stores bytes, not names.** `tailor build` rewrites a posting's PDF at a
  deterministic path, so a name resolves forever while coming to mean a document you never
  sent. `JOBTRACKER_SUBMISSIONS` is the one directory here nothing can rebuild — it **must**
  point into `/data`, same rule as `JOBTRACKER_RESUMES`.
- **The freeze rides the `applied` write; it is not a new write path.** Three callers, each
  conditioned on the application not existing *before* the write — `_api_disposition`,
  `_api_application`, `cli.cmd_apply`. Mail accept never freezes: it can only move a row that
  exists.
- **Write-once**, and the row is claimed before anything is copied, so two racing writers
  cannot both decide they are first. **"Update what I submitted" is the only `replace=True`
  caller**, and only while the application is still at `applied` — once someone replied, what
  you sent is history.
- **A failed copy is a log and a gap, never an exception.** This runs inside the click that
  records an application; a refused "I applied" is work you have to redo.
- **`/api/posting-letter` is in `_UPLOAD_ROUTES`, `/api/posting-letter/clear` is not** — the
  clear route carries two strings and would inherit a multi-megabyte body cap. Both halves are
  tested.
- **`GET /api/document` serves all four documents through one containment check**
  (`_send_posting_file`). `app-save`/`app-meta` select `.app, .aprow` so `/applications` and
  this page cannot log a stage differently.
- **The log records counts, never content** — `answers=2`, not the answers.
- **`purge` keeps `posting_answers` and `application_submissions`**; `posting_letters` mirrors
  `posting_resumes` and is named by `purge_blockers`.

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

## Job boards (import plugins)

`jobtracker plugins`, `jobtracker/plugins/`, a card on `/settings`, one extra loop in
`cmd_check`, and the `/jobboards` page. `docs/plugins.md`. One module plus one import line;
module pure, `runner.py` owns the socket.

**Two shapes of board, and the difference is what the runner branches on.** A *cursor walk*
(Discord) reads an endless stream forward and remembers where it stopped. A *snapshot* board
(Simplify, YC) republishes its whole listing every run: `page_urls` returns a fixed set of pages,
there is no cursor, and `collect` takes the snapshot path.

- **A failed snapshot page ends the read and drops what it had collected** (`_nothing`). Half a
  listing is not a smaller listing — it is the shape a board that emptied would present. The
  cursor walk keeps its partial pages instead, because each is a complete statement about its own
  window and the cursor simply does not advance.
- **Snapshot pages overlap by construction** (a role listing and a location listing share jobs),
  so ids are de-duplicated across them — otherwise one req is two rows inside a single board.
- **`closed_ids` is the honest half of closing.** A board that publishes `active: false` is
  *telling* us; `store.close_feed_postings` closes those, chunked because a listing carrying its
  whole history names far more ids than this tracker holds. Absence still closes nothing.
- **`health.evaluate_plugin` reads an empty snapshot as SUSPECT_EMPTY on every run**, not just the
  first. The distinction that module protects is between a poll and a statement, never between a
  plugin and a board.
- **Every row records `origin` and wears a `tag`.** `origin` is the plugin name and is what splits
  the two pages; `tag` is the short label the row shows, because one list holds every board's rows
  and each has to say which board that was. `employer` is its own column too — titles keep the
  `"Employer — Role"` shape the criteria tokens and the eval corpus read.

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
- **`level`, `judge` and `inbox` default to on**; every import plugin defaults to off, including
  `simplify`, which replaced a feed that *was* running nightly. Installing a board never starts
  reading anything: an upgrade that quietly began fetching 13MB from a new host is exactly what
  that rule is for. Switching one on is `jobtracker plugins enable <name>` or the Settings card.
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

`jobtracker/dedupe.py`, three columns on `postings`, one column on `applications`, two calls in
`cmd_check`. `docs/dedupe.md`. Runs across **every** source, not just plugins.

**It flags; it does not close.** Postings live on two pages, and the same req legitimately appears
on both — once from the employer's own board, once from a job board pointing at it. Closing either
deletes half of what the job boards page is for. `close_duplicates` and `dedupe.preferred` are
gone, `append_postings` no longer refuses an announcement of a job already tracked, and rows
closed by the old rule are reopened once on connect **with the count logged** — `sync_postings`
reopens only closures that came from absence, so nothing else ever would.

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
- **The question is answered where it is read.** `open_postings_by_verdict` and `ranked_matches`
  resolve `applied_status` through the row's own application **or** any application sharing its
  key, as a correlated subquery — two applications can share a key, and a join would multiply the
  posting row. `rank.one_row_per_req` then collapses the picks to one row per key, with the
  company-board row winning even when the feed row scores higher: it is the one carrying health,
  identity, a description and a form.
- **What survives of precedence is the finding, not the closure.** Two *board* rows on one key
  means the key cannot tell two live reqs apart; `store.board_key_conflicts` reports it and
  touches nothing. `_is_feed` still asks "redundant by construction?" rather than "is this api?",
  because an uncurated company is not a feed and reading it as one would hide a real collision.
  Since nothing is closed anywhere, that WARNING is the only way such a key becomes visible.
- **The index cannot live in `_SCHEMA`.** `connect()` runs `executescript(_SCHEMA)` *before*
  `_apply_column_migrations`, so an index on `postings(dedupe_key)` raises `no such column` on any
  pre-existing database — and passes on every freshly built one, which is every database in the
  test suite. `_ADDED_INDEXES` is applied after the columns.
- **`sync_postings` must not reopen a dedupe closure.** The reopen is conditional on
  `closed_reason IS NULL`, i.e. on the closure having come from absence.
- **`dedupe_key` is a plain assignment, not COALESCE** — the key is derived from the same
  statement's URL, and a URL that moves must take its key with it.
- **`applications.dedupe_key` follows its URL exactly**, including being cleared: absent/None
  leaves it, `""` clears it, and a URL yielding no key stores `''` rather than leaving yesterday's
  key on today's link. Every reader excludes `''`, or one empty key would match every other.
- **`closed_reason` still exists and still matters** — `'aged_out'` and `'feed_inactive'` are
  closures absence did not cause, and they are what stops `sync_postings` reopening them nightly.
  `'duplicate'` is no longer written by anything.
- **Blind spot, documented not hidden:** a `simplify.jobs/p/<uuid>` row and its direct twin do not
  dedupe. A test asserts the miss. Workday is deliberately out of the URL extractor and covered by
  `key_from_identity`.
- **A migration that changes what the pages show says how many in the run log**, or a legitimate
  one-time cleanup reads as a regression at 2am. That is why `store.py` has a logger at all.

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
  reading the diff at `/apply`. **`--attach` writes the PDF into `RESUMES_DIR` as well as
  `TAILORED_DIR`** — `posting_resumes.filename` is resolved by every reader through
  `resumes.path_for`, so recording the tailored file's bare name left an override nothing could
  open while the pick card printed its name off the row. **Building and downloading are not accepting** and are allowed from
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
- **`tex✉` copies the source the `✉` compiles** — `GET /api/coverletter-tex`, a pure read
  through `server._letter_source`, the one derivation `/api/coverletter-build` also calls.
  It needs no TeX engine. The envelope is on the label for the same reason it is on the
  download: the resume's `tex` sits two controls to its left in the same cell, and a bare
  `tex` on each is the ambiguity that rule exists to prevent. One delegated handler serves
  both copies, choosing the endpoint off the class and restoring the label it read from
  the button rather than a hardcoded string.
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

## The job boards themselves

Three, and each is a different answer to "can this be read at all". `plugins/simplify.py`,
`plugins/ycombinator.py`, and a `companies.yaml` entry for Wellfound.

- **Simplify reads `listings.json`, never the README.** The repo publishes both; the JSON names
  every field the table parser used to reconstruct by regex, links straight at the employer's own
  application page — which is what lets a row meet its board's row under one key — and carries
  ~3,000 active listings against the table's ~460. The README is restyled every hiring cycle.
- **Its group name is `Simplify New-Grad-Positions`, byte-identical to the old entry.** `postings`
  is keyed by `(company, ats_job_id)` and every verdict, override, decision and application ever
  recorded against this feed hangs off that pair.
- **`active` and `is_visible` both mean "no longer published"**, and both go to `closed_ids`.
- **YC's `applyUrl` is the same login URL on every job** — store it and the whole board collapses
  onto one dedupe key, one pick, every row claiming to be the same req. The job's own detail path
  is unique and is what `url` holds; no special extractor is needed.
- **YC's `minExperience` ("Any (new grads ok)" against "6+ years") goes in the description as
  prose**, never a filter. Deciding a level is the `level` task's job, and a gate here would apply
  before any title was read.
- **Everything `page_error` catches answers 200 and parses to zero rows** — a login wall, a
  redirect to marketing, a truncated body, a payload rename. Zero rows on a *well-formed* page is
  allowed through: that is health's question, not the parser's.
- **Ouckah/CVrve is gone**, not parked: its 2026/2027 URL was never confirmed, and an entry that
  fetches nothing is better deleted than carried.

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
