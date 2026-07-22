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
- **Location: NYC or remote.** New York / NYC / Hybrid NYC / Remote US are preferred.
  Boston, Austin, Seattle, SF/Bay Area, Chicago, Atlanta, DC are acceptable.

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
confirmed against the company name. Current composition of the 89 entries:

| | Count | Status |
|---|---|---|
| `api` | 56 | All return valid JSON. 43 Greenhouse, 12 Ashby, 1 Lever. |
| `manual` | 31 | Never scraped, by rule. Flagged weekly. |
| `aggregator` | 2 | GitHub new-grad lists, diffed daily. |

**Tier 6 added 2026-07-20** — seven Chinese frontier AI labs, all `manual`. None has a
keyless JSON board; MiniMax, Zhipu, and Moonshot run Feishu/Lark boards that 405 on
unauthenticated reads. These entries do **not** satisfy the Match Criteria (`locations_*`
is US-only) and were added as research-interest targets at the user's request — only
ByteDance Seed and Alibaba have confirmed US hiring. Do not silently drop them for failing
the location filter, and do not "promote" any of them to `api`.

**Known name collision:** `ashby/moonshot-ai` is an unrelated NYC startup, not 月之暗面.
Live jobs, wrong company — the `ashby/cedar` failure mode again.

**Validated:** all 56 api slugs resolve, return parseable JSON, and belong to the intended
company. 15 entries were corrected in that audit — see the bucketed commits following the
`pre-audit baseline` commit.

**Valid but currently empty** — correct slugs, genuinely zero open reqs. Do not "fix"
these; they are not broken:

- `greenhouse/dbtlabsinc` (dbt Labs)
- `greenhouse/root` (Root Insurance)

**Not yet done:**

- No Match Criteria filtering has ever been run. No role matching has happened.
- `status`, `last_checked`, and `last_posting_seen` are untouched across all 89 entries.
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

## Repo conventions

- Each change to the tracker is its own commit, grouped by *failure class* (not by tier),
  so one class of mistake can be reverted with `git revert` without losing the rest.
- The commit before any correction is the `pre-audit baseline`; it exists purely as a
  revert target.
