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
| `board_url` | Full JSON API URL. Empty when `check_method` is not `api`. |
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
confirmed against the company name. Current composition of the 98 entries:

| | Count | Status |
|---|---|---|
| `api` | 62 | All return valid JSON. 47 Greenhouse, 13 Ashby, 2 Lever. |
| `manual` | 34 | Never scraped, by rule. Flagged weekly. |
| `aggregator` | 2 | GitHub new-grad lists, diffed daily. |

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

- `greenhouse/dbtlabsinc` (dbt Labs)
- `greenhouse/root` (Root Insurance)

**Not yet done:**

- No Match Criteria filtering has ever been run. No role matching has happened.
- `status`, `last_checked`, and `last_posting_seen` are untouched across all 98 entries.
  The first real daily run is still pending.
- The two aggregator sources have never been fetched or diffed. The Ouckah/CVrve repo URL
  is unverified and these repos rename by cycle year.

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

## Repo conventions

- Each change to the tracker is its own commit, grouped by *failure class* (not by tier),
  so one class of mistake can be reverted with `git revert` without losing the rest.
- The commit before any correction is the `pre-audit baseline`; it exists purely as a
  revert target.
