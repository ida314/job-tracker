# CLAUDE.md

Working notes for any agent operating on this repo. The one artifact here is
`backend-newgrad-2027-tracker.md`, a machine-parseable target list of companies to
check for backend new-grad openings.

**Keep this file current.** Whenever we change how the tracker works — the schema, the
check rules, the validation state — update the relevant section here in the same commit
as the change.

---

## Goals

- **Backend new-grad, 2027 graduation.** Backend is the primary target: distributed
  systems, infrastructure, platform, data engineering, SRE. Not frontend, not ML-first.
- **Skill and career growth over pay or prestige.** A company that gives real
  distributed-systems work early beats a bigger name with a narrower first role. Tier 1
  (backend scale-ups) and Tier 2 (infra/devtools, where backend *is* the product) are the
  anchor. Tier 4 (Big Tech) is applied to but not anchored on.
- **Location ranks, it does not disqualify** (set 2026-07-22). NYC is preferred above
  all, then anywhere else in the US, then unspecified, then outside the US. Nothing is
  rejected for where it is. Until 2026-07-22 `locations_exclude` was a hard gate that
  fired *before* the level gate, so 390 postings — London, Dublin, Bengaluru, Toronto,
  Singapore — were discarded without their titles ever being read. Don't reintroduce a
  geography gate; `match.location_rank()` is the mechanism, and it only sorts.

Full include/exclude rules live in the `Match Criteria` YAML block in the tracker. That
block is the source of truth for role matching — don't duplicate it here.

---

## Field schema

Every company is an `### Name` heading followed by a flat `- key: value` list. Keep the
field order below; the parser and every `awk`/`grep` sweep in this repo depends on it.

| Field | Meaning |
|---|---|
| `ats` | `greenhouse`, `lever`, `ashby`, `workday`, `gem`, `bespoke`, `aggregator`, or `unknown` (Snowflake — portal type never confirmed) |
| `slug` | The board identifier within that ATS. Empty for `bespoke`/`workday`. |
| `board_url` | Full JSON API URL for `api`; the raw README URL for `aggregator`. Loaded into `Company.board_url`. Empty for `manual`. |
| `careers_page` | Human-facing careers URL. The fallback when the API breaks. |
| `category` | Free-text bucket, e.g. `data-infra`, `fintech-backend`. |
| `check_method` | `api`, `manual`, or `aggregator`. Governs what the agent may do. |
| `status` | `not-open` / `open`. |
| `last_checked` | Date of the last check. Set on every run, including no-match runs. |
| `last_posting_seen` | Appended title + date when a matching role is found. |
| `notes` | Free text. Record *why* a field is what it is, especially after a fix. |

API URL shapes:

```
greenhouse  https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
lever       https://api.lever.co/v0/postings/{slug}?mode=json
ashby       https://api.ashbyhq.com/posting-api/job-board/{slug}
```

---

## Rule: slugs are verified, never guessed

A slug is only correct once a live fetch has confirmed it belongs to the right company.
Guessing from the company name is how this tracker silently breaks. Two failure modes,
both observed here on 2026-07-09:

**A 200 with jobs does not mean the slug is right.** `ashby/cedar` returns live postings
belonging to an unrelated mortgage/real-estate Cedar, not Cedar the NYC healthtech
(which is `greenhouse/careportalinc`, its legal entity). Confirm identity, not just
reachability:

- Greenhouse: `GET https://boards-api.greenhouse.io/v1/boards/{slug}` returns `.name`.
- Ashby: check `.jobs[0].jobUrl` and read a few job titles.

**A 200 with zero jobs does not mean "no openings."** `greenhouse/hubspot` is a real
board named "HubSpot Product" that always returns an empty array; the live board is
`hubspotjobs`. Mercury and Vercel left behind stale, empty Ashby boards after migrating
to Greenhouse. An empty board must be investigated and reported as `ATS-check-failed`,
never recorded as "no matches."

When a slug fails, read the real one off the careers page — the Greenhouse embed
(`job_board?for=X`), or a link to `job-boards.greenhouse.io/X`, `jobs.lever.co/X`,
`jobs.ashbyhq.com/X`. Then update `ats`, `slug`, `board_url`, and `notes` together.

**That procedure is now executable as `jobtracker repair`** (see "Slug repair" below and
`docs/repair.md`). It does the same reading and the same verifying, and it stops where
this rule says to stop: it proposes a diff, and a human applies it. Do it by hand when
the page is a JavaScript shell — that is the documented blind spot, not a bug to fix in
the regexes.

Do **not** verify an Ashby slug by fetching `jobs.ashbyhq.com/{slug}` and checking the
status code — that host returns a 200 SPA shell for any string. Use the posting-api, or
the `ApiJobBoardWithTeams` GraphQL operation.

---

## Rule: `manual` companies are never scraped

If `check_method: manual`, do not fetch, scrape, or attempt to reverse-engineer a portal.
These companies (Workday, bespoke portals, Gem, IBM Careers) have no keyless public JSON
board. The agent adds them to a "check these by hand" list in the daily report, at most
once per week per company, rate-limited via `last_checked`.

The point is honesty about coverage: surfacing a company for manual review is correct,
pretending to have checked it is not. Never let a `manual` entry silently report zero.

---

## Validation state

**Audited 2026-07-09.** Every `check_method: api` entry was fetched and its board identity
confirmed against the company name. Current composition of the 99 entries:

| | Count | Status |
|---|---|---|
| `api` | 62 | All return valid JSON. 47 Greenhouse, 13 Ashby, 2 Lever. |
| `manual` | 35 | Never scraped, by rule. Flagged weekly. |
| `aggregator` | 2 | GitHub new-grad lists, diffed daily. |

**Red Hat added 2026-08-13** at tier 2, `manual`. Workday tenant `redhat.wd5` / site `jobs`,
read off `redhat.com/en/jobs`; `greenhouse|lever|ashby/redhat` all 404, so there is no keyless
JSON board and it may not be promoted to `api`. It is IBM-owned but, unlike HashiCorp, has not
migrated to IBM Careers. Filed at tier 2 on the growth axis, not product category — RHEL,
OpenShift, Ansible and Ceph mean backend *is* the engineering — with the caveat that much of
that engineering is in Brno and Pune and the US footprint is Raleigh and Boston, no NYC.

**Tier 6 added 2026-07-20** — seven Chinese frontier AI labs, all `manual`. None has a
keyless JSON board; MiniMax, Zhipu, and Moonshot run Feishu/Lark boards that 405 on
unauthenticated reads. Added as research-interest targets at the user's request — only
ByteDance Seed and Alibaba have confirmed US hiring. Do not "promote" any of them to `api`.

(Their original caveat was that they failed the US-only location filter. That filter no
longer exists — location ranks rather than gates as of 2026-07-22 — so these rank last
rather than being excluded. They stay tier 6 on hiring-pipeline grounds, not geography.)

**Known name collision:** `ashby/moonshot-ai` is an unrelated NYC startup, not 月之暗面.
Live jobs, wrong company — the `ashby/cedar` failure mode again.

**Observability vendors added 2026-07-22** — five entries at the user's request. SolarWinds
(`greenhouse/solarwinds`, 95 reqs), Sumo Logic (`greenhouse/sumologic`, 16 reqs) and Five9
(`greenhouse/five9`, 160 reqs) were confirmed by board identity before being written.
Coralogix and SAP are `manual`: Coralogix runs **Comeet**, whose careers-api needs a company
uid plus a page-embedded token, and SAP runs its own SuccessFactors site (`jobs.sap.com`) —
neither is a keyless JSON board, so neither may be promoted to `api`. Datadog (tier 1) and
Elastic (tier 2) were already present and were not duplicated.

**Tiers rank growth opportunity, not product category.** SolarWinds and Sumo Logic were
first filed at tier 2 because observability is their product — that was the wrong axis and
they were moved to **tier 5** the same day. Both are PE-owned take-privates (Turn/River and
Francisco Partners respectively), which structurally means cost discipline over growth
investment, illiquid or immaterial equity, and thin new-grad hiring; SolarWinds adds heavy
engineering offshoring and a legacy, maintenance-shaped product surface. Sumo Logic's 16-req
board next to SolarWinds' 95 and Five9's 160 is a hiring-appetite signal, not a bad slug —
do not flag either as `SUSPECT_EMPTY`, and do not re-promote them on product category alone.

Tier 5 now ends with the enterprise/PE cluster in ascending risk: Five9 → SAP → SolarWinds →
Sumo Logic. **Coralogix stays at tier 2 but is the least certain entry there** — the
technology is genuinely observability-infra, but engineering is centered in Israel with a
small US footprint, which limits both the backend work open to a US new grad and the
mentorship attached to it. Demote it if no US role materializes.

**Tier 7 added 2026-07-22** — four data-versioning / lakehouse companies, isolated from the
new-grad pipeline the same way Tier 6 is. The tech is genuinely distributed-systems-shaped
but none runs a new-grad program, so a near-empty result here is the expected state:
Dremio has 6 reqs (1 engineering), LanceDB's postings are 100% `Senior` and match zero roles
against `exclude_titles`, Onehouse puts backend/infra in Bangalore and only senior/staff in
the US, and lakeFS is Tel Aviv with no US roles. Do not flag these as `SUSPECT_EMPTY`.

Three traps recorded while adding them:

- **Lever slugs are case-sensitive.** Onehouse is `lever/Onehouse`; `lever/onehouse` 404s.
- **`lakeFS` acquired `DVC`** — one company, one entry. DVC's original company, Iterative,
  rebranded to **DataChain**, and `iterative.ai/careers` 302s to a 404.
- **`Delta Lake` is a Databricks project**, not a company — Databricks is already Tier 1.
  Adding a project as a company is the duplicate-target version of the `ashby/cedar` trap.

**Validated:** all 62 api slugs resolve, return parseable JSON, and belong to the intended
company. 15 entries were corrected in the 2026-07-09 audit — see the bucketed commits
following the `pre-audit baseline` commit.

**Valid but currently empty** — correct slugs, genuinely zero open reqs. Do not "fix"
these; they are not broken:

- `greenhouse/root` (Root Insurance)
- ~~`greenhouse/dbtlabsinc` (dbt Labs)~~ — **no longer true as of 2026-08-03.** The board
  now returns HTTP 404, so this is `FETCH_FAILED`, not empty: the slug is gone, not idle.
  `repair` detects it and finds nothing, because `getdbt.com/about-us/careers` is a 1.4 MB
  JS shell that never mentions Greenhouse — the documented blind spot. Needs a slug read
  off the rendered page by hand. Until then it is a legitimate `EXIT_DEGRADED` every night,
  which is a real signal, not the permanent-empty exemption it used to have.

**Matching has run, and `resolve` has now drained the queue (2026-07-25).** Verdicts
in `state.db` by who decided them:

| Verdict | rules | llm | Meaning |
|---|---|---|---|
| `reject` | 7,084 | 572 | A rule fired, or the model read the description as not-entry. |
| `uncertain` | 867 | — | No level token in the title *and* not engineering-signal (the model's queue is scoped to engineering titles; the rest correctly park). |
| `match` | 30 | 99 | Open matches, not run totals. |

`resolve` was a silent no-op until the 2026-07-24 `response_format` fix; its first real
run (2026-07-25) considered 670 engineering-signal uncertains and settled 665 (99 → match,
566 → reject), leaving 5. The uncertain backlog fell 1,538 → 867 — and the remainder is
the non-engineering tail `looks_engineering()` deliberately leaves alone, not a review pile.

**Then `check` erased all of it (discovered 2026-08-02, fixed the same day).** By the time
anyone looked, 0 of those 99 `llm/match` verdicts survived and 570 of the 572 `llm/reject`
were gone; the uncertain backlog was back to 1,742. `cmd_check` re-derives a rules verdict
from the *title* for every posting it fetches, and the title is exactly what the rules
already found insufficient — so an unpinned llm verdict lasted one night. Human verdicts
were never affected because `overrides` pins them.

The fix is that `resolve` now pins too, with `overrides.decided_by` keeping the two apart.
A model pass may never displace a human ruling (`set_override` returns False and declines);
the reverse is allowed. **Re-run `resolve` to restore the queue.** The old advice — "run
`check` then `resolve`, in that order" — is no longer load-bearing, but it is still the
right order: `resolve` should see the night's new postings.

The first check run reported 129 matches, mostly noise; adding the engineering-gate
requirement (a level token alone is not enough — see `match.py`) cut it. That fix is the
reason `role_type_include` and `engineering_terms` are separate lists.

The **aggregator** feed adds ~318 postings on the next `check` (151 rules-matches — new-grad
roles across many employers, some not otherwise tracked). Validated on a scratch copy
2026-07-25; not yet in the live DB because it enters via `check`.

**Still leaking, as of 2026-07-23.** The engineering gate can still be satisfied by a
non-engineering role: Stripe's *"Seller Systems Operations Associate (Night Shift)"*
matches on `level:associate+role:systems`, because `systems` in `role_type_include` fires
on an operations title. This is the `Finance Associate` bug through a different door. Fix
it with the tuning loop and a regression check, not with a bare YAML edit.

**The model passes became tasks (2026-08-13).** `resolve` and `rank`'s judging phase are
now `level` and `judge` in the queue behind `jobtracker work`, joined by a third task,
`prefill`. Both old commands still exist and still work — `resolve` is literally
`work --task level`. Transport moved to the `sir-client` SDK and the `Provider` registry
was deleted. See `docs/tasks.md` and `docs/prefill.md`; the schema gained
`task_attempts`, `form_fields`, `prefill_gaps` and `prefill_plans`.

**Not yet done:**

- **The tracker markdown is a stale mirror.** `status`, `last_checked`, and
  `last_posting_seen` are still untouched across all 99 entries — not because nothing has
  run, but because v2 keeps run state in `state.db` and never writes back to the markdown.
  Don't read the markdown to learn what happened; query `state.db` or open the dashboard.
- The Ouckah/CVrve aggregator is still unwired — its 2026/2027 new-grad repo URL is
  unconfirmed (2025 archived, 2026 404s), so its `companies.yaml` entry has no `board_url`
  and is skipped. Simplify **is** wired and fetched (2026-07-25) — see "Aggregator sources".

**Doc bug, unfixed (found 2026-08-02).** Three places — this file, `docs/llm.md`, and
`resolve.py`'s docstring — claim *"Member of Technical Staff"* stays UNCERTAIN as a known
blind spot. It does not: `staff` is in `exclude_titles`, so `match()` rejects it outright
long before `resolve` could look at it. The existing test only asserts
`looks_engineering()` is False, which is true and a different claim. Either the title
should be reachable or the docs should stop saying it is — decide with `jobtracker eval`,
not a bare YAML edit.

---

## Operational notes

- Fetching `boards-api.greenhouse.io` ~12-way parallel gets egress throttled; curl then
  returns `http=000` for *every* host, including Lever and Ashby. It looks exactly like
  mass breakage. Pace requests and re-check sequentially before believing a failure.
- Drop `?content=true` when you only need job counts. Full-content payloads are large
  enough to blow a 20s timeout on big boards (Databricks carries ~787 reqs).

### Observability

Progress goes to **stderr** via `logging`; the report goes to **stdout**. `check > out.md`
stays clean, and a run is watchable live rather than silent until it finishes.

- Default level is INFO: one line per board as it lands (`[12/56] Stripe  518 jobs (1.1s)`),
  plus a phase line for health/match and for report rendering.
- `-v` → DEBUG, one line per HTTP attempt. `-q` → warnings and errors only.
- **Log format follows the terminal.** A TTY gets the human lines above; a pipe or
  container gets one JSON object per line — same content, with any `extra={...}` fields
  promoted to top-level keys and a UTC-offset timestamp. Override with
  `JOBTRACKER_LOG_FORMAT=json|text|auto` (default `auto`, keyed off `stderr.isatty()`).
  Still stderr-only, so `check > out.md` is unaffected. The JSON formatter is hand-rolled
  in `cli.py` — no dependency, same rule that keeps a web framework out of `server.py`.
- `fetch_all` uses `as_completed` so lines appear as boards finish, but reassembles results
  into **input order** before returning. Downstream stays reproducible — don't "simplify"
  this back to `pool.map`.
- Retries are logged, including ones that *succeed* (`recovered on attempt 2/3`). A board
  that quietly needs two tries every day is degrading; silence used to hide that.
- The end-of-fetch summary reports failures, retry count, and **cumulative time asleep in
  the per-host limiter**. This is the fast way to tell pacing from breakage: a healthy
  56-board run is ~29s wall with ~106s of pacing summed across 4 workers, i.e. essentially
  all of it is deliberate spacing on `boards-api.greenhouse.io`. Near-zero pacing plus a
  fast run means something returned early, not that things got faster.

### OpenTelemetry

Off by default. `--telemetry console` prints spans to **stderr**; `--telemetry otlp` ships
them to `$OTEL_EXPORTER_OTLP_ENDPOINT`. Env equivalent: `$JOBTRACKER_TELEMETRY`.

- `jobtracker/telemetry.py` is the **only** file allowed to import `opentelemetry.sdk`.
  Instrumented modules import `opentelemetry.api` and nothing else — with no provider
  configured the API is a no-op, which is why `fetch.py` has no enablement checks.
- `fetch.py` is instrumented today: `fetch.all` → `fetch.company` → `http.request` →
  (auto-instrumented) `GET`. Retries are span events, not extra spans.
- Worker threads do **not** inherit OTel context. `fetch_all` captures it and
  `_fetch_timed` attaches/detaches it. Remove that and you get 56 orphan traces.
- Span names stay low-cardinality (`fetch.company`, never the company name). Identifiers
  belong in attributes. Never attach posting IDs or URLs — 8k postings per run.

### Metrics and the tier-3 stack

`compose.yaml` + `otel/` run collector → Jaeger (traces) + Prometheus (metrics) + Grafana.
`otel/stack.sh {up|down|run}` does the same with plain podman, since this machine has no
compose provider installed.

Two decisions here exist *because this is a batch job*, and both break silently if undone:

- **Metrics are pushed, never scraped.** A 30-second daily process is essentially never
  running when a scrape interval elapses. The collector remote-writes into Prometheus
  (`--web.enable-remote-write-receiver`); nothing scrapes jobtracker.
- **Counters are exported as DELTA, not cumulative** (`_delta_temporality()` in
  telemetry.py) and reassembled by the collector's `deltatocumulative` processor.
  Cumulative means "total since process start", which resets to zero every run and makes
  a backend read restarts as decreases. Verified: two consecutive runs of 60 new postings
  each report 60 then 120, not 60 then 60.
- **`service.instance.id` is pinned to the hostname.** The SDK defaults it to a random
  UUID per process, which would mint a fresh Prometheus series every night.

Metric names arrive in Prometheus with dots → underscores, unit suffixes appended, and
`_total` on counters: `jobtracker.fetch.duration` becomes
`jobtracker_fetch_duration_seconds_bucket`, attribute `health.status` becomes label
`health_status`.

Keep metric attributes bounded — `ats` (4 values) and `outcome` (2) are fine; `company`
(56) belongs in traces, not metrics.

**Containerized runs MUST set `JOBTRACKER_INSTANCE_ID`.** `telemetry.py` pins
`service.instance.id` to `os.uname().nodename` so a daily job keeps one continuous
Prometheus series. Inside a container that nodename is the *container ID*, which is a new
random value on every `--rm` run — so the pinning silently does the opposite of its
intent and mints a series per run. `otel/stack.sh run` passes the host's name; any new
invocation path (cron, compose, CI) has to do the same.

### Querying: the counters are cumulative

`deltatocumulative` means Prometheus sees monotonic counters, so **`last_over_time` on a
counter is the all-time total, not last night's run.** Use `increase(...[24h])` for
"what happened in the last day", and `increase(sum)/increase(count)` for a histogram
average. `increase()` cannot see a rise preceding the series' first sample, so a freshly
wiped Prometheus reads 0 on day one and self-corrects on day two.

"How long since the last run" is the one query that catches a job that stopped, and the
obvious idiom is silently broken — `time() - timestamp(last_over_time(...))` always
returns 0, because `last_over_time` re-stamps at evaluation time. The working form is
`time() - max_over_time(timestamp(jobtracker_run_duration_seconds_count)[24h:1m])`.

### The Grafana dashboard

`otel/grafana-dashboard.json`, provisioned by `otel/grafana-dashboards.yml`. The file is
the source of truth; `allowUiUpdates: true` permits live experimentation but a restart
discards anything not saved back. Datasource `uid`s in `otel/grafana-datasources.yml` are
pinned (`prometheus`, `jaeger`) because the dashboard binds by uid — remove them and
every panel reads "Datasource not found".

**Panel descriptions carry the why.** That is where facts like "dbt Labs and Root
Insurance are legitimately empty, do not fix them" live, so someone reading
`suspect_empty: 2` at 2am doesn't go repair two healthy boards. Keep them current when
the reasoning changes.

## The HTML dashboard

`jobtracker dashboard` renders `state.db` to a single self-contained HTML file
(`data/dashboard.html`, gitignored). Separate concern from Grafana: Grafana watches the
*pipeline*, this watches the *job search* — open matches, the uncertain backlog, flagged
boards, and the manual companies that are never scraped.

**Three tabs, and Today is the landing screen** (2026-08-02). The page opens on the three
jobs to apply to, each with the model's one-line reasoning and an Apply link; the old
front page moved to "All postings" and board health to "Boards". The point is to shorten
the distance between opening the page and applying to something.

- **Panels are server-rendered; the script only toggles `[hidden]`.** `.tabs` is
  `display:none` until JS confirms it is running, so with JS off every panel shows
  stacked — exactly the pre-tabs page. Same rule as the row filters.
- **The picks must never be a `table[data-filterable]`.** The filter JS selects those, so
  a tier or location filter left set on another tab would silently empty a curated list
  the user never asked to filter. There is a test.
- **Disposition buttons only exist under `serve`** (`build_dashboard(interactive=True)`).
  They POST, and the static file has to stay offline and read-only — a dead button in a
  mailed file is worse than no button. The "Open prefilled" button follows the same rule,
  and for a stronger reason: it drives a browser, which only a live process can do. The
  *counts* (`prefill 13/16 fields · 3 need you`) render in both, because they are useful
  offline — they say whether opening a job takes thirty seconds or ten minutes.
- **`serve` has a third page, `/settings`** — the answer bank and every question prefill
  could not answer. `render_settings` is connection-in/string-out like `render_tuning`,
  and `POST /api/answer` writes through `safewrite`. `POST /api/apply-to` starts the
  browser **on a daemon thread**: `server.py` is `HTTPServer`, not `ThreadingHTTPServer`,
  so driving it inline would freeze the page for as long as the window stayed open.
- **It is a pure read.** Unlike `report`, it never marks manual companies as surfaced.
  Opening a view of your data must not mutate it; there is a test asserting this.
- **No network at view time, ever.** No CDN, no chart library — the one chart is CSS.
  That is what makes the file mailable and openable offline years from now.
- **Rows render server-side; JS only hides them.** With JS off you still get every
  posting. Don't "improve" this into client-side rendering from embedded JSON.
- **Escape everything.** Titles and locations come from third-party ATS APIs. URLs get a
  scheme check too — a `javascript:` href would execute on click.
- Tier color is **three bands, not seven steps** (T1–T2 anchor, T3–T5 applied, T6–T7
  research). Seven steps do not fit the blue ramp's usable range with visible gaps; the
  tier number is always printed, so color is reinforcement, never the encoding.
- **Location sorts, never filters.** Rows come out NYC-first (`match.location_rank()`),
  NYC rows carry a pin, and there is a location dropdown that defaults to "Anywhere".
  A location filter the user did not choose would silently hide roles they asked to see.

## The tuning loop

`criteria.yaml` is easy to edit and hard to edit safely: a token added to stop one bad
match silently changes the verdict on thousands of postings already judged. Full guide
in `docs/tuning.md`; the rules that matter here:

- **Never hand-edit `criteria.yaml` without running `jobtracker eval`.** It replays the
  current rules against every recorded judgment and exits 1 on a regression. This is the
  only thing standing between "fixed one leak" and "silently re-broke three".
- **`uncertain` is not a regression.** If the rules say `uncertain` where a human said
  `match`, that is correct behaviour — the level genuinely is not in the title. Counting
  it as failure pushes toward rules that guess level from titles, which is the
  over-fitting the whole mechanism exists to prevent. Only active contradiction blocks.
- **Suggestions are string counting, not a model**, and they are scoped to rejects the
  rules do *not* already handle so the list terminates. Do not "improve" this by feeding
  it to an LLM; the zero-in-accepted test is what keeps `engineer` and `software` out
  without a hand-maintained blocklist.
- **Overrides outrank rules** and survive rematch, carrying `decided_by='human'`. They
  are applied in the caller path (`cmd_check`, `cmd_rematch`, `serve`), never inside
  `match()` — that function's purity is load-bearing for the tests.
- `decisions.title` is denormalized on purpose. Joining to `postings` would shrink the
  corpus every time a req closed, which is exactly when the evidence matters most.

## The task queue

`jobtracker work`, documented in `docs/tasks.md`. Added 2026-08-13, and it is where all
model work now lives: `level` (was `resolve`), `judge` (was `rank`'s first phase), and
`prefill`. The scheduler polls tasks by priority and runs the first with work.

- **`work` rescores after every run, and that is load-bearing.** `judge` writes a ranking
  with a NULL score and `prefill` only queues postings that have one, so without it a
  `work` loop drains level, drains judge, then reports "prefill: nothing to do" forever —
  a stall that looks exactly like an empty queue. Scoring stays out of the queue (no
  model, must always run) but it is still a link in the chain, so the runner closes it.
  There is a test that walks one posting from uncertain to prefilled using only `work`.
- **`jobtracker prepare` is the nightly "is tomorrow useful?" check.** Rescore, take the
  postings `today` will surface, prefill exactly those, exit 2 if any has no plan.
  **Gaps never cause exit 2** — an unanswered question is the normal state and failing on
  it would make the unit permanently red for something only the user can clear, the same
  trap as flagging dbt Labs' empty board.
- **Priority is the pipeline's dependency chain, not a preference.** level → judge →
  prefill, because each produces what the next consumes. Reorder it and "work the next
  available task" stops meaning "keep every stage drained", which is the entire reason
  the scheduler exists. There is a test.
- **The queue is derived, never stored.** Each task's `pending()` is a SQL read over
  tables that already exist, so there is nothing to reconcile — a posting that closes
  overnight simply stops appearing. `task_attempts` is a *failure ledger*, not a queue:
  three consecutive failures set a unit aside so it stops eating the budget while the
  rest of the queue starves. Do not turn it into a work table.
- **Every unit commits on its own.** That is the fix for a real defect — the old passes
  held everything until one commit at the end, so an interrupted run wrote nothing. A
  task that raises while writing is rolled back to the last committed unit.
- **`unit_key` is the question, not the posting.** `judge` carries the profile prose
  hash, `prefill` the answers hash. Change the question and every unit is new, with its
  retry count reset — correct, because a failure answering the old question says nothing
  about the new one. It is also the router's idempotency key.
- **`pending()` must only return work the task can actually do.** `level` excludes
  postings with no cached description; `prefill` excludes companies whose form it has
  never seen. Counting those overstates a backlog no model could drain, and sends a
  budgeted run to guaranteed no-ops.
- Adding a task is one module plus one import line, same as an ATS. **Task modules are
  pure** — prompts, parsers, and a description of what to write; `runner.py` owns every
  socket, transaction, and clock.

## The ambiguity pass

The `level` task, documented in `docs/llm.md`. **Local only** — the model is an address,
and there is no API-key handling anywhere in `jobtracker/llm/`. Do not add a hosted
provider.

- **Transport is the `sir-client` SDK** (`../stupid-inference-router`), since 2026-08-13.
  The `Provider` interface and its registry were **deleted**, not kept: they existed so a
  second wire format could be slotted in, and the router is that indirection now. Two
  dispatch layers doing one job is what was removed. `llm/` is two files — `wire.py`
  (pure, knows the body shape) and `client.py` (the only module that opens a socket).
- **The SDK is async-only**, which is why `tasks/runner.py` is async and why
  `browser.py` — Playwright's sync API, which must not run inside a loop — is a separate
  module. Do not try to merge them.
- **`sir` forwards the body untouched.** It reads only `model` and `stream`. So the
  schema request still travels, and the parsers are still the only thing between a
  backend that ignores it and a fabricated verdict. Routing through a router is not a
  guarantee about anything.
- **Scope is level extraction only.** The model never decides that a role is on-target;
  an `entry` reading still has to pass the rules' engineering gate. Widening this would
  put a nondeterministic component back in the main loop, which is what DESIGN.md was
  written to undo.
- **Every failure path must leave the posting UNCERTAIN.** Unreachable, timeout,
  malformed, unsure — all of them. Nothing here may raise for a down server. If you add
  a code path that can produce a verdict from a failed call, that is a bug.
- **The schema request is `response_format`, not `guided_json`** (changed 2026-07-24).
  vLLM dropped the `guided_json` / `guided_decoding_backend` pair; 0.23 accepts a body
  carrying them, *ignores* them, and answers in prose. `_parse_verdict` then rejected
  every response and the whole pass became a silent no-op — it still fetched a
  description per posting and resolved nothing. Because failure-is-absence is the
  design, this cost no accuracy and raised no error, which is exactly why it could sit
  undetected. Diagnostic: `work` reporting ~zero applied while the server is up means the
  wire format, not the model. Verify against the server you actually run —
  `test_request_constrains_output_and_is_deterministic` pins the request shape, but only
  a live call proves the server honours it. Demonstrated again 2026-08-13 against the
  router's *mock* backend, which ignores the schema: every prefill question-match came
  back unparseable and every field became a gap. Right answer, and a good reminder.
- **The `level` task is a pure read** (2026-08-02). `check` caches the description for every
  match/uncertain posting, so this pass opens no ATS connection at all — it lost its
  `fetcher`/`store_mod`/`conn` parameters and its lazy fetch-and-cache block. A throttled
  board can no longer shrink the queue it considers.
- The queue is scoped to titles with an engineering signal (674 of 1,537). Known blind
  spot: "Member of Technical Staff" is never read. It stays UNCERTAIN rather than being
  rejected, and there is a test asserting that. Do not "fix" it by rejecting no-signal
  titles. (**But see the doc bug under "Not yet done"** — `staff` is in `exclude_titles`,
  so `match()` actually rejects that title outright and `resolve` never sees it.)

## Descriptions are cached by `check`

Since 2026-08-02, `cmd_check` stores a description for every posting whose verdict is
`match` or `uncertain`. This is what makes `resolve` and `rank` offline with respect to
the ATSes: they read `state.db` and talk to nothing but the local model.

- **Scoped, deliberately.** The ~7,100 open rejects are excluded — fetching them would
  cost ~40 minutes and ~48MB to serve nothing that reads them. Self-healing: retune
  criteria so a former reject becomes a match, and the next `check` fills it in.
- **Write-once.** `NULL` = never fetched, `''` = fetched and genuinely empty. Only NULL
  is retried.
- **`--max-descriptions` (default 400)** caps requests per run so a bad night cannot turn
  a 30-second job into a 40-minute one. Measured: ~0.6s per Greenhouse fetch behind the
  existing limiter, ~30–40 fetches/night in steady state.
- **A description failure is invisible to board health** and must never produce
  `EXIT_DEGRADED`. A 500 on one job detail is not a broken board.
- Ashby and Lever ship `descriptionPlain` in the **bulk** payload for free; only
  Greenhouse needs a per-posting fetch, and its `content` is HTML-escaped *inside* the
  JSON string, so unescape before stripping tags. Fetches go through
  `Fetcher._request_json` so they inherit the per-host limiter — the ATS is the scarce
  resource, not the local model.

## Posted dates

`postings.posted_at` is the vendor's raw value and is **three mutually incomparable
formats** — Greenhouse ISO-with-offset, Ashby ISO-UTC-with-millis, Lever epoch-millis as
a string, aggregator a relative age like `2d`. As text an epoch string collates before
every ISO timestamp, so `ORDER BY posted_at` is silently wrong. Nothing may sort on it.

`postings.posted_on` is that value normalized to a plain ISO day, and is the only date
anything is allowed to compare. Conversion lives in the adapter behind
`Source.normalize_posted_at(raw, today)`; `today` is a parameter, not a clock read,
because adapters are pure and one source dates relatively.

- **Greenhouse's bulk field is `updated_at`, which is not a posted date.** It moves
  whenever anyone edits the req. Observed: a Stripe posting first published 2023-11-01
  reporting `updated_at` of 2026-07-27 — ranked on the old field it would have looked
  like the freshest thing on the board. The real value is `first_published`, which only
  exists on the detail payload, so it arrives with the description fetch.
- **`sync_postings` writes posted_on with COALESCE.** A bulk pass with no date must not
  erase one already stored, or the 47 Greenhouse boards would blank their own dates
  nightly.
- **Unparseable input is NULL, never today.** A missing date reading as "posted now"
  would invert the ranking it exists to inform.
- `first_seen` is not a substitute: 8,634 of 9,765 rows share the 2026-07-23 backfill date.

## The ranking pass

`jobtracker rank` and `jobtracker today`, documented in `docs/ranking.md`. Where `level`
decides whether a posting is *on-target*, this decides which of those is *urgent*. It is
the model's second bounded role, and the smaller one.

Judging is the `judge` task now. **Scoring deliberately is not a task** — it needs no
model, must run whether or not one is reachable, and is arithmetic over rows the task
already wrote. Keep it in `cmd_rank`.

- **The model judges one posting; Python does the ordering.** It returns three labelled
  ordinals (`backend_fit`, `growth`, `entry_risk`) plus a sentence, never a score and
  never a comparison, and it never sees another posting. Widening that would put a
  nondeterministic component back into the ordering itself.
- **Ordinals, not 0–100.** LLM numeric scores cluster in a narrow band and shift with any
  prompt or model change, which would silently re-rank everything on an unrelated edit.
- **`profile.yaml` splits prose from weights, and `prose_hash` covers only the prose.**
  That is the mechanism, not a detail: change a weight and every cached judgment stands,
  so re-sorting the queue costs zero model calls. Change the prose and judgments are
  re-taken, because they were answers to a question you have now changed.
- **Scores are absolute.** A new posting is judged once and lands in its slot without
  disturbing anything else. Do not replace this with pairwise insertion: it costs
  ~log₂(n) calls per posting, is path-dependent, and cannot recover from non-transitive
  comparisons.
- **Two absences that must not be "simplified":** an unjudged posting scores `None`, not
  `0.0` (zero buries a model failure at the bottom of the list); an undated one scores
  mid-scale, not "today" (that floats stale reqs to the top). Both have tests.
- **Rankings live in their own table, never in `verdicts`.** `check` rewrites `verdicts`
  every night — that is what erased the LLM's work before.
- **`rank` never fails for want of a model.** With none configured or reachable it skips
  judging and still scores from stored judgments; yesterday's order beats nothing.
- **`rank` can only judge what `check` cached.** A large "still unranked" count while the
  model is up usually means the description backfill is still draining, not a model fault.

## Prefilled applications

`jobtracker work --task prefill` and `jobtracker apply-to`, documented in
`docs/prefill.md`. Added 2026-08-13. Two halves: an offline task that builds a plan and
names what is missing, and an on-demand browser that carries the plan to the page.

- **A cookie cannot carry prefill state, and neither can a URL.** Greenhouse, Ashby and
  Lever hold no server-side draft for an anonymous candidate, only Lever honours
  query-parameter prefill, and no URL of any kind attaches a file. What fills a
  third-party form is code running on the page. Do not re-propose the link.
- **The browser never submits.** There is no click path in `browser.py` at all and a
  test asserts it against the source (no `.click(`, `.press(`, `requestSubmit`,
  `dispatchEvent`). An application is irreversible and goes out under the user's name.
- **The model may only point, never write.** Its schema is an enum of answer keys the
  user already wrote plus `none`. There must be no code path by which a sentence the
  model composed reaches a form field — free text with no stored answer is a gap, the
  same as an unanswered dropdown. It is the fourth bounded role in DESIGN.md §8, and the
  narrowest.
- **A dropdown that does not offer our answer is a gap, not a fill.** Picking the nearest
  option puts an answer the candidate did not give onto a submitted application.
- **`answers.yaml` is gitignored** — it is personal data. `answers.example.yaml` is the
  tracked file. Everything above the `# ===== unanswered questions` marker is the user's
  and is never parsed or rewritten; the block below it is regenerated wholesale from
  `prefill_gaps` on every run. Writes go through `safewrite.py` (candidate → parse →
  `.bak` → atomic swap), extracted from `server._api_rule`, which had it inline.
- **Adding an answer is text surgery, not a YAML round trip.** A round trip deletes every
  comment in the file, including the stubs the user is working through.
- **Only Greenhouse publishes its form** (`?questions=true`, keyless, complete — 47 of 62
  api boards). Ashby's per-job posting-api is 401 and its GraphQL introspection is off;
  Lever exposes no custom questions. Verified 2026-08-13. Their forms are learned from
  the DOM on the first `apply-to` visit and cached per company, which is what puts every
  ATS in the same gap loop. A company whose form is neither held nor fetchable is **not**
  counted as pending work — it is waiting on a browser, not a model.

Three things learned from live forms; all are handled and none is obvious:

- **Greenhouse's current board UI sets no `name` attributes** — everything is keyed off
  `id`, including `id="resume"`. Reading only `name` silently failed to attach the
  resume, which is the single most valuable field. Field keys are `name` → `id` → a slug
  of the label.
- **One visible question can be several inputs.** A combobox renders as a text input plus
  a hidden select; "Resume/CV" is a file input plus a textarea, either of which satisfies
  it. Once any sibling holds the answer the question is answered and the rest are not
  gaps.
- **Some employers redirect the hosted board to their own careers site.** Stripe's
  `absolute_url` is a search page with no form on it, and `job-boards.greenhouse.io`
  redirects there too. Greenhouse gets the canonical board URL when the slug and job id
  are known, and **zero fields discovered is reported as "no application form found",
  never as "0/0 filled, nothing left to do"** — absence read as success is the failure
  DESIGN.md §3.4 exists to prevent.
## Slug repair

`jobtracker repair`, documented in `docs/repair.md`. The third and last of the model's
bounded roles (DESIGN.md §8), and the only one where the model is a *fallback* rather
than the mechanism. Deterministic regexes read the careers page first; the model is asked
only about pages they could not parse.

- **The trigger set is narrower than `is_degraded()` on one axis and wider on another,
  and both differences matter.** `IDENTITY_DRIFT` fires immediately (the assertion is
  deterministic over two stable strings). `FETCH_FAILED` needs `REPAIR_FAILURE_THRESHOLD`
  = 2 consecutive *nights* — `fetch.py` already burned `MAX_RETRIES` inside the run, so
  anything transient is absorbed a layer down, and rewriting a hand-verified slug because
  a CDN hiccuped once is the expensive mistake. Alerting `SUSPECT_EMPTY` is included
  because a dead board never presents as `FETCH_FAILED`.
- **Non-alerting `SUSPECT_EMPTY` never triggers.** dbt Labs and Root Insurance would
  otherwise be "repaired" every night. There is a test named after them.
- **`manual` and `aggregator` companies are never targets.** A careers page is a scrape.
- **Nothing is proposed on the strength of a string.** Every candidate — regex or model —
  is fetched through the real `Source` adapter and rejected if the board is empty (the
  `greenhouse/hubspot` rule) or its identity does not match (the `ashby/cedar` rule).
  Both have named regression tests; do not weaken them.
- **`no_identity` is checked before `wrong_company`.** `health.identity_matches` returns
  True when either side is empty — right for the nightly loop, catastrophic here, where
  it would read "the identity endpoint 500'd" as "verified".
- **Ashby and Lever identity is tautological for a candidate slug**, because it is derived
  from the job URL and therefore just restates the candidate. Those proposals are accepted
  on *provenance* (the link came off the company's own careers page) and labelled
  `evidence_kind: provenance` with sample titles attached. Never let that comparison read
  as identity proof.
- **A model slug must appear on the page it was shown**, after a `/` or `=`, case-
  sensitively. A bare substring test is not enough: the company name is all over its own
  careers page, so every invented slug would ground.
- **`repair` never writes without `--write`,** and `--write` moves `expected_board_name`
  along with the slug — leaving the dead board's name behind would make the repaired board
  drift on the very next run.
- **The companies.yaml writer is line-oriented, not a YAML round-trip.** PyYAML re-folds
  long strings to its own width, so a round-trip to change one `slug:` re-wraps `notes:`
  prose on unrelated entries — measured, ten of them. The deliverable here is a diff
  somebody reads, and reflow noise destroys that. `verify-slugs --write` shares the writer
  and got the same fix.
- **Known blind spot, do not "fix" it in the regexes.** A careers page that renders its
  board link in JavaScript often contains no identifier at all — HubSpot's is 519 KB with
  neither `greenhouse` nor `hubspotjobs` in it. Those report `no_candidates` and stay
  visible.
## Aggregator sources

`jobtracker/sources/aggregator.py`. Community new-grad list repos (SimplifyJobs-style) are
the highest-yield source for new-grad roles specifically — they aggregate across every
company, including ones not on our list (DESIGN.md §9). One `check_method: aggregator`
entry with a `board_url` = one feed. The adapter parses the README's HTML `<table>`; the
fetch is `Fetcher.fetch_aggregator` → `_request_text` (text, not JSON), then it flows
through the same health/`sync_postings`/`match` loop as any board in `cmd_check`.

- **The feed is the `company`, the employer is in the title.** One feed lists many
  employers, so we keep the feed name as `Posting.company` (one stable diff namespace per
  feed) and set `title = "Employer — Role"`. The title-only matcher reads that fine and the
  employer stays visible in the dashboard without a schema change. Caveat: an employer name
  containing a title-shaped exclude token would be conservatively rejected — near-zero risk,
  not yet observed.
- **`ats_job_id` is the Simplify `/p/<uuid>` when present, else a hash of employer+role.**
  Stable across runs so `sync_postings` recognizes the same row and closes a dropped one.
- **Closed rows (`🔒`) are skipped** — a filled req is not an opening. At last check 1,745
  of 2,072 rows were closed; ~318 open.
- **A missing `board_url` skips the feed** rather than failing the run. That is why the
  unverified Ouckah/CVrve entry costs nothing — the subsystem is generic, so it works the
  moment a confirmed URL is set (same table format).
- **Parsing tolerates garbage** (`[]` on any unexpected shape) — these repos rename by cycle
  and restyle the table; an empty feed is a visible SUSPECT_EMPTY, never a crash.

## Deployment

`docs/deployment.md`. **This repo does not know about orchestrators** — no Kubernetes
manifests, no systemd units in-tree. The deliverable is a container plus a documented
contract; the units live on the machine that runs them.

**CD publishes that container; it does not deploy it (added 2026-08-15).** Green `main`
pushes `ghcr.io/ida314/job-tracker:{sha-<sha>,latest}` from the `publish` job, built on
`ubuntu-24.04-arm` — arm64 only, matching the DGX Spark target and this aarch64 laptop.
The host **pulls**; nothing in CI holds credentials to a machine. That keeps the rule
above intact: publishing an artifact names no orchestrator.

- **`sir-client` is baked in, and CI asserts `import sir_client` on the published
  image.** Without it `work` is a silent nightly no-op that still exits 0 — the same
  failure-is-absence shape as the `response_format` regression. An unverified image
  would reproduce it at the deploy layer.
- **Two Dockerfile traps, both fixed 2026-08-15.** `python:*-slim` carries **no git**, so
  the `SIR_CLIENT="git+ssh://…"` invocation this file documented could never have worked;
  git is now installed and purged inside one `RUN` (+8MB net, not +50). And a credential
  must **never** be a build arg — build args are readable with `docker history` on the
  published image. The Dockerfile takes an optional BuildKit secret instead, and wipes
  `/root/.gitconfig` in the same layer. The router repo is public today, so no secret is
  needed; if it goes private the clone fails loudly rather than shipping an SDK-less
  image.
- **Every run names its own build**: `jobtracker 0.1.0+<sha12>`, from `JOBTRACKER_REVISION`
  (`ARG GIT_SHA`), logged by `main()` for every subcommand and set as `service.version`.
  This exists because a host that failed to pull is otherwise indistinguishable from one
  that succeeded — identical runtime, identical report, exit 0. A bare `0.1.0` means not
  running from a published image; absence is never guessed at.
- **Rollback is by tag.** Pin `Image=` to a good `:sha-…`. `podman auto-update`'s
  rollback needs a healthcheck, which a 32-second batch job does not have.
- Forward image updates are safe unattended: `store.py` does `CREATE TABLE IF NOT EXISTS`
  plus additive column migrations on connect, so a new image upgrades `state.db` in place.
- **The host half is not written yet.** gx10 is a DGX Spark on the tailnet; whether it
  runs podman/quadlet (like the laptop) or Docker (DGX OS's default) is unconfirmed, and
  that decides the unit shape. Nothing is deployed there today.

- `check` exits 0 (clean), 2 (a board needs attention), or 1 (could not run).
- `serve` is the one long-running process, so it carries the service affordances: it
  exposes `/healthz` (liveness — a constant, never touches the DB) and `/readyz`
  (readiness — 503 until state.db opens *and* criteria.yaml parses, payload names which
  failed), and it drains the in-flight request and exits 0 on SIGTERM/SIGINT. This is
  what lets an orchestrator run it while the app stays orchestrator-agnostic; the app
  ships the endpoints, the deployment repo decides how to poll them.
- Exit 2 is narrower than `status != OK` — see `health.is_degraded()`. dbt Labs and Root
  Insurance are permanently `suspect_empty` and must never fail a run.
- **Containers must set `TZ`.** The image is UTC; `date.today()` drives `first_seen`, the
  report's `since` window, and `manual_due()`. A UTC container running at 21:00 local
  stamps tomorrow onto everything.
- `otel/stack.sh run` mounts the repo's `./data` — it is a **real run against real
  state.db**, not a smoke test. Point `JOBTRACKER_DB` at a scratch copy to exercise the
  stack without writing history.

## Repo conventions

- Each change to the tracker is its own commit, grouped by *failure class* (not by tier),
  so one class of mistake can be reverted with `git revert` without losing the rest.
- The commit before any correction is the `pre-audit baseline`; it exists purely as a
  revert target.
