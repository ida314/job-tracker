# CLAUDE.md

Working notes for any agent operating on this repo. The one artifact here is
`backend-newgrad-2027-tracker.md`, a machine-parseable target list of companies to check
for backend new-grad openings.

**Keep this file current.** When we change how the tracker works — schema, check rules,
validation state — update the relevant section in the same commit.

---

## Goals

- **Backend new-grad, 2027 graduation.** Distributed systems, infrastructure, platform,
  data engineering, SRE. Not frontend, not ML-first.
- **Skill and career growth over pay or prestige.** Real distributed-systems work early
  beats a bigger name with a narrower first role. Tier 1 (backend scale-ups) and Tier 2
  (infra/devtools, where backend *is* the product) are the anchor. Tier 4 (Big Tech) is
  applied to, not anchored on.
- **Location ranks, it does not disqualify** (2026-07-22). NYC first, then elsewhere in
  the US, then unspecified, then outside the US. Nothing is rejected for where it is.
  Until 2026-07-22 `locations_exclude` was a hard gate firing *before* the level gate, so
  390 postings — London, Dublin, Bengaluru, Toronto, Singapore — were discarded without
  their titles ever being read. Don't reintroduce a geography gate; `match.location_rank()`
  only sorts.

Full include/exclude rules live in the `Match Criteria` YAML block in the tracker — the
source of truth for role matching. Don't duplicate it here.

---

## Field schema

Every company is an `### Name` heading followed by a flat `- key: value` list. Keep the
field order below; the parser and every `awk`/`grep` sweep depends on it.

| Field | Meaning |
|---|---|
| `ats` | `greenhouse`, `lever`, `ashby`, `workday`, `gem`, `bespoke`, `aggregator`, or `unknown` (Snowflake — portal type never confirmed) |
| `slug` | Board identifier within that ATS. A Workday slug is the *triple* `tenant/dc/site` (`redhat/wd5/jobs`) — the data centre is part of the hostname and is not derivable from the tenant name. Empty for `bespoke`. |
| `board_url` | Full JSON API URL for `api`; raw README URL for `aggregator`. Loaded into `Company.board_url`. Empty for `manual`. |
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
workday     POST https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
                 {"appliedFacets":{},"limit":20,"offset":N,"searchText":""}
```

Workday is **paged** — the first source whose board does not arrive in one call. See
"Paged boards"; its page cap is a trap, and so is its idea of an end of results.

---

## Rule: slugs are verified, never guessed

A slug is correct only once a live fetch confirms it belongs to the right company.
Guessing from the company name is how this tracker silently breaks. Two failure modes,
both observed 2026-07-09:

**A 200 with jobs does not mean the slug is right.** `ashby/cedar` returns live postings
for an unrelated mortgage/real-estate Cedar, not Cedar the NYC healthtech (which is
`greenhouse/careportalinc`, its legal entity). Confirm identity, not just reachability:

- Greenhouse: `GET https://boards-api.greenhouse.io/v1/boards/{slug}` returns `.name`.
- Ashby: check `.jobs[0].jobUrl` and read a few job titles.

**A 200 with zero jobs does not mean "no openings."** `greenhouse/hubspot` is a real board
named "HubSpot Product" that always returns an empty array; the live board is
`hubspotjobs`. Mercury and Vercel left stale, empty Ashby boards after migrating to
Greenhouse. An empty board must be investigated and reported as `ATS-check-failed`, never
recorded as "no matches."

When a slug fails, read the real one off the careers page — the Greenhouse embed
(`job_board?for=X`), or a link to `job-boards.greenhouse.io/X`, `jobs.lever.co/X`,
`jobs.ashbyhq.com/X`. Then update `ats`, `slug`, `board_url`, and `notes` together.

**That procedure is executable as `jobtracker repair`** (see "Slug repair" and
`docs/repair.md`). It stops where this rule says to stop: it proposes a diff, a human
applies it. Do it by hand when the page is a JavaScript shell — the documented blind spot,
not a bug to fix in the regexes.

Do **not** verify an Ashby slug by fetching `jobs.ashbyhq.com/{slug}` and checking the
status code — that host returns a 200 SPA shell for any string. Use the posting-api, or
the `ApiJobBoardWithTeams` GraphQL operation.

---

## Rule: `manual` companies are never scraped

If `check_method: manual`, do not fetch, scrape, or reverse-engineer a portal. These
companies (bespoke portals, Gem, Comeet, Avature, IBM Careers) have no keyless public JSON
board. The agent adds them to a "check these by hand" list in the daily report, at most
once per week per company, rate-limited via `last_checked`.

The point is honesty about coverage: surfacing a company for manual review is correct,
pretending to have checked it is not. Never let a `manual` entry silently report zero.

**The rule is unchanged; its premise was audited 2026-08-31 and was wrong for six
entries.** `manual` asserts a company *has no keyless JSON board* — a claim about the
world, and for Workday it was never true, merely never checked. Its `cxs` endpoint is
keyless, unauthenticated, clean JSON, identical in shape across every tenant. Red Hat,
Nvidia, Capital One, Workday and Target Tech are `api` now, behind an adapter like any
other board.

Amazon is the counter-example, and why "has a JSON board" is not the whole question.
`amazon.jobs/en/search.json` **is** keyless and ships descriptions inline — and still
cannot be read: offset paging is hard-capped, page 101 answers *"Cannot return more than
10000 results at once"*, and a run takes ~9 minutes before failing. An adapter was
written, measured, and removed the same day. It stays `manual` because a board that ends
`FETCH_FAILED` every night is not coverage, and narrowing it with `base_query` would be a
role gate applied before any title is read.

So the instruction is narrower than it looks:

- **Never scrape a company whose portal has no public JSON board.** Absolute. It protects
  Coralogix (Comeet mints its token in the page), Epic (Avature behind a login), YC Work
  at a Startup (login), and every entry in the "no marker" list below.
- **`manual` is a finding, not a category.** It records that nobody found an endpoint; the
  right response to "is there one?" is to look — at the network the page makes, not its
  DOM. Finding one is a reclassification, not a rule change.
- **A found endpoint is verified before it is written.** Same rule as slugs:
  `target/wd5/targetcareers` was fetched and returned 2,000 reqs before it went in the
  file. A guessed triple that 404s is indistinguishable from a dead board.

Recon notes for the 29 that remain, so nobody repeats the sweep:

| Company | Finding (2026-08-31) |
|---|---|
| Snowflake | Phenom People. `/api/apply/v2/jobs` returns structured JSON (`"Tenant not identified"`) — right endpoint family, tenant param not found. `ats: unknown` is known to be wrong. |
| Retool | Gem. `api.gem.com/v0/job-board/retool` answers **403 JSON**, not 404 — the endpoint is real and header- or key-gated. |
| Epic Systems | Avature (`epic.avature.net/Careers/Login`). Login-walled. |
| Coralogix | Comeet, rendered client-side; the uid+token pair is not in the static HTML. |
| SAP | SuccessFactors, and `jobs.sap.com/search/` is **server-rendered HTML** — parseable as text if ever worth doing. |
| JPMorgan, Fidelity, Walmart, Intuit | `ats: workday` unconfirmed for all four; none names a tenant in static HTML. Guesses already refused, so nobody repeats them: `jpmc.wd5/ExternalCareers`, `jpmorgan.wd5/ExternalCareers`, `fidelity.wd5/Fidelity`, `fidelity.wd1/FidelityCareers`, `walmart.wd5/WalmartExternal`, `intuit.wd5/IntuitExternalCareerSite` — all **422**. But `intuit.wd1/IntuitExternalCareerSite` answers **401**: a wrong tenant 422s, so Intuit's tenant exists at wd1 and only the site name is unknown. Read the triple off the rendered careers page rather than guessing — a 401 or 422 is FETCH_FAILED, never "no openings". |
| Rippling, HashiCorp, lakeFS, Two Sigma, Jane Street, Goldman, Bloomberg, Google, Meta, Microsoft, LinkedIn, Uber, tier-6 labs | No ATS marker in static HTML. Several are likely endpoint-behind-a-shell rather than browser-locked; none has been run down. |

---
## Validation state

**Audited 2026-07-09.** Every `check_method: api` entry was fetched and its board identity
confirmed against the company name. Composition of the 100 entries:

| | Count | Status |
|---|---|---|
| `api` | 68 | All return valid JSON. 47 Greenhouse, 14 Ashby, 5 Workday, 2 Lever. |
| `manual` | 29 | Never scraped, by rule. Flagged weekly. |
| `aggregator` | 2 | GitHub new-grad lists, diffed daily. |

**Import plugins are a fourth kind of source and are not in that table** (2026-08-31) —
not entries in `companies.yaml` at all. See "Import plugins" for why that absence is
load-bearing.

**Five entries promoted from `manual` to `api` 2026-08-31** — Red Hat, Nvidia, Capital One,
Workday and Target Tech, onto the Workday adapter. See the audit under "Rule: `manual`
companies are never scraped" for why that is a reclassification, and "Paged boards" for the
traps.

**Per-company reasoning lives in `companies.yaml` `notes:`, not here.** Every entry below
carries its own dated note — why the slug was accepted, what the board holds, why the tier is
what it is. What stays in this file is the cross-cutting rules those notes are applications of:

- **Tiers rank growth opportunity, not product category.** SolarWinds and Sumo Logic were first
  filed at tier 2 because observability is their product — wrong axis, moved to **tier 5** the
  same day (both PE-owned take-privates: cost discipline over growth, illiquid equity, thin
  new-grad hiring). Do not re-promote on product category alone. Tier 5 ends with the
  enterprise/PE cluster in ascending risk: Five9 → SAP → SolarWinds → Sumo Logic.
- **A near-empty board is the expected state on tiers 5–7 and at OpenRouter**, not a bad slug.
  Sumo Logic's 16 reqs beside SolarWinds' 95 and Five9's 160 is a hiring-appetite signal;
  Dremio has 6 reqs, LanceDB is 100% `Senior` and matches zero, Onehouse puts backend in
  Bangalore, lakeFS is Tel Aviv, and OpenRouter is a small team with no new-grad program. Do
  not flag any of them `SUSPECT_EMPTY`.
- **Tier 6 (seven Chinese frontier AI labs, added 2026-07-20) is never promoted to `api`.**
  None has a keyless JSON board; MiniMax, Zhipu and Moonshot run Feishu/Lark boards that 405
  unauthenticated. Research-interest targets at the user's request — only ByteDance Seed and
  Alibaba have confirmed US hiring. They stay tier 6 on hiring-pipeline grounds, not geography.
- **OpenRouter (`ashby/openrouter`) is verified by provenance, not identity** — the slug came
  out of the careers page's own JS chunk, because the posting-api's `jobUrl` restates the
  candidate slug and can never be identity proof.
- **Red Hat's original note ruled out three ATSes and mistook that for a fact about the
  company** — `greenhouse|lever|ashby/redhat` all 404, but its Workday board answers
  `redhat/wd5/jobs` with 122 reqs. The general form of that mistake is under "Rule: `manual`
  companies are never scraped".
- **Coralogix stays at tier 2 but is the least certain entry there** — genuinely
  observability-infra, but engineering is centered in Israel with a small US footprint. Demote
  it if no US role materializes.
- **Known name collision:** `ashby/moonshot-ai` is an unrelated NYC startup, not 月之暗面.
  Live jobs, wrong company — `ashby/cedar` again.

Three traps recorded while adding tier 7 (2026-07-22):

- **Lever slugs are case-sensitive.** Onehouse is `lever/Onehouse`; `lever/onehouse` 404s.
- **`lakeFS` acquired `DVC`** — one company, one entry. DVC's original company, Iterative,
  rebranded to **DataChain**, and `iterative.ai/careers` 302s to a 404.
- **`Delta Lake` is a Databricks project**, not a company — Databricks is already Tier 1.
  Adding a project as a company is the duplicate-target version of the `ashby/cedar` trap.

**Validated:** all 62 api slugs resolve, return parseable JSON, and belong to the intended
company. 15 entries were corrected in the 2026-07-09 audit — see the bucketed commits after
the `pre-audit baseline` commit.

**Valid but currently empty** — correct slugs, genuinely zero open reqs. Do not "fix":

- `greenhouse/root` (Root Insurance)
- ~~`greenhouse/dbtlabsinc` (dbt Labs)~~ — **no longer true as of 2026-08-03.** The board
  404s, so this is `FETCH_FAILED`, not empty: the slug is gone, not idle. `repair` finds
  nothing, because `getdbt.com/about-us/careers` is a 1.4 MB JS shell that never mentions
  Greenhouse — the documented blind spot. Needs a slug read off the rendered page by hand.
  Until then it is a legitimate `EXIT_DEGRADED` every night, not a permanent-empty exemption.

**Matching has run, and `resolve` has drained the queue (2026-07-25).** Verdicts in
`state.db` by who decided them:

| Verdict | rules | llm | Meaning |
|---|---|---|---|
| `reject` | 7,084 | 572 | A rule fired, or the model read the description as not-entry. |
| `uncertain` | 867 | — | No level token in the title *and* not engineering-signal (the model's queue is scoped to engineering titles; the rest correctly park). |
| `match` | 30 | 99 | Open matches, not run totals. |

`resolve` was a silent no-op until the 2026-07-24 `response_format` fix; its first real run
considered 670 engineering-signal uncertains and settled 665 (99 → match, 566 → reject). The
backlog fell 1,538 → 867 — the remainder is the non-engineering tail `looks_engineering()`
deliberately leaves alone, not a review pile.

**Then `check` erased all of it (found and fixed 2026-08-02).** 0 of the 99 `llm/match`
verdicts survived and 570 of 572 `llm/reject` were gone; the backlog was back to 1,742.
`cmd_check` re-derives a rules verdict from the *title* for every posting it fetches, and the
title is exactly what the rules already found insufficient — so an unpinned llm verdict lasted
one night. Human verdicts were safe because `overrides` pins them. The fix: `resolve` pins
too, with `overrides.decided_by` keeping the two apart. A model pass may never displace a
human ruling (`set_override` returns False); the reverse is allowed. **Re-run `resolve` to
restore the queue.** "Run `check` then `resolve`" is no longer load-bearing but is still the
right order: `resolve` should see the night's new postings.

The first check run reported 129 matches, mostly noise; the engineering-gate requirement (a
level token alone is not enough — see `match.py`) cut it. That is why `role_type_include` and
`engineering_terms` are separate lists.

The **aggregator** feed adds ~318 postings on the next `check` (151 rules-matches across many
employers, some not otherwise tracked). Validated on a scratch copy 2026-07-25; not in the
live DB because it enters via `check`.

**Still leaking, as of 2026-07-23.** The engineering gate can be satisfied by a
non-engineering role: Stripe's *"Seller Systems Operations Associate (Night Shift)"* matches
on `level:associate+role:systems`, because `systems` in `role_type_include` fires on an
operations title — the `Finance Associate` bug through a different door. Fix with the tuning
loop and a regression check, not a bare YAML edit.

**The model passes became tasks (2026-08-13).** `resolve` and `rank`'s judging phase are now
`level` and `judge` behind `jobtracker work`, joined by `inbox` (2026-08-16). Both old
commands still work — `resolve` is literally `work --task level`. Transport moved to the
`sir-client` SDK and the `Provider` registry was deleted. See `docs/tasks.md` and
`docs/prefill.md`; the schema gained `task_attempts`, `form_fields`, `prefill_gaps`,
`prefill_plans`.

`prefill` was a fourth task from 2026-08-13 to **2026-08-25**, when its model pass was deleted
and it left the queue for `jobtracker prefill`. What it decided is in DESIGN.md §8.1; what a
database that ran it needs is `jobtracker forget-learned`.

**Not yet done:**

- **The tracker markdown is a stale mirror.** `status`, `last_checked` and
  `last_posting_seen` are untouched across all 99 entries — v2 keeps run state in `state.db`
  and never writes back. Don't read the markdown to learn what happened; query `state.db` or
  open the dashboard.
- The Ouckah/CVrve aggregator is unwired — its 2026/2027 repo URL is unconfirmed (2025
  archived, 2026 404s), so its entry has no `board_url` and is skipped. Simplify **is** wired
  and fetched (2026-07-25) — see "Aggregator sources".

**Doc bug, unfixed (found 2026-08-02).** Three places — this file, `docs/llm.md`,
`resolve.py`'s docstring — claim *"Member of Technical Staff"* stays UNCERTAIN as a known
blind spot. It does not: `staff` is in `exclude_titles`, so `match()` rejects it outright long
before `resolve` could look. The existing test only asserts `looks_engineering()` is False,
which is true and a different claim. Either make the title reachable or stop saying it is —
decide with `jobtracker eval`, not a bare YAML edit.

---
## Operational notes

- Fetching `boards-api.greenhouse.io` ~12-way parallel gets egress throttled; curl then
  returns `http=000` for *every* host, including Lever and Ashby. It looks exactly like mass
  breakage. Pace requests and re-check sequentially before believing a failure.
- Drop `?content=true` when you only need job counts. Full-content payloads can blow a 20s
  timeout on big boards (Databricks carries ~787 reqs).

### Observability

Progress goes to **stderr** via `logging`; the report goes to **stdout**, so
`check > out.md` stays clean and a run is watchable live.

- INFO by default: one line per board as it lands (`[12/56] Stripe  518 jobs (1.1s)`), plus
  a phase line for health/match and for report rendering. `-v` → DEBUG, one line per HTTP
  attempt. `-q` → warnings and errors only.
- **Log format follows the terminal.** A TTY gets the human lines; a pipe or container gets
  one JSON object per line — same content, `extra={...}` fields promoted to top-level keys,
  UTC-offset timestamp. Override with `JOBTRACKER_LOG_FORMAT=json|text|auto` (default
  `auto`, keyed off `stderr.isatty()`). Still stderr-only. The JSON formatter is hand-rolled
  in `cli.py` — no dependency, same rule that keeps a web framework out of `server.py`.
- `fetch_all` uses `as_completed` so lines appear as boards finish, but reassembles results
  into **input order** before returning. Downstream stays reproducible — don't "simplify"
  this back to `pool.map`.
- Retries are logged, including ones that *succeed* (`recovered on attempt 2/3`). A board
  that quietly needs two tries every day is degrading; silence used to hide that.
- The end-of-fetch summary reports failures, retry count, and **cumulative time asleep in
  the per-host limiter** — the fast way to tell pacing from breakage. A healthy 56-board run
  is ~29s wall with ~106s of pacing summed across 4 workers, i.e. essentially all deliberate
  spacing on `boards-api.greenhouse.io`. Near-zero pacing plus a fast run means something
  returned early, not that things got faster.

### OpenTelemetry

Off by default. `--telemetry console` prints spans to **stderr**; `--telemetry otlp` ships
them to `$OTEL_EXPORTER_OTLP_ENDPOINT`. Env equivalent: `$JOBTRACKER_TELEMETRY`.

- `jobtracker/telemetry.py` is the **only** file allowed to import `opentelemetry.sdk`.
  Instrumented modules import `opentelemetry.api` and nothing else — with no provider
  configured the API is a no-op, which is why `fetch.py` has no enablement checks.
- `fetch.py` is instrumented: `fetch.all` → `fetch.company` → `http.request` →
  (auto-instrumented) `GET`. Retries are span events, not extra spans.
- Worker threads do **not** inherit OTel context. `fetch_all` captures it and `_fetch_timed`
  attaches/detaches it. Remove that and you get 56 orphan traces.
- Span names stay low-cardinality (`fetch.company`, never the company name). Identifiers
  belong in attributes. Never attach posting IDs or URLs — 8k postings per run.

### Metrics and the tier-3 stack

`compose.yaml` + `otel/` run collector → Jaeger (traces) + Prometheus (metrics) + Grafana.
`otel/stack.sh {up|down|run}` does the same with plain podman, since this machine has no
compose provider installed.

Two decisions exist *because this is a batch job*, and both break silently if undone:

- **Metrics are pushed, never scraped.** A 30-second daily process is essentially never
  running when a scrape interval elapses. The collector remote-writes into Prometheus
  (`--web.enable-remote-write-receiver`); nothing scrapes jobtracker.
- **Counters are exported as DELTA, not cumulative** (`_delta_temporality()`) and
  reassembled by the collector's `deltatocumulative` processor. Cumulative means "total
  since process start", which resets to zero every run and makes a backend read restarts as
  decreases. Verified: two consecutive runs of 60 new postings report 60 then 120.
- **`service.instance.id` is pinned to the hostname.** The SDK defaults it to a random UUID
  per process, which would mint a fresh Prometheus series every night.

Metric names arrive in Prometheus with dots → underscores, unit suffixes appended, and
`_total` on counters: `jobtracker.fetch.duration` becomes
`jobtracker_fetch_duration_seconds_bucket`, attribute `health.status` becomes label
`health_status`.

Keep metric attributes bounded — `ats` (4 values) and `outcome` (2) are fine; `company` (56)
belongs in traces, not metrics.

**Containerized runs MUST set `JOBTRACKER_INSTANCE_ID`.** `telemetry.py` pins
`service.instance.id` to `os.uname().nodename` so a daily job keeps one continuous series.
Inside a container that nodename is the *container ID*, new on every `--rm` run — so the
pinning silently does the opposite of its intent. `otel/stack.sh run` passes the host's
name; any new invocation path (cron, compose, CI) has to do the same.

### Querying: the counters are cumulative

`deltatocumulative` means Prometheus sees monotonic counters, so **`last_over_time` on a
counter is the all-time total, not last night's run.** Use `increase(...[24h])` for "what
happened in the last day", and `increase(sum)/increase(count)` for a histogram average.
`increase()` cannot see a rise preceding the series' first sample, so a freshly wiped
Prometheus reads 0 on day one and self-corrects on day two.

"How long since the last run" is the one query that catches a job that stopped, and the
obvious idiom is silently broken — `time() - timestamp(last_over_time(...))` always returns
0, because `last_over_time` re-stamps at evaluation time. The working form is
`time() - max_over_time(timestamp(jobtracker_run_duration_seconds_count)[24h:1m])`.

### The Grafana dashboard

`otel/grafana-dashboard.json`, provisioned by `otel/grafana-dashboards.yml`. The file is the
source of truth; `allowUiUpdates: true` permits live experimentation but a restart discards
anything not saved back. Datasource `uid`s in `otel/grafana-datasources.yml` are pinned
(`prometheus`, `jaeger`) because the dashboard binds by uid — remove them and every panel
reads "Datasource not found".

**Panel descriptions carry the why** — facts like "dbt Labs and Root Insurance are
legitimately empty, do not fix them", so someone reading `suspect_empty: 2` at 2am doesn't
repair two healthy boards. Keep them current when the reasoning changes.

## The HTML dashboard

`jobtracker dashboard` renders `state.db` to a single self-contained HTML file
(`data/dashboard.html`, gitignored). Grafana watches the *pipeline*; this watches the *job
search* — open matches, the uncertain backlog, flagged boards, and the manual companies that
are never scraped.

**Four tabs, and Today is the landing screen** (2026-08-02; Applications added 2026-08-16).
The page opens on the three jobs to apply to, each with the model's one-line reasoning and an
Apply link; the old front page is "All postings" and board health is "Boards". The point is to
shorten the distance between opening the page and applying. "Applications" sits second: work
already committed to outranks the raw corpus.

- **Panels are server-rendered; the script only toggles `[hidden]`.** `.tabs` is
  `display:none` until JS confirms it is running, so with JS off every panel shows stacked.
  Same rule as the row filters.
- **The postings tables group by company** (2026-08-16): one `<tbody class="grp">` per
  company, headed by a `<tr class="cohead">` carrying the tier chip, name, role count and a
  `.cotoggle` button. Four rules, all load-bearing:
  - **Rows render visible and JS collapses on load** — never the reverse. `.cotoggle` is
    `display:none` until the script adds `js-groups`: with JS off you get every row and no
    dead control in a mailed file.
  - **Filtering owns `hidden`; collapsing owns `tbody.closed`.** Two owners of one property
    is how they drift.
  - **Any active filter force-expands the matching groups**, with `data-closed` remembering
    your choice for when the filters clear. A collapsed page under a typed search reads as
    "nothing found", which this page may never say while holding rows.
  - **A data row is one carrying `data-search`.** Group heads do not, keeping them out of "N
    of M shown" — that number means postings. The filter IIFE was rewritten off
    `t.tBodies[0].rows`, which with one tbody per company filtered only the first group.
    Tier/company cells moved into the head but stay in the row's search blob, or searching a
    company name stops matching its own rows.
- **Today has a `<details>` drawer for the rest of the ranking** (2026-08-16), grouped by
  company, numbered with the real rank. `<details>` because it opens with no script and is not
  a table the filter JS could reach. Built from `rank.available(...)[3:]` — **never** raw
  `ranked_matches`, or a job you applied to this morning reappears on the page it left. **No
  buttons in it, in either mode**: a pick is what has buttons, which keeps `.pick [data-act]`
  meaning exactly three cards.
- **The picks must never be a `table[data-filterable]`.** The filter JS selects those, so a
  tier or location filter set on another tab would silently empty a curated list. Tested.
- **Disposition buttons only exist under `serve`** (`build_dashboard(interactive=True)`).
  They POST, and the static file stays offline and read-only — a dead button in a mailed file
  is worse than no button. "Open prefilled" likewise, and more so: it drives a browser, which
  only a live process can do. The *counts* (`prefill 13/16 fields · 3 need you`) render in
  both — offline they say whether opening a job takes thirty seconds or ten minutes.
- **And the script it lives in has to parse.** `server._JS` carried three `'\n'` sequences in
  a non-raw Python string (2026-08-19 to 2026-08-26), putting a real newline inside a quoted JS
  literal. A `SyntaxError` kills the whole script, so **every** handler it carries was dead on
  all four pages emitting it: `/tuning`'s rule controls, Settings' answer saves, Applications'
  status buttons, the Add a company form — no symptom, because a page whose script never ran
  still renders perfectly. In a `"""` block, `\n` is a real newline and `\\n` emits the two
  characters JavaScript wants. Parity tests assert a handler was *written*, not that it parses;
  `test_no_emitted_script_carries_a_newline_inside_a_string` is the one that can see it.
- **A button's handler lives in the file that renders the button.** "Open prefilled" was
  emitted by `dashboard.py` while its handler sat in `server._JS`, which only `/tuning` and
  `/settings` emit — so every click silently did nothing (fixed 2026-08-15). `server._JS` and
  `dashboard._JS` are two scripts on three pages. Tests assert the button exists *and* that
  the page's script listens for it.
- **`serve` has a third page, `/settings`** — who you are, your resume, the answer bank, and
  every question prefill could not answer. `render_settings` is connection-in/string-out like
  `render_tuning`; `POST /api/answer`, `/api/attach`, `/api/identity` and `/api/resume` write
  through `safewrite`. `POST /api/apply-to` starts the browser **on a daemon thread**:
  `server.py` is `HTTPServer`, not `ThreadingHTTPServer`, so driving it inline would freeze the
  page for as long as the window stayed open.
- **Nothing on that daemon thread can answer the click that started it,** so everything
  knowable first — the posting, the answer bank, whether Playwright imports, whether a window is
  already open — is checked *before* the endpoint returns. An exception on the thread reaches
  the log and nowhere else, which on a box with no Playwright is a button stuck on "Opening…"
  over a browser that never opens. One window at a time, because Chromium locks the one profile
  directory (`_APPLY_LOCK`).
- **The window has to be waited on, or it vanishes.** `fill_application` closes the context on
  the way out, so `wait=True` (the CLI's Enter prompt) and `hold=True` (`serve`'s
  block-until-closed) are the only two ways a human gets the window; `wait=False` fills the form
  and shuts the browser, which reads as "the button does not work". And waiting means waiting
  *inside* a Playwright call — the sync API dispatches driver events only while one is in
  flight, so `time.sleep` plus a look at `context.pages` reads a frozen snapshot: a killed
  browser still showed one open page forever and the hold thread never returned, pinning
  `_APPLY_LOCK` until `serve` restarted. `_hold_until_closed` waits in
  `page.wait_for_timeout(500)`; both endings then arrive as an empty page list or
  `TargetClosedError`. Measured 2026-08-16; a test asserts the call is still there.
- **A launch failure has to say why it failed** (2026-08-29). `_launch` tries `chrome` then
  bundled Chromium and used to log both exceptions at debug and raise a fixed *"no browser to
  drive… `playwright install chromium`"*. Twice in two days that named the wrong cause: once
  `$DISPLAY` pointed at a dead X server, once the profile held a `SingletonLock` naming a
  recreated container. `_why_no_browser` carries the launcher's own first line into the
  exception and separates *missing* (install something) from *would not start* (check the
  display and the profile lock). Debug logging cannot substitute — this runs on `serve`'s daemon
  thread, where the exception is the only thing reaching a human.
- **The browser opens on the machine running `serve`, not the machine viewing the page**, and
  `DISPLAY`/Xvfb stay: Chromium will not launch headful without a display, and headless is a
  different bot-detection posture. `config.BROWSER_VIEW_URL` is the fallback route to that
  window and renders **View window ↗** on `/apply` only — never on the Today card, and
  `_safe_url`-checked like every third-party href. See "The mirrored form" for why it was
  deleted on 2026-08-22 and restored on 2026-08-29.
- **"Open application" on `/apply` is not that link** (2026-08-26). It opens the *form* —
  `Session.url`, the page the browser landed on — in a tab of your own browser, for reading what
  the discovery pass could not mirror. It reaches neither the window nor the host's display, and
  typing in it changes nothing here: two tabs on one anonymous form share no draft, the same
  fact that makes this a browser rather than a link. Scheme-checked like every third-party URL.
- **The resume upload is base64 inside JSON, not multipart.** It reuses the one POST path this
  server has and keeps `form-action 'none'` in the CSP meaningful — the page never submits a
  form, it fetches. `MAX_UPLOAD` is per-route, so a decision POST cannot buffer 6 MB.
  Hand-rolling a multipart parser to save a 33% wire overhead on a file you upload once is the
  wrong trade, and the same rule that keeps a web framework out of `server.py`.
- **It is a pure read.** Unlike `report`, it never marks manual companies as surfaced. Tested.
- **No network at view time, ever.** No CDN, no chart library — the one chart is CSS. That is
  what makes the file mailable and openable offline years from now.
- **Rows render server-side; JS only hides them.** With JS off you still get every posting.
  Don't "improve" this into client-side rendering from embedded JSON.
- **Escape everything.** Titles and locations come from third-party ATS APIs. URLs get a
  scheme check too — a `javascript:` href would execute on click.
- Tier color is **three bands, not seven steps** (T1–T2 anchor, T3–T5 applied, T6–T7
  research). Seven steps do not fit the blue ramp's usable range; the tier number is always
  printed, so color is reinforcement, never the encoding.
- **Location sorts, never filters.** Rows come out NYC-first (`match.location_rank()`), NYC
  rows carry a pin, and the location dropdown defaults to "Anywhere". A location filter the
  user did not choose would silently hide roles they asked to see.

## Applications: the outer loop

`jobtracker applications`, `/applications` under `serve`, and a read-only fourth tab in the
static dashboard. Full guide in `docs/applications.md`. Added 2026-08-16.

The table was already there and had **three writers and no reader** — `all_applications` had
no production caller — while all three writers began with `SELECT ... FROM postings` and
bailed when there was no row, so a referral could not be recorded at all. What is new is the
reading, the history, and manual entry.

- **Two writers, deliberately.** `record_application` sets state and logs nothing;
  `add_application_event` appends and changes nothing; `advance_application` does both and is
  what "something happened" calls. Folding the append into the upsert is wrong in both
  directions — editing a note would duplicate an event, *or* a second interview round would be
  suppressed as a no-op, and from inside an upsert those look identical. Rescheduling a
  reminder is `record_application` alone.
- **`interview` is one repeatable status, not numbered rounds.** `round_1/round_2/onsite`
  caps the enum at however many rounds you guessed. Rounds are repeated events with notes;
  counting them renders `interview ×3`. `interviewing` is gone — the live table had zero rows.
- **There is no `ghosted`.** Silence is derived from `updated_at` (30 days). A status only the
  user can set is one they will not remember to set, and an unset "no reply" is
  indistinguishable from an application going well.
- **Optional columns update through `COALESCE(excluded.col, applications.col)`.** Not style —
  `jobtracker apply` passes none of them, so a plain assignment blanks a URL set from the web
  page on the next status change. Same rule as `sync_postings` and `posted_on`. At the API
  layer: absent/null = leave it, `""` = clear it.
- **`source` is nullable and read as `COALESCE(source, 'tracked')`.** A `NOT NULL DEFAULT`
  fires only when the INSERT *omits* the column, and this one is always bound — so it rejected
  every caller passing None. Measured: it failed five tests.
- **Manual ids are minted and deterministic** (`manual:<slug of title>`), so re-adding the
  same role updates rather than duplicating — the aggregator's stable-id rule. The prefix makes
  collision with a real `ats_job_id` impossible. Manual rows have no `postings` row, which is
  why `all_applications` reads the table directly; join it and they vanish.
- **The static tab has no buttons and no `interactive` flag.** Every control is emitted by
  `server.render_applications`, every handler is a branch in `server._JS`. A test asserts the
  rendered button set and the handler set match exactly.
- **The applications panel is never a `table[data-filterable]`** — same trap as the picks,
  with its own test.
- **A refused write writes nothing.** Bad status, unparseable date, `javascript:` URL, missing
  title: `{"ok": false}` at HTTP 200, both tables untouched. A date that does not parse is a
  refusal, never stored raw — text collated against ISO dates would never come due, inside the
  one feature whose job is to remind you.
- **`rank.is_available` excludes on the presence of a status, not on which one**, and must
  stay that way: a rejection does not put the job back in tomorrow's top 3. A test walks all
  seven.
- **`applied_at`/`updated_at` are timestamps; `next_action` is a day.** `date.fromisoformat`
  rejects a timestamp outright and *silently* — the row just reports no age — so every
  comparison goes through `applications.day_of`. `days_since` returns None, never 0, and an
  unreadable `updated_at` sorts last rather than first.

## Reading the mailbox

`jobtracker mail`, the `inbox` task, and a review list on `/applications`. Full guide in
`docs/mail.md`. Added 2026-08-16. The **fourth** bounded model role (DESIGN.md §8 — fifth of
five until question matching was removed on 2026-08-25) and the first thing other than the
user to touch the outer loop, which is why it may only propose.

- **`mail` is to `inbox` what `check` is to `level`.** The deterministic pass does the I/O and
  caches into `mail_candidates`; the task is a pure read of `state.db` whose only socket is the
  router. Do not give the task a mailbox — an unmounted volume would then silently shrink a
  queue that was already recorded.
- **Nothing writes to the maildir.** `mailbox.Maildir(path, factory=None, create=False)` —
  `create` defaults to **True**, so a typo'd `$JOBTRACKER_MAILDIR` would make directories
  inside the user's mail store. `keys()` + `get_bytes()` and nothing else; no flags, no renames,
  not `get_message`. Two tests: one on the source text, one on a filesystem snapshot.
- **`Message-ID` is the identity; the maildir filename is not.** A client renames `1234.host`
  to `1234.host:2,S` the moment you read the message, so a filename key would re-propose the
  whole inbox every time you opened your mail. No header → `synth:<digest>`, the
  `manual_job_id` fallback.
- **The narrower is built from `applications`,** so a message can never be a candidate for a
  company you never applied to — the shape of the data, not a rule. Domains are *read* off URLs
  you applied at; synthesizing `stripe.com` from "Stripe" is `ashby/cedar` again. ATS relay
  domains identify the ATS, never the company. Names match on whole tokens ("Ramp" ≠ "Rampart")
  and only in the From display name; a name in the *subject* additionally needs an
  application-shaped word, or every digest naming a tracked employer becomes a candidate
  (measured against a real newsletter). A name in the body alone is never enough.
- **An unresolved job is asked about, not guessed.** `ats_job_id=''` plus a `choices` list; the
  card renders a dropdown and the endpoint refuses until one is picked.
- **`read_at` NULL is the queue**, and non-NULL with no proposal means "read, and not
  application news" — the NULL-vs-`''` distinction `postings.description` draws. Rejected
  messages are deliberately **not** stored, which lets a message predating the application
  become a candidate on the next scan.
- **"Nothing here" is an answer and is written**, unlike `level`'s `unclear`. Copying `level`
  would spend three calls on every newsletter and fill the blocked-unit count — which exists to
  signal breakage — with healthy readings. Only a transport failure leaves a message unread.
- **The model's quote must appear in the message verbatim.** It is the only free text it
  produces; grounding it is `repair`'s "a slug must appear on the page it was shown", and it is
  what keeps a fabricated rejection off the list.
- **`unit_key` is the message id.** Two ambiguous messages at one company both carry
  `ats_job_id=''`, so without it they share an `ident`, `task_attempts` charges one's failures
  to the other, and the router collapses two questions onto one answer.
- **Accepting is the only path into `applications`,** and the event note is composed by Python
  — the model's quote stays in `mail_proposals.evidence`. A refusal writes nothing. Dismissed
  is a resolution, never a delete; deleting would let the next scan re-propose it.
- **Priority 40 is a starvation argument, not a dependency.** Nothing in the chain consumes
  what `inbox` produces.
- **No scan endpoint, ever.** Walking a mailbox on a single-threaded `HTTPServer` blocks every
  other request and makes a page render a writer. A test asserts `server.py` imports neither
  `maildir` nor `mailbox`.
- **The banner is a derived count, not a `seen` flag.** A flag would have to be written by a
  GET (breaking `test_the_page_is_a_pure_read`) or by JS (which never fires with JS off). The
  acknowledgement is accepting or dismissing, not glancing.
- **`state.db` holds the text of personal mail.** Gitignored and local; no log line or span
  attribute may carry a subject or a body.

## Import plugins

`jobtracker plugins`, `jobtracker/plugins/`, and one extra loop in `cmd_check`. Full guide in
`docs/plugins.md`. Added 2026-08-31. Discord is the first, and the third registry in this
package after `sources/` and `tasks/` — one module plus one import line, module pure,
`runner.py` owns the socket.

**A plugin has a `kind` since 2026-09-01, and there are two.** `import` is everything below —
a feed of postings. `task` is the switch for a bounded model role in `tasks/`; it implements
*nothing* and has no `page_url`/cursor/`parse_page` (genuinely absent, not stubbed, so the
paging loop fails at the boundary rather than three layers into a request). The role stays
implemented in `tasks/` like the ones that are not switchable, and `plugins/roles.py` derives
one switch per registered task — so putting a role on the switchboard is a change to a set of
names, not a module.

- **`plugins/` may import `tasks/`; `tasks/` may not import `plugins/`.** `survey()` takes the
  enabled set as an argument, so a task module stays pure and cannot tell whether it is
  switchable, and the queue never depends on what is on disk. Tested off the source.
- **A switched-off task is absent from the survey, not unavailable.** Switched off is a
  decision you typed; a reason printed beside it reads as a fault. `work --task <off>` says so
  and exits **0** — falling through prints "every task is drained", which is false.
- **The switch comes before the query.** A disabled task is never asked for
  `unavailable_reason` and never asked for `pending()`, or a machine that turned a role off
  still pays for its backlog nightly.
- **`level`, `judge` and `inbox` default to on**; new plugins default to off. Adding the
  switch was not meant to change anyone's queue; a test asserts an existing plugins.yaml loads
  key for key.
- **`purge` is import-only.** It removes postings a feed imported; a model role imports
  nothing, and the postings it writes proposals *about* belong to whichever board owns them.
- **Each plugin declares its own settings** (`defaults()`), and the type of a setting is the
  type of its default. One flat global `DEFAULTS` made every plugin's config surface the union
  of all of them — `channel_id` was a valid setting on a model role, `.isdigit()`-validated as
  one. Semantic rules live in `validate()` on the owning plugin, and **`coerce` runs
  `validate` too**, or `backfill_days=-3` degrades from a refusal at the prompt to a
  `RefusedWrite` three layers down.
- **A cursor is described by the plugin that minted it** (`describe_cursor`). `plugins list`
  decoded every plugin's cursor with Discord's snowflake decoder, imported directly into the
  CLI — invisible with one feed, a confidently wrong date for the second.

- **A feed is not a board, and that is why this package exists.** `sync_postings` closes every
  posting absent from a fetch, rightly: a board is a *complete statement* of what a company has
  open. A poll returns only what arrived since the last read, so on a normal night it returns
  nothing — routed through `sync_postings`, one quiet evening would close every posting the
  feed ever imported. `store.append_postings` never closes by absence. There is a test named
  after it. Discord could not have been a fifth `Source`.
- **`append_postings` writes `description` at insert, and that is not an optimization.**
  Nothing else would write it: `_cache_descriptions` builds `wanted` inside `cmd_check`'s board
  loop and a plugin group is not in it. A NULL description drops the row out of
  `level.pending()` **and** out of `store.matches_needing_judgment` (`AND p.description IS NOT
  NULL AND p.description != ''`) — never judged, never scored, filtered out of
  `rank.available()`: present in the table, absent from the product. Plugin postings are also
  kept **out** of `wanted`, because that pass's "no detail endpoint" branch writes `''` through
  a bare UPDATE and would erase the text.
- **A verdict is recorded for every plugin posting.** Every downstream query is `postings JOIN
  verdicts`; without one the posting is invisible everywhere.
- **Postings close by age, never by absence** (`expire_after_days`, default 90). A channel
  cannot report that a req was filled. Age is honest because it is a statement about *our
  observation*, not an inferred claim about the employer. The better mechanism (check the
  linked ATS board and close what is gone) is a documented follow-up, not a rejected idea.
- **`health.evaluate_plugin` can never return `SUSPECT_EMPTY` for a routine poll**, and it
  lives in `health.py` rather than the CLI because a second health policy in `cli.py` is what
  that module exists to prevent. §7.1 reads an empty board as suspect because a board is a
  complete statement; a channel poll is not. Flagging it would put the feed on the Boards tab
  every night (the dbt Labs mistake) and make the night the token expires look identical to
  every healthy night. §7.3 is untouched — a 401 is FETCH_FAILED, streaks, and degrades the run.
- **The one exception is an empty *first* read.** A backfill reaching back two weeks that finds
  nothing is not a quiet channel; on Discord it is very likely a missing Read Message History
  permission, which answers **200 with `[]`, not 403**.
- **Discord has two independent `greenhouse/hubspot`s.** The permission above, and a missing
  MESSAGE CONTENT intent — which returns every message correctly authored with `content`,
  `embeds` and `attachments` blank, so every format declines and a channel full of jobs reports
  zero. `page_error` flags a page where *no* message has content; one blank message is ordinary
  (an attachment-only post), a whole page is configuration. The intent is listed as a *gateway*
  intent and this code opens no gateway; it gates the REST payload anyway.
- **A failed read never advances the cursor**, and **the cursor advances past messages we
  deliberately skipped.** Both halves matter and pull opposite ways: stamping on failure loses
  that window silently, while moving only for imported postings stalls forever on a channel
  whose recent traffic is all conversation. `page_cursor` reads the **raw** page for the same
  reason.
- **A plugin's group is never curation.** Not written to `companies.yaml`, never joined onto a
  `load_companies` result. Verified harmless: `report.py:68,120`, `dashboard.py:1198,1267,1580`
  and `server._companies` all use `.get` and degrade to tier `—`. The absence is load-bearing
  in one place — `repair.detect` skips companies it cannot find, which stops a failing feed
  sending the slug-repair agent to scrape a Discord careers page.
- **`purge` keeps `decisions`, `overrides`, `applications` and `application_events`**, and says
  so. The first is the corpus `eval` replays — `decisions.title` is denormalized precisely so it
  does not shrink — and "I applied here" stays true whatever happens to the posting row. Dry by
  default, `--write` applies.
- **This is the repo's first credential.** `$JOBTRACKER_DISCORD_TOKEN`, env only: never
  `plugins.yaml` (a config file gets pasted into issues), never a build ARG (`docker history`),
  never in `extra={}` (the JSON formatter promotes those to top-level), **never a query
  parameter** (`_request` records `url.full` as a span attribute and logs the URL on every
  retry), and never echoed by `plugins list`. Header only, per-request — a *session* header
  would carry it to every board in `companies.yaml`. Two tests.
- **`plugins.yaml` is curation, `plugin_state` is observation** (DESIGN.md §3.3). The enabled
  flag is a decision you made; the cursor is what a run found out. `load_settings` is strict,
  because a config format nothing validates is a comment (§2.1) — and a malformed file **stops
  the feed** rather than reading as "no plugins", which would turn a typo into a feed that
  silently stopped importing.
- **Formats are their own registry** (`plugins/discord/formats/`), ordered by a declared
  `fallback` flag rather than import order. A format returns `None` to fall through; the
  dispatcher catches exceptions anyway, because "never raises" is a promise one malformed date
  breaks inside `strptime`, and the cost is a poll that dies mid-channel. `flatten` renders an
  embed's title+url back as `## [title](url)`, so a markdown format keeps working when a bot
  switches to embeds.
- **`generic` never guesses an employer.** A guessed employer becomes a company name in a
  tracker whose discipline is that identity is verified, not inferred.
- **Sponsorship rides in the description and goes no further.** A criteria token would be a
  gate applied before any title is read — the `locations_exclude` mistake — and in the title it
  would collide with `clearance` in `exclude_titles`.
- **`prepare` gained a third outcome** (same change, own commit): a pick whose source could
  never publish or learn a form is reported as "apply on the employer's own page" and does not
  count against `ready`. Without it `prepare` exits 2 every night a feed posting reaches the top
  three. **It was already latent for aggregator postings**; `Aggregator.application_form_url`
  returns None too.

## URL dedupe

`jobtracker/dedupe.py`, three columns on `postings`, and two calls in `cmd_check`. Full guide
in `docs/dedupe.md`. Added 2026-08-31, and it runs across **every** source, not just plugins.

- **The key is not the URL string.** ATS identity is extracted from the path first
  (`lever:artera-2:<uuid>`), and **before the query is dropped** — Greenhouse's
  `embed/job_app?for=X&token=Y`, which `browser.py` mints, carries its identity in the query.
  Normalized URL is the fallback.
- **`key_from_identity` is the primary rule for api rows**, and it closes the big hole. On the
  live DB, of 9,150 postings only 3,487 are on their own ATS's host — the other 5,663 link to
  careers sites, because 25 of 45 Greenhouse boards return one there. Keyed off the URL, most
  of the corpus would be unreachable by a feed link. Where both derivations apply they agree on
  3,487 rows and disagree on none.
- **The path is not lowercased** (paths are case-sensitive, and `lever/Onehouse` vs
  `lever/onehouse` is a live trap) **but the slug and id inside an ATS key are** — those are
  case-insensitive identifiers, and there the failure runs the other way.
- **`gh_jid` is identity and must survive normalization.** It is on 6,019 stored URLs and
  equals `ats_job_id` on all 6,403 carrying it, none differing. Drop it and every board that
  links its reqs to one careers page collapses: **13 keys covered 2,805 open postings** before
  this was fixed — 795 Databricks, 527 Stripe, 400 MongoDB, 41 Betterment at byte-identical
  URLs. `t` (holding `gh_src`) and `utm_*` are tracking and are dropped.
- **Precedence is the safety argument, and the rule is "only a feed is closed by a peer", not
  "unless it is api".** An uncurated company has no `check_method`; ranking it below a feed
  would make *forgetting an entry in companies.yaml* a way to close live rows. Measured: the
  pass against a deliberately empty companies file closed 795 Databricks postings, which had
  lost their identity key and their rank together. So unknown loses to a board, outranks every
  feed, and is never peer-closed; two board rows sharing a key are logged at WARNING with
  neither touched — the only way a too-coarse key becomes visible, since nothing is closed.
- **The index cannot live in `_SCHEMA`.** `connect()` runs `executescript(_SCHEMA)` *before*
  `_apply_column_migrations`, so an index on `postings(dedupe_key)` there raises `no such
  column` on any pre-existing database — and passes on every freshly built one, which is every
  database in the test suite. `_ADDED_INDEXES` is applied after the columns; a test drops the
  column and reconnects.
- **`sync_postings` must not reopen a dedupe closure.** It resets `closed_at` on every re-seen
  posting, right for a board — but the duplicate's own feed still lists it tomorrow, so an
  unconditional reset undoes the closure at 01:00 and remakes it at 01:05, forever. The reopen
  is conditional on `closed_reason IS NULL`, i.e. on the closure having come from absence.
- **`dedupe_key` is a plain assignment, not COALESCE** like `posted_on` beside it. The key is
  derived from the same statement's URL, and a URL that moves must take its key with it —
  Greenhouse migrating an `absolute_url` to a careers page is documented and observed.
- **`close_duplicates` runs once per check, after every board has synced**, never inside the
  board loop. A shared key's winner can be fetched later in the same run than its loser, and
  boards are fetched in `companies.yaml` order — deciding per board would make which row
  survives depend on the ordering of a curated file.
- **The reason lives in `closed_reason`/`duplicate_of_url`, not in `verdicts`** (rewritten from
  the title every night — the mechanism that erased 99 llm matches on 2026-08-02) and not in
  `overrides` (which mean "I ruled on this role", while a duplicate is a fact about the *row*).
- **Blind spot, documented not hidden:** `simplify.jobs/p/<uuid>` shares no string and no job
  id with its redirect target, so a Simplify row and its direct twin do not dedupe. Two Simplify
  links do. A test asserts the miss. Workday is deliberately out of the URL extractor — its
  path is title-derived and does not follow a rename — and is covered on the api side by
  `key_from_identity`.
- **Measured before it was wired:** on the live corpus the pass derives 9,150 keys, closes
  **0** postings and reports **0** conflicts — and, after the two fixes above, closes 0 even
  when handed an empty companies.yaml, where the first attempt closed hundreds. The first run
  that does close things must say how many in the run log, or a legitimate cleanup reads as a
  regression at 2am.

## The tuning loop

`criteria.yaml` is easy to edit and hard to edit safely: a token added to stop one bad match
silently changes the verdict on thousands of postings already judged. Full guide in
`docs/tuning.md`; the rules that matter here:

- **Never hand-edit `criteria.yaml` without running `jobtracker eval`.** It replays the current
  rules against every recorded judgment and exits 1 on a regression — the only thing standing
  between "fixed one leak" and "silently re-broke three".
- **`uncertain` is not a regression.** Rules saying `uncertain` where a human said `match` is
  correct behaviour — the level genuinely is not in the title. Counting it as failure pushes
  toward rules that guess level from titles, the over-fitting the mechanism exists to prevent.
  Only active contradiction blocks.
- **Suggestions are string counting, not a model**, scoped to rejects the rules do *not*
  already handle so the list terminates. Do not "improve" this by feeding it to an LLM; the
  zero-in-accepted test is what keeps `engineer` and `software` out without a hand-maintained
  blocklist.
- **Overrides outrank rules** and survive rematch, carrying `decided_by='human'`. They are
  applied in the caller path (`cmd_check`, `cmd_rematch`, `serve`), never inside `match()` —
  that function's purity is load-bearing for the tests.
- **`/tuning` shows the five gating lists and can add to any of them** (2026-08-19). Before
  this the only rule control was one button hardwired to `exclude_titles`, and no list was ever
  displayed — so the one list that can clear a non-engineering title out of UNCERTAIN was
  unreachable from the UI. That is `role_type_exclude`, and the asymmetry is why the section
  exists: it is checked at **step 2, before the level gate**, so it applies to every title,
  while `exclude_titles` can only reject a title that already carries a level token. 55% of the
  1,537 open uncertains had no engineering word in the title when this was built.
  - **Location lists are deliberately absent.** They rank, they never gate; heading them
    "rules" beside the gating lists would advertise the geography filter removed on 2026-07-22.
    Tested.
  - **A suggestion may only target a *reject* list.** Suggestions are phrases mined from titles
    you rejected, so offering `engineering_terms` or `role_type_include` in that dropdown would
    let one click widen matching on exactly the titles the suggestion exists to remove.
    `_SUGGEST_TARGETS`, with a test.
  - **There is no delete control, and adding one is not a small feature.** Removing a token
    silently re-admits every posting it was rejecting; that path is an edit plus `jobtracker
    eval`, not a button that skips the regression replay the page exists to run. The section is
    a *reading* first — tokens render as chips, because an add box over an invisible list is
    how you re-add a token that is already there.
  - Both controls post to `/api/rule`, which already validated `list` against `_LIST_KEYS`;
    only the UI was hardcoded. Handlers live in `server._JS` beside the markup, with a parity
    test.
- **Rejects are kept, and the disk-space argument for dropping them is not real.** Measured
  2026-08-19: 7,522 rejects are ~1.06 MB of a 3.4 MB `state.db`, ~140 bytes each, and
  `_cache_descriptions` already excludes them from the only expensive path. Do not replace them
  with a hash set of "seen and rejected" postings: `cmd_check` re-derives `match()` over every
  posting in the fetch, which is what makes a `criteria.yaml` edit reclassify all of history
  with no backfill, and `eval` replays the rules against that same corpus. A hash cache pins
  today's rules including today's mistakes. The supported "never show me this again" is
  `overrides`, which pins per posting and survives rematch. The fetch unit is also the
  **board**, not the posting — one bulk call returns all N reqs, so filtering at ingest saves no
  network either.
- `decisions.title` is denormalized on purpose. Joining to `postings` would shrink the corpus
  every time a req closed, which is exactly when the evidence matters most.

## The task queue

`jobtracker work`, documented in `docs/tasks.md`. Added 2026-08-13, and where all model work
lives: `level` (was `resolve`), `judge` (was `rank`'s first phase), and `inbox` (2026-08-16).
The scheduler polls tasks by priority and runs the first with work.

**`prefill` was here too, at priority 30, and left on 2026-08-25** when its model pass was
deleted — not a rename. `cmd_work` returns early when no router is configured and `_work` bails
on a failed `probe()`, so a task only ever runs when a model is reachable: correct for the
three above, silently wrong for a pass that resolves a form against a YAML file. `prepare` had
that bug already — on a box with no GPU it built nothing and every pick reported "no plan",
the failure `prepare` exists to catch, produced by `prepare` itself. Same argument that has
always kept scoring out of the queue: no model, must always run.

- **`work` rescores after every run, and that is load-bearing.** `judge` writes a ranking with
  a NULL score, and both `today` and `prefill` only consider postings that have one, so without
  it a `work` loop drains level, drains judge, and leaves the score NULL until something else
  rescores. Scoring stays out of the queue (no model, must always run) but is still a link in
  the chain, so the runner closes it. A test walks one posting from uncertain to scored with
  `work` and then to prefilled with `prefill`.
- **`jobtracker prepare` is the nightly "is tomorrow useful?" check.** Rescore, take the
  postings `today` will surface, prefill exactly those, exit 2 if any has no plan. **Gaps never
  cause exit 2** — an unanswered question is the normal state and failing on it would make the
  unit permanently red for something only the user can clear, the same trap as flagging dbt
  Labs' empty board. Verified after the model came out, when the gap count went from 200 to
  1,900: still exit 0.
- **Priority is the pipeline's dependency chain, not a preference.** level → judge, because
  each produces what the next consumes. Reorder it and "work the next available task" stops
  meaning "keep every stage drained". Tested. **`inbox` (40) is outside that chain**: it
  consumes nothing the others produce, and is last on a starvation argument — its queue refills
  from an external stream, so anywhere earlier a chatty mailbox would keep the pipeline's own
  stages waiting. Also tested.
- **The queue is derived, never stored.** Each task's `pending()` is a SQL read over existing
  tables, so there is nothing to reconcile — a posting that closes overnight stops appearing.
  `task_attempts` is a *failure ledger*, not a queue: three consecutive failures set a unit
  aside so it stops eating the budget while the rest starves. Do not turn it into a work table.
  (`prefill` lost its ledger when it left; acceptable, since its only remaining failure is a
  form fetch, transient and retried nightly, and `fetch.py` already burns `MAX_RETRIES` inside
  the run.)
- **Every unit commits on its own.** The fix for a real defect — the old passes held everything
  until one commit at the end, so an interrupted run wrote nothing. A task that raises while
  writing is rolled back to the last committed unit. `prefill.build_plans` kept this when it
  left the queue; it is the one thing from the runner worth carrying out by hand.
- **`unit_key` is the question, not the posting.** `judge` carries the profile prose hash.
  Change the question and every unit is new with its retry count reset — correct, because a
  failure answering the old question says nothing about the new one. It is also the router's
  idempotency key.
- **`pending()` must only return work the task can actually do.** `level` excludes postings
  with no cached description. Counting what it cannot reach overstates a backlog no model could
  drain, and sends a budgeted run to guaranteed no-ops. `prefill.pending` keeps the rule outside
  the queue: a company whose form is neither held nor fetchable is waiting on a browser visit,
  not on this pass.
- Adding a task is one module plus one import line, same as an ATS. **Task modules are pure** —
  prompts, parsers, and a description of what to write; `runner.py` owns every socket,
  transaction, and clock.
- **A task can be switched off in plugins.yaml** (2026-09-01) — see "Import plugins".
  Enablement only, and it stays outside this package: `survey(conn, ctx, enabled=...)` takes the
  set as an argument, `enabled=None` means all of them, and `tasks/` never reads a config file.

## Tailoring a resume

`jobtracker/tasks/tailor.py`, `jobtracker/resume/`, and `jobtracker tailor build`. Full guide
in `docs/tailor.md`. Added 2026-09-01. **The fifth bounded model role (DESIGN.md §8) and the
first that composes prose** — so the bound is not the shape of the answer.

- **Your resume's source is LaTeX** (`$JOBTRACKER_RESUME_TEX`), which dissolved the
  dependency question rather than answering it. `resumes.RESUME_TYPES` is `.pdf`/`.docx` and
  there is no text extractor here; a `.tex` file is already text. What matters more than the
  missing dependency: the model quotes lines back, a PDF extractor's idea of a line is a
  column-layout accident, and the output is a diff.
- **Both anchors are verbatim quotes.** `evidence` must occur in the description and
  `current_line` in the resume — `inbox`'s quote rule at *both* ends: one keeps an invented
  requirement off a resume, the other keeps the page from attributing a line to you that you
  never wrote. Grounding is checked whitespace-normalized but `apply_edits` replaces exactly,
  so **an edit must pass both**; one passing the loose check and failing the exact one would
  render on the page and then do nothing when applied.
- **The LaTeX guard is a security control.** A resume is compiled, so a suggestion is a
  program about to be run: `\input` reads files, `\write`/`\openout` create them,
  `\catcode`/`\csname` rewrite what the source means, `\write18` runs a shell. An
  **allowlist** of control sequences, because a blocklist is a guess and `\csname` composes
  command names out of characters. It runs inside parsing, not at assembly — an edit that
  renders and is refused later is one you accept and watch do nothing.
- **`apply_edits` replaces a line it was handed verbatim, and does nothing else.** No search,
  no fuzzy match, no insertion — which puts the preamble out of reach by construction and is
  why a document that compiled before compiles after. The cost is real: it cannot add a bullet
  or reorder a section, because insertion has no anchor.
- **`resume_suggestions` has exactly one reader**, the page that shows it to you. Nothing
  joins it into prefill, ranking or matching. This is §8.1's finding, not an accident: the
  role removed there was *more* tightly bounded (an enum of keys, no free text) and was still
  wrong because its answers were cached where the rules replayed them. Ask where an answer is
  stored, not just where it is produced.
- **Assembly is not a task.** It needs no model, and `cmd_work` returns early with no router —
  the bug that made `prefill` leave the queue. `jobtracker tailor build` always runs.
- **Nothing writes bytes to your resume.** Edits apply to a copy in memory; the result is a
  new file under `$JOBTRACKER_TAILORED`; `--attach` records it through the per-posting
  override that already existed. A test reads the no-write rule off the source.
- **`assemble.py` is the only subprocess in this repo**, with a test keeping it that way. List
  argv, `shell=False`, a scratch directory the engine runs inside, a timeout (TeX loops rather
  than erroring), and `--untrusted`. **Exit 0 with no PDF is a failure and is named** —
  otherwise a compile that "worked" and produced nothing is a blank attachment.
- **A missing toolchain is not an `unavailable_reason` for the task.** Suggestions are text
  and need no engine; withholding the whole feature for want of its last step is the
  capability-absent shape this file keeps naming. `tailor build` reports it once and exits
  **0**, and must never make `prepare` exit 2.
- **Tectonic is in the serve image only**, on `Dockerfile.serve`'s argument about the
  browsers: the nightly never compiles anything, and a toolchain in the 177MB batch image
  multiplies the nightly pull for something it never runs. CI asserts
  `latex.unavailable_reason() is None` on the published image, as it does Playwright — a lost
  capability otherwise looks exactly like a working deployment.
- **`dismissed` is reachable, and kept.** `jobtracker tailor dismiss` is the only way to set
  it; a state nothing can reach is dead weight that reads as a feature. Kept rather than
  deleted — deleting would let the next run propose the same edits again (`mail_proposals`'
  rule) — and it reopens when the resume hash moves, because a ruling about the old wording is
  not a ruling about the new.
- **Priority 50, last.** It consumes what `judge` produces and feeds nothing, so the chain
  does not fix its number; it is behind `inbox` because its unit key is a hash of the resume
  **text**, so one edit re-keys every posting at once. (`Answers.hash` covers only a resume's
  basename and cannot be reused for this.)
- **Neither surface has a button, in either mode.** Accepting means compiling a document; a
  control on the Today card or `/apply` would put a model-authored PDF one click from an
  application with the diff unread, and would widen what `.pick [data-act]` and `.lf` select.

## The ambiguity pass

The `level` task, documented in `docs/llm.md`. **Local only** — the model is an address, and
there is no API-key handling anywhere in `jobtracker/llm/`. Do not add a hosted provider.

- **Transport is the `sir-client` SDK** (`../stupid-inference-router`), since 2026-08-13. The
  `Provider` interface and its registry were **deleted**: they existed so a second wire format
  could be slotted in, and the router is that indirection now. `llm/` is two files — `wire.py`
  (pure, knows the body shape) and `client.py` (the only module that opens a socket).
- **The SDK is async-only**, which is why `tasks/runner.py` is async and why `browser.py` —
  Playwright's sync API, which must not run inside a loop — is a separate module. Do not merge
  them.
- **`sir` forwards the body untouched.** It reads only `model` and `stream`, so the schema
  request still travels and the parsers are still the only thing between a backend that
  ignores it and a fabricated verdict. Routing through a router guarantees nothing.
- **Scope is level extraction only.** The model never decides that a role is on-target; an
  `entry` reading still has to pass the rules' engineering gate. Widening this would put a
  nondeterministic component back in the main loop, which is what DESIGN.md was written to
  undo.
- **Every failure path must leave the posting UNCERTAIN.** Unreachable, timeout, malformed,
  unsure — all of them. Nothing here may raise for a down server. A code path that can produce
  a verdict from a failed call is a bug.
- **The schema request is `response_format`, not `guided_json`** (changed 2026-07-24). vLLM
  dropped the `guided_json` / `guided_decoding_backend` pair; 0.23 accepts a body carrying
  them, *ignores* them, and answers in prose. `_parse_verdict` then rejected every response and
  the whole pass became a silent no-op — still fetching a description per posting and resolving
  nothing. Because failure-is-absence is the design, this cost no accuracy and raised no error,
  which is why it sat undetected. Diagnostic: `work` reporting ~zero applied while the server
  is up means the wire format, not the model. Verify against the server you actually run —
  `test_request_constrains_output_and_is_deterministic` pins the request shape, but only a live
  call proves the server honours it. Demonstrated again 2026-08-13 against the router's *mock*
  backend, which ignores the schema: every prefill question-match came back unparseable and
  every field became a gap. (That pass no longer exists — DESIGN.md §8.1 — so reproduce
  against `level`.)
- **The `level` task is a pure read** (2026-08-02). `check` caches the description for every
  match/uncertain posting, so this pass opens no ATS connection — it lost its
  `fetcher`/`store_mod`/`conn` parameters and its lazy fetch-and-cache block. A throttled board
  can no longer shrink the queue it considers.
- The queue is scoped to titles with an engineering signal (674 of 1,537). Known blind spot:
  "Member of Technical Staff" is never read. It stays UNCERTAIN rather than being rejected, and
  a test asserts that. Do not "fix" it by rejecting no-signal titles. (**But see the doc bug
  under "Not yet done"** — `staff` is in `exclude_titles`, so `match()` rejects that title
  outright and `resolve` never sees it.)

## Descriptions are cached by `check`

Since 2026-08-02, `cmd_check` stores a description for every posting whose verdict is `match`
or `uncertain`. That is what makes `resolve` and `rank` offline with respect to the ATSes:
they read `state.db` and talk to nothing but the local model.

- **Scoped, deliberately.** The ~7,100 open rejects are excluded — fetching them would cost
  ~40 minutes and ~48MB to serve nothing that reads them. Self-healing: retune criteria so a
  former reject becomes a match, and the next `check` fills it in.
- **Write-once.** `NULL` = never fetched, `''` = fetched and genuinely empty. Only NULL is
  retried.
- **`--max-descriptions` (default 400)** caps requests per run so a bad night cannot turn a
  30-second job into a 40-minute one. Measured: ~0.6s per Greenhouse fetch behind the existing
  limiter, ~30–40 fetches/night in steady state.
- **A description failure is invisible to board health** and must never produce
  `EXIT_DEGRADED`. A 500 on one job detail is not a broken board.
- Ashby and Lever ship `descriptionPlain` in the **bulk** payload for free; only Greenhouse
  needs a per-posting fetch, and its `content` is HTML-escaped *inside* the JSON string, so
  unescape before stripping tags. Fetches go through `Fetcher._request_json` so they inherit
  the per-host limiter — the ATS is the scarce resource, not the local model.

## Posted dates

`postings.posted_at` is the vendor's raw value and is **five mutually incomparable formats** —
Greenhouse ISO-with-offset, Ashby ISO-UTC-with-millis, Lever epoch-millis as a string,
aggregator a relative age like `2d`, Workday relative English prose (`Posted 2 Days Ago`). As
text an epoch string collates before every ISO timestamp, so `ORDER BY posted_at` is silently
wrong. Nothing may sort on it.

`postings.posted_on` is that value normalized to a plain ISO day, and the only date anything
may compare. Conversion lives in the adapter behind `Source.normalize_posted_at(raw, today)`;
`today` is a parameter, not a clock read, because adapters are pure and **three** sources now
date relatively.

- **Greenhouse's bulk field is `updated_at`, which is not a posted date.** It moves whenever
  anyone edits the req. Observed: a Stripe posting first published 2023-11-01 reporting
  `updated_at` of 2026-07-27 — ranked on the old field it would have looked like the freshest
  thing on the board. The real value is `first_published`, only on the detail payload, so it
  arrives with the description fetch.
- **`sync_postings` writes posted_on with COALESCE.** A bulk pass with no date must not erase
  one already stored, or the 47 Greenhouse boards would blank their own dates nightly.
- **Unparseable input is NULL, never today.** A missing date reading as "posted now" would
  invert the ranking it exists to inform.
- `first_seen` is not a substitute: 8,634 of 9,765 rows share the 2026-07-23 backfill date.

## The ranking pass

`jobtracker rank` and `jobtracker today`, documented in `docs/ranking.md`. Where `level`
decides whether a posting is *on-target*, this decides which is *urgent*. The model's second
bounded role, and the smaller one.

Judging is the `judge` task now. **Scoring deliberately is not a task** — it needs no model,
must run whether or not one is reachable, and is arithmetic over rows the task already wrote.
Keep it in `cmd_rank`.

- **The model judges one posting; Python does the ordering.** It returns three labelled
  ordinals (`backend_fit`, `growth`, `entry_risk`) plus a sentence, never a score and never a
  comparison, and never sees another posting. Widening that would put a nondeterministic
  component back into the ordering itself.
- **Ordinals, not 0–100.** LLM numeric scores cluster in a narrow band and shift with any
  prompt or model change, silently re-ranking everything on an unrelated edit.
- **`profile.yaml` splits prose from weights, and `prose_hash` covers only the prose.** Change
  a weight and every cached judgment stands, so re-sorting the queue costs zero model calls.
  Change the prose and judgments are re-taken, because they answered a question you have now
  changed.
- **Scores are absolute.** A new posting is judged once and lands in its slot without
  disturbing anything else. Do not replace this with pairwise insertion: ~log₂(n) calls per
  posting, path-dependent, and unable to recover from non-transitive comparisons.
- **Two absences that must not be "simplified":** an unjudged posting scores `None`, not `0.0`
  (zero buries a model failure at the bottom of the list); an undated one scores mid-scale, not
  "today" (that floats stale reqs to the top). Both have tests.
- **Rankings live in their own table, never in `verdicts`.** `check` rewrites `verdicts` every
  night — that is what erased the LLM's work before.
- **`rank` never fails for want of a model.** With none configured or reachable it skips
  judging and still scores from stored judgments; yesterday's order beats nothing.
- **`rank` can only judge what `check` cached.** A large "still unranked" count while the model
  is up usually means the description backfill is still draining, not a model fault.

## Prefilled applications

`jobtracker prefill` and `jobtracker apply-to`, documented in `docs/prefill.md`. Added
2026-08-13. Two halves: an offline pass that builds a plan and names what is missing, and an
on-demand browser that carries the plan to the page.

**It asks a model nothing** (2026-08-25). `jobtracker/prefill.py` is deterministic and opens
no socket except the ATS form fetch; a test reads that off the source. It was `work --task
prefill` until then — see "The task queue" for why leaving the queue was required, and
DESIGN.md §8.1 for what the model had been deciding. Two rules follow from the removal:

- **A deleted model does not delete its decisions.** `record` writes every resolved key onto
  `form_fields.question_key`, and `known_question_keys` replays it as a deterministic alias at
  every company forever. 229 of 383 resolved fields in the live database were explicable only
  as model output, including *"Protected Veteran Status"* → `are_you_a_current_mongodb_employee`
  and every *"do you require sponsorship?"* → a work-**authorization** answer, whose stored
  value means the opposite. **`jobtracker forget-learned`** sweeps everything no rule and no
  user-written alias can account for, dry by default, grouped by key; it is also the bulk form
  of `forget-question` for a bad *human* alias, the only kind left. It **takes the values out of
  the stored plans too** — blanking `answers_hash` only helps a posting that will be re-planned,
  and 13 of 64 live plans had left the queue for good while `apply-to` still reads them through
  `get_plan`. The sweep is judged from the plan entry rather than a join to `form_fields` (the
  two drift; the join missed 7 of 37), exempts `file` and `alternative` entries (a DOM file
  input can be keyed `attach`, and detaching the resume is the worst thing this could do), and
  runs even when `form_fields` has nothing left to clear.
- **The bank now only grows by hand, so both doors have to work.** See "Growing the answer
  bank". The fill rate fell from ~61% of plan entries to ~21% the day the sweep ran — the
  accepted trade, not a regression to fix by loosening a rule.

- **A cookie cannot carry prefill state, and neither can a URL.** Greenhouse, Ashby and Lever
  hold no server-side draft for an anonymous candidate, only Lever honours query-parameter
  prefill, and no URL attaches a file. What fills a third-party form is code running on the
  page. Do not re-propose the link.
- **The browser never submits.** No click path in `browser.py`, asserted against the source
  (no `.click(`, `.press(`, `requestSubmit`, `dispatchEvent`). (Superseded twice: `_submit` gained the one gated click on
  2026-08-22 and `_press` the widget click on 2026-08-23 — see "Reading a form as it actually
  is". The ban on `requestSubmit`/`form.submit`/`dispatchEvent`/`keyboard.press` never moved.)
  An application is irreversible and goes out under the user's name.
- **Nothing may put a value in a field the user did not give for it.** This was "the model may
  only point, never write" — an enum of answer keys plus `none`, with no path by which a
  sentence it composed could reach a form field. Every part of that bound held and the feature
  was still removed, because pointing at the wrong key puts the wrong text on a real
  application as surely as writing it. The surviving rule has no exception: a value reaches a
  field because a canonical ATS name matched, or because *the user attached this wording to
  this answer*.
- **A dropdown with no list to open is asked, not guessed at** (2026-08-29). `search` is the
  seventh command and the only way to read a menu that has no vocabulary: Greenhouse's *Location
  (City)* fetches its options per keystroke, so opening it shows nothing — the one combobox of
  ten on Twilio's form with no "Toggle flyout" button, because there is nothing to toggle. So
  `_learn_vocabularies` correctly learnt nothing, `/apply` rendered a text box, and any answer
  not character-for-character one of its suggestions came back *"would not take it"* with no way
  to see what would have been taken. Now a **refused** `_pick` publishes the menu it had to read
  in order to refuse (`seen` → `Session.offer`), and the row carries a query box and **Look
  up**; either way the suggestions render as the row's `<select>` and picking one pushes a
  string the widget's own menu produced. The reading is **never** stored as the field's
  `options` — it answers one query, and `known_options` would replay it at every later visit —
  and a re-reading of the form drops it. `_pick` used to type only when the menu came up
  *empty*, so a menu showing anything at all was refused without being asked for the value we
  hold.
- **A dropdown that does not offer our answer is a gap, not a fill.** Picking the nearest option
  puts an answer the candidate did not give onto a submitted application. `match_option` waves
  any string through when `options` is empty — right for a text box, and for a menu a statement
  that nothing checked it, which is how identity `location` ("New York, New York") ended up in a
  phone-number country selector. `prefill.vocabulary_known` carried the menu half of that and
  went with the model pass, because holding a canonical name and a deliberate alias to it would
  make every combobox permanently unanswerable. The guard is structural now: with no alias, an
  unrecognized label is a gap whatever its type, and a test says so by name.
- **A resolved `question_key` is only stored when it actually placed a value.**
  `known_question_keys` replays it as a deterministic alias at every company, so storing one
  the rules then refused turned a guess into a rule nothing would reconsider. `jobtracker
  forget-question "<label>"` is the way out, and it moves all three places holding the decision
  — the `form_fields` key, the closed `prefill_gaps` row, and the `prefill_plans` whose stored
  value beats a fresh `resolve_field`.
- **What `Answers.hash` covers is settled by one test: does this change what goes in a field.**
  - **An alias does**, so aliases are in it (2026-08-25). While they were not,
    `matches_needing_prefill`'s `answers_hash != ?` never fired, the plan was never rebuilt, and
    the field you had just explained stayed a gap forever while the page said it saved.
  - **The name a resume goes out under does not.** Disk names are minted for collision safety
    (`resumes.stored_name`); `resume_name` in the bank is what a person at the other end opens,
    defaulting to `resume<ext>`, suffix always from the real file. Folding it in would re-plan
    every posting for a rename.
  - **A per-posting resume override does not** (2026-08-16). `prefill_plans.resume_key` carries
    it instead, and one disjunct in `matches_needing_prefill` compares the two stored columns,
    so attaching a resume re-plans that posting and no other. Fold it into the hash and every
    plan built with one looks permanently stale. `prefill.record` must keep storing
    `ctx.answers.hash`, never the `dataclasses.replace`d copy's.
- **Gaps are split generic vs company-specific** (2026-08-16). `prefill.split_gaps`: generic = a
  key in `GENERIC_KEYS` *or* asked by 2+ employers, sorted by ask count descending; everything
  else groups under its one company. No new state and no maintained list — a question migrates
  on its own when a second employer asks it. It decides **rendering order only**; no write and
  no fill reads it, so a misfiled question costs ordering, never correctness.
  `answers.render_gap_block` uses the same order, and `_gap_card` emits identical markup in both
  lists, which is why `server._JS`'s save branch needed no change.
- **Swapping `answers.resume` is not enough to attach that override.** `browser._plan_index`
  lets a stored plan value beat a fresh `resolve_field`, so `prefill.retarget_resume` rewrites
  the plan's resume entries too. Both halves are applied in `server._api_apply_to` **and**
  `cli.cmd_apply_to` — the button and the terminal must not disagree about which file went out
  under the user's name.
- **Uploads are named by us, validated by content, and share one function.**
  `resumes.validate_upload` (suffix allowlist *before* decode, base64, size, magic bytes) is
  called by both `/api/resume` and `/api/posting-resume`; `resumes.stored_name` mints
  `<company>_<job>_<digest8><suffix>`, so no separator or `..` survives from a company name and
  two look-alike slugs cannot overwrite each other. `_UPLOAD_ROUTES` is a **set** — a file
  route left out of it reads its body as `{}` and reports "no file", a correct-looking error
  for the wrong reason.
- **"Rebuild prefill" opens no socket.** `server._rebuild_plan` is CPU + SQLite only: the
  server is single-threaded, so a form fetch would freeze every tab. Since the model pass went,
  it is the same resolution over the same inputs as the nightly pass. What it will not do is
  *fetch* a form it has never seen: no cached form → it **refuses and says so**, never `0/0 ·
  nothing left to type`. The unit is built directly rather than filtered out of
  `prefill.pending()`, which returns nothing when the plan is already current — the button
  would then silently do nothing in exactly the case it exists for.
- **A gap is re-examined, not only recorded** (2026-08-25). `prefill.close_answered_gaps` runs
  at the end of `build_plans` and resolves every open gap the bank can now answer, through the
  same `resolve_field` the plan uses and with the stored options attached, so a dropdown that
  does not offer the answer stays listed. `_api_answer` closes the one key you just wrote,
  which covers the common path and misses every other route to the same place — an identity
  field filled in Settings, a value edited by hand, an alias attached to a different key,
  `LABEL_ALIASES` gaining the wording. Measured right after the first `forget-learned`: 11 of
  200 open gaps were already answerable, and "Phone", "LinkedIn Profile" and "Website" sat near
  the top of the most-asked list.
- **`answers.yaml` is gitignored** — personal data. `answers.example.yaml` is the tracked file.
  Everything above the `# ===== unanswered questions` marker is the user's and is never parsed
  or rewritten; the block below is regenerated wholesale from `prefill_gaps` on every run.
  Writes go through `safewrite.py` (candidate → parse → `.bak` → atomic swap).
- **Adding an answer is text surgery, not a YAML round trip.** A round trip deletes every
  comment in the file, including the stubs the user is working through. Same for the identity
  fields and the resume path — `upsert_identity`, `set_resume` and `set_resume_name` sit beside
  `insert_answer` in `answers.py` for that reason. `insert_answer` **updates in place**
  (2026-08-23) and rewrites only the `value:` line, leaving alias lists, quoting and comments
  byte-for-byte; aliases are additive, because an alias is one employer's exact wording and
  editing the value says nothing about it.
- **The Settings tab creates the bank; there is no `cp` step** (2026-08-15). Saving identity
  writes `answers.STARTER` and upserts into it, so a fresh box gets there from the browser.
  STARTER carries the example's prose and **none of its values** — `answers.example.yaml`
  documents the shape with Ada Lovelace's name and email in it, and a bank that loads with a
  stranger's identity is the failure this file's strictness exists to prevent. Three rules in
  the writers: a commented-out field is filled *in place* (two lines that disagree is worse than
  one), clearing a field deletes the key rather than blanking it (a blank loads as an answer
  typed as the empty string), and the resume is validated by magic bytes with the client's
  filename used for its suffix only. The upload writes the file **before** the `resume:` key,
  because `load_answers` refuses a path that is not yet a real file.
- **Only Greenhouse publishes its form** (`?questions=true`, keyless, complete — 47 of 62 api
  boards). Ashby's per-job posting-api is 401 and its GraphQL introspection is off; Lever
  exposes no custom questions. Verified 2026-08-13. Their forms are learned from the DOM on the
  first `apply-to` visit and cached per company, which puts every ATS in the same gap loop. A
  company whose form is neither held nor fetchable is **not** counted as pending work — it is
  waiting on a browser, not a model.

### Reading a form as it actually is (2026-08-23)

Measured against Twilio's live Greenhouse embed and kept as
`tests/fixtures/greenhouse_react_form.html`. **That page has no `<select>` on it at all**, and
five reported bugs were one consequence of assuming it did.

- **Every dropdown is a react-select combobox**, so `page.fill` sets a search query the widget
  discards on its next render — *and* remounts the input, taking the `data-jt-id` with it. The
  field stayed empty while the row reported `filled` and the submit gate counted it as
  answered, on the one page where the cost is an application going out blank. `_pick` opens the
  widget, reads what it offers, and presses the matching option; `data-jt-ctl` is on the
  *control*, which survives being typed into.
- **A combobox's options exist only while its menu is open**, so `_learn_vocabularies` opens
  each unknown one once per visit (bounded by `MAX_VOCABULARIES`) and `upsert_form_field` keeps
  them — its `options` column is `COALESCE`d, because a DOM pass that cannot see them was
  erasing what the Greenhouse API had published. A field past the cap keeps an empty list and
  renders as a text box saying so.
- **`_press` is the module's second click and the rule got narrower, not looser.** `_submit`
  presses the employer's button once behind the gate; `_press` presses a *widget's* own control
  — an option, a clear indicator, its open/close toggle — reached only from `_pick`, `_clear`
  and `_read_vocabulary`, always scoped to the control of the field being written. A controlled
  React component learns a value from its own handlers and nothing else. A test pins both sites
  and all three callers.
- **`aria-hidden` elements are skipped.** react-select renders a phantom `<input required
  tabindex="-1" aria-hidden="true">` beside every combobox; with no name and no id it keyed on
  a slug of the same label as the widget it shadows. One dropdown, two required rows — and
  since `Session.carried()` is keyed by field key, the phantom's stale value was handed back to
  the row you had just edited, which is what made typing into a field revert one poll later.
  `carried()` is first-wins now as a second line.
- **A checkbox set is one question.** Members carry the question in `label`/`group` and their
  own choice in `option`; grouping is fieldset legend → Greenhouse's `description` attribute →
  shared `name`, and a set of one is not a set. One gap, one block on the page, one bank
  control — and each box pushes *its own choice*, not `"yes"`. `live._unanswered` counts by
  question, or a nine-box set would leave eight permanent blockers on the submit gate.
- **`Country*` on a Greenhouse form is the phone's dialling code** — its menu offers "United
  States +1". Nothing in the code knows that and nothing needs to: it is a dropdown with a
  vocabulary, so a wrong answer is refused rather than typed.

Four more things learned from live forms, all handled and none obvious:

- **Greenhouse's current board UI sets no `name` attributes** — everything is keyed off `id`,
  including `id="resume"`. Reading only `name` silently failed to attach the resume, the single
  most valuable field. Field keys are `name` → `id` → a slug of the label.
- **One visible question can be several inputs.** A combobox renders as a text input plus a
  hidden select; "Resume/CV" is a file input plus a textarea, either of which satisfies it.
  Once any sibling holds the answer the question is answered and the rest are not gaps.
- **Some employers redirect the hosted board to their own careers site, and the fix for that
  was itself wrong until 2026-08-19.** Stripe's `absolute_url` is a search page with no form on
  it, so the canonical board URL was used instead — but **the board redirects too**:
  `job-boards.greenhouse.io/asana/jobs/…` 302s to `asana.com`, a JS shell whose form is a
  cross-origin iframe, and the card reported *"no application form found"* about a job with 32
  fields. **25 of 45** tracked Greenhouse boards do not carry the form there (airbnb, asana,
  betterment, brex, coinbase, databricks, datadog, dropbox, lyft, mongodb, okta, pinterest,
  stripe, …; `careportalinc` 403s). Greenhouse now gets `embed/job_app?for={slug}&token={id}` —
  the form itself, keyless, never redirected, present on all 45. **Zero fields discovered is
  still "no application form found", never "0/0 filled, nothing left to do"** (DESIGN.md §3.4).
- **`page.evaluate` only ever sees the main frame.** An employer that embeds its ATS puts the
  whole application in an iframe, which a main-frame reading calls an empty page. `_discover`
  falls back to the frames and returns the **surface** the fields came off — a `Page` or a
  `Frame` — and every write, highlight and re-reading goes through it, because a handle minted
  by one discovery names nothing outside it. Second line of defence behind the URL above.

## Growing the answer bank

Added 2026-08-25, when the prefill model pass was removed and nothing was left to attach a
question to an answer on its own. Two doors, and neither works without the other. Full guide in
`docs/prefill.md`.

- **The enum did not go away; it became a `<datalist>`.** The model chose one key from
  `Answers.answerable` per unplaceable question. That same list is offered on every `/apply`
  row's "save as" box and on every Settings gap card, and the choice is made by someone who
  knows the answer. `<datalist>` rather than `<select>` because minting a new key by typing has
  to stay as easy as picking an old one.
- **On `/apply` the save box is ticked by default**, because the moment you know the answer to
  "how did you hear about us" is while you are typing it into a form. Two things had to change
  for that default to be safe, both the same mistake in different clothes:
  - **`bank()` may not run from the typing debounce.** It used to, then untick itself after the
    first success — harmless while *you* did the ticking, because you ticked when you had
    finished typing. Ticked from the start it stores whatever you had reached at the first
    400ms pause and disarms, so the finished sentence never lands. Blur and dropdown-change
    still call it; both mean "done with this field". A test reads the debounce branch and
    asserts `bank(` is not in it.
  - **It stays armed.** `/api/answer` is an upsert, so correcting a value overwrites; disarming
    after the first save would make the correction the one thing that does not stick.
- **`alias` and `gap_key` travel in the payload, and neither is optional in practice.**
  `_api_answer` used to read the alias out of `prefill_gaps.ask` and close by answer key, which
  works only when the answer goes under the gap's own key. Attaching to an *existing* key
  recorded no alias — so nothing would recognize the question at the next employer — and closed
  a row that was never open, leaving the one you were looking at on the page. With the model
  gone that is the main path, not a corner.
- **`/api/attach` carries no value.** The value is already in the bank; accepting one would
  make attaching a second way to edit an answer, which is `_api_answer`'s job, and the two would
  disagree the first time one grew a validation rule. Three refusals, each a state that would
  otherwise look like success: an unknown key, a key holding no answer, and a `gap_key` with no
  open row.
- **An identity key cannot hold an alias**, because `Answers.by_alias` is built from the
  `answers:` block alone. Attaching a wording to `email` is written to `form_fields.question_key`
  via `store.learn_question_key`, which is what `known_question_keys` replays. It never mints a
  `form_fields` row — a label nothing has asked is not a question, and a row minted there would
  name a company that never asked it.
- **`LABEL_ALIASES` is for wordings that are true everywhere**, and that is the whole admission
  test. Eleven entries were harvested 2026-08-25 out of what `forget-learned` swept — wordings
  the model had matched correctly, which would otherwise have become questions to retype. The
  near-misses were deliberately left out and a test names them: *"Preferred First Name"* is not
  `first_name`, it is a different question with a different answer, and reading it as one is how
  a nickname reaches a legal-name field. A wording that needs the employer, the surrounding
  question, or a choice between two readings belongs in the user's own alias list, attached
  while looking at the form.
- **A resolved gap reopens carrying the wording still unanswered.** `question_key` is
  `slugify(ask)` capped at eight words, so five real questions can share one row — measured:
  *"Are you legally authorized to work in the United States?"* and four variants naming a
  company or a country. Matching is by the **full** normalized label, so attaching an alias to
  one fills that employer's form and none of the others. The row used to be marked resolved and
  never come back: the question vanished from Settings while four employers kept a blank
  required field and the page said it saved. `record_gap` reopens on re-sighting (only the
  caller knows a field is still empty, since `record` calls it for `result.gaps` and nothing
  else) and refreshes `ask` to the open wording, which keeps it out of `close_answered_gaps`'
  way — the stored wording is by construction one nothing can fill, so the two cannot cycle.
  Settings says this out loud, because a question returning nearly identical reads as a bug.
- **Settings orders by how many employers ask** — `split_gaps` sorts the generic list by
  `-len(gap_companies(gap))`. The per-company list stopped hardcoding "1 employer" about gaps
  with no company recorded, and the note names at most five companies before counting the rest,
  because thirty names is the reason you cannot see the next question.

## The mirrored form

`/apply` under `serve`, `jobtracker/live.py`, and the drain in `browser._hold_until_closed`.
Full guide in `docs/prefill.md` under "Filling it in". Added 2026-08-18.

**The window is no longer where you type.** "Open prefilled" navigates to `/apply`, which
renders one HTML field per field discovered on the real form; you type there, the value is
pushed to the browser `serve` is holding, and a screenshot every few seconds shows the real
page. This replaced VNC — a remote X server shipping video frames for a task that is fifteen
text fields. The write primitive already existed (`_write`, keyed by the `data-jt-id` handle
`_DISCOVER_JS` mints); all that was missing was a channel to it.

- **`live.py` is pure.** No Playwright, no HTTP, no SQLite — so `browser.py` and `server.py`
  both import it and neither imports the other, and every rule below is testable with no
  browser and no socket. Same split as the task modules.
- **The drain runs in the tick that was already there.** `_hold_until_closed` polls
  `page.wait_for_timeout(500)` and drains the queue in that same tick, the only code touching
  `page` outside the fill. **Playwright objects belong to the thread that made them** — an HTTP
  handler calling into one is the bug this shape prevents. A write is queued, answered
  immediately, and its outcome arrives on the next poll.
- **A command points, it does not write.** The vocabulary is exactly seven names — `set`,
  `clear`, `reset`, `rediscover`, `shoot`, `highlight`, `search` — and a command carries a field
  *handle*, never a selector and never anything the browser thread evaluates. That is
  `browser.py`'s no-click-path rule carried across the new channel, and it needs its own test
  because the existing one only scans that module's source.
- **Every answerable row shows the answer behind it, and can change it** (2026-08-23). Rows
  carry `question_key`; before that the bank control rendered only on `GAP`/`REFUSED` rows, so
  the bank was writable exactly once per question and a field holding "New York, New York" under
  the label "Country" gave no sign where that came from. Both controls post to `/api/answer`,
  which must be a real **upsert** — `insert_answer` used to prepend unconditionally, so
  re-answering wrote a duplicate YAML key, `yaml.safe_load` kept the old one, and the page said
  saved while your value was discarded. An identity key goes to `identity:`, because
  `Answers.get` reads that first and an `answers:` entry of the same name is a write nothing
  loads.
- **`clear` is a name of its own, and an empty `set` is refused** (2026-08-22). `page.fill(el,
  "")` succeeds, so deleting a value used to record the row `filled` holding nothing — counted
  as done and counted *out* of "need you" — the reading `answers.py:327` refuses in the answer
  bank, arriving instead on a form about to be sent. `cleared` is its own
  status and counts as needing you. Overloading `set` is wrong for `file` rows, where the value
  is a path on this box and `""` means *no file*, not *no text*.
- **All four control kinds empty, and three of them could not.** `_clear` mirrors `_write`
  branch for branch (`fill("")`, `select_option([])`, `uncheck`, `set_input_files([])`).
  Unticking a checkbox used to report "would not take it" and change nothing, the select's blank
  option was dropped by the client's `if (v)` guard, and a file input — which no browser lets
  you empty — had no path at all, hence the **detach** button on a file row that holds
  something.
- **A handle is only valid for the discovery that minted it.** `_DISCOVER_JS` renumbers
  `jt0…jtN` from scratch every pass, so once the form changes shape a handle names its
  neighbour. Commands carry the `epoch` they were written against and are dropped on a mismatch,
  in the drain, where nothing can bypass it. This is the one way this feature could put an
  answer you did not give into a field you cannot see.
- **But stopping is not an ending** (2026-08-26). A correct bump used to disable every field and
  wait for a Reload nobody had reason to press — which is what attaching a resume looked like,
  since Greenhouse's file row re-renders the moment it takes a file. The handles the page holds
  are stale; the **server's** rendering of them is not, and re-reading it is what a reload does,
  so the page reloads itself. It asks only when it cannot: a reload while you are typing
  discards the sentence you are in, and one with a push in flight lands before its outcome does.
  The guard is read **before** anything is disabled, or it is reading a page it has just
  blurred; a file picker deliberately does not count as typing, being the row that most reliably
  moves the epoch.
- **`reset` is the sixth name in the vocabulary** (2026-08-26): `clear` over every field at
  once, on `clear`'s side of the activation line since emptying reaches nothing a fill does not.
  One command rather than a loop of clears, because `_clear` re-reads the form on its way out —
  thirty of those is thirty chances for the shape to change under the remaining handles, after
  which every later clear is correctly dropped and a reset that emptied four fields of thirty is
  reported as a whole one. It touches only rows holding something (a `gap` is a question nobody
  had an answer for; `cleared` is one you emptied on purpose), and a field that will not give
  its value up stays `refused` holding it. It is the **one command carrying no epoch** — naming
  no handle from the page's side is what makes it the way out of a form that has moved.
- **But the epoch moves only when the handles actually moved.** A successful write re-reads the
  form (questions get revealed by answers), so bumping every time would mean the second field
  you typed is refused because the first one succeeded. `live.signature` asks the one question
  the epoch is about: does every position still report the same field under the same handle.
  Both halves have tests.
- **Statuses carry over by key, not by handle.** The handle is positional and is exactly what
  moves; the key is the ATS's field name. Carrying by handle would hand a new question the
  answer that belonged to its predecessor.
- **`PENDING` counts as "needs you".** A field the fill never reached, or one a re-reading has
  only just revealed, is not "nothing left to type". Zero discovered is still *"no application
  form found"*.
- **`img-src 'self'` is as load-bearing as `connect-src 'self'`.** The CSP is `default-src
  'none'` and both fall back to it, failing the same silent way: the preview becomes a broken
  image over a browser that is working perfectly. `_CSP` is one string, and the test asserts the
  value rather than `_send`'s source text.
- **The shot is the whole page, and the zoom is client-side** (2026-08-19). A viewport is 720px
  and a form is several thousand — Asana's is 1280x3352 — so a viewport-shaped preview showed
  five fields of thirty-two over a window nobody on `/apply` can scroll. `full_page=True`,
  rendered scaled-to-fit with a Fit/100% toggle that is **two CSS classes and no request**.
  `SHOT_EVERY_S` went 2s → 4s to pay for it (190 KB / 113 ms against 22 KB / 36 ms). `.fit` is
  in the server's markup and `#zoom` is hidden until the script adds `js-zoom`, so JS-off gets
  the whole page and no dead control.
- **No screenshots for a page nobody is looking at.** Each poll refreshes a deadline and the
  drain shoots only inside it, which covers the closed tab for free. **Pause was not that case
  until 2026-08-22**: the button suppressed the `<img>` src on the client while the poll kept
  refreshing the deadline, so the browser thread went on rendering a full-page JPEG every four
  seconds for a picture nobody would see. A paused poll now sends `?idle=1` and `_api_session`
  skips `session.watch()` — withholding the claim is the only thing that stops the shooting.
- **The preview says how old it is, and `paint` writes values as well as statuses** (both
  2026-08-22, both consequences of this being the *only* view now). `#ago` saying "refreshed just now"
  unconditionally is indistinguishable from a preview that has stopped refreshing; and with
  `paint` moving only the status pill, the several seconds of fill landing *after* the page
  renders showed up on the real form and nowhere on the page mirroring it.
- **`highlight` is wired to focus** (2026-08-22) — it ties the row under your cursor to a place
  in a 3352 px picture. Fire-and-forget: it moves no value, and the epoch is still checked,
  because a second rule about when the epoch matters is one somebody gets wrong later.
- **The window closing must set `CLOSED`, and the page must land it.** Otherwise the page polls
  a form nobody holds and every edit queues into nothing — a mirror that looks live over a
  browser that is gone. Setting the phase was only half: Done closed the window and released the
  lock while the page left the button on *"closing…"* and the fields still taking input, which
  reads as a hang. `CLOSED` (or a session that has gone entirely) now unhides a server-rendered
  banner, disables every control, and **stops the poll**. `render_apply` emits that state
  directly too, so a reload is honest with no script.
- **Sending it is a gate, not a command** (2026-08-22). It is **not** in the vocabulary — that
  list's stated property is that nothing in it can activate anything, and queuing a submit would
  make sending an application the same kind of act as typing into a text box. It takes
  `request_close`'s shape for the mirror-image reason: closing is outside the vocabulary because
  it does nothing to the form, submitting because it does the one thing that cannot be undone.
  - **Three checks, in three places, and none is redundant.** `Session.request_submit` refuses
    unless the phase is `READY`, the epoch matches, a submit control was found, every required
    row is `FILLED`, and the company name has been typed. `_submit` takes the same checks again
    on the browser thread, because the gate's reading is up to one poll old and a form can
    reveal a required question in that gap — it stands down with `disarm()`, leaving
    `submitted_at` untouched so the button returns rather than the session jamming.
    `claim_submit` spends the one submit under the lock **before** the click, so two reads of
    the flag cannot become two applications.
  - **The refusal names the fields.** "Not ready" is not actionable; the required questions it
    is waiting on are. This is also why `cleared` had to be its own status — deleting a required
    answer must put the job back in the way of the button.
  - **`browser.py` has exactly one click, in `_submit`, reached only from the hold loop**, and
    `requestSubmit` / `form.submit` / `dispatchEvent` / `keyboard.press` are still banned.
    Stricter than the old no-click rule, not weaker: a real press of the employer's own control
    runs their validation, their required-field checks and their captcha hooks, all of which a
    programmatic send skips — which is how you submit an application their own page would have
    rejected. The test scans the source **with docstrings stripped**, because the prose now
    names the mechanisms it bans in order to explain them.
  - **`_SUBMIT_JS` is separate from `_DISCOVER_JS`** and mints its own attribute
    (`data-jt-submit`), because the discovery pass deliberately skips submit and button inputs —
    a handle minted for one would put a clickable target inside the vocabulary that says it has
    none. It ranks candidates (explicit `type=submit` beats submit-shaped wording, since "Apply"
    is also what the button that *opens* a form says) and drops cancel/back/cookie-banner text.
    **Zero candidates is "no submit button found"**, and the page renders no button at all.
  - **What follows the click is a reading, never a verdict.** Nothing here can prove an employer
    received an application, so `finish_submit` records what changed — the URL, the field count
    — and the page says exactly that. A page that did not change reads *"nothing on the page
    changed — read the preview before assuming it went"*. An unverifiable "submitted
    successfully" is how a failed send stops being re-checked.
  - **It reaches `applications` only when the page moved**, through an `on_submitted` callback
    supplied by `server._api_apply_to._run` — so `browser.py` never learns about that table and
    the write goes through the worker thread's own connection, the thread the submit lands on.
    `record_submission` is **module-level and not a closure**: nested in the wrong scope,
    `worker_conn` was unbound and every recording would have raised `NameError` inside the
    callback's own `except`, leaving a submit that looked complete and landed nothing. Ruff's
    F821 found it and the test did not, because it read the source for the right words instead
    of calling the function — test the write, not the text. When nothing changed, nothing is
    written and the page offers a **Record it as applied** button instead: `applied` is the
    status that stops a job coming back round, so setting it on a guess would make a failed send
    go quiet exactly as a successful one does.
- **Closing discards the fill, so the button confirms first.** No ATS keeps a draft for an
  anonymous candidate — the same fact that makes this a browser rather than a link — so the
  window is the only place the work exists.
- **`/api/session/file` is in `_UPLOAD_ROUTES`.** The browser's file picker shows the *server's*
  disk, so this upload is the file transfer, and a file route left out of that set reads its
  body as `{}` and reports "no file". Validation, naming and the atomic write are `resumes`' —
  there is no second way a file reaches this box.
- **The cap on the body is not the cap on the file** (2026-08-26). Base64 is ~4/3 of what it
  encodes, so a body capped at `resumes.MAX_UPLOAD` refused every file over about three quarters
  of the documented limit — and refused it *as an empty payload*, pointing at the picker instead
  of the size. `MAX_UPLOAD_BODY` bounds memory; `resumes.MAX_UPLOAD` describes a resume and is
  checked after the decode. Over-length is answered **413 in words**, not read as `{}`: it is
  the one refusal that happens with the body still arriving, so without an answer of its own the
  client sees a connection closing mid-upload.
- **Every ending of an upload has to reach the status pill and take `.busy` off.** Only a
  refusal the server answered used to move it, so a file the reader could not open, a body hung
  up on, or a request that never came back all left the row reading *"uploading…"* over a card
  still wearing `.busy`, which is what stops the poll repainting it. Success is `attaching…`,
  never `filled`: the upload queued a command, and only the poll can see it land.
- **Handlers live in `server._APPLY_JS`**, emitted only by `render_apply`. Parity test.
- **"Done — close the window" is the only ending a headless host can reach** (2026-08-19). With
  no screen to reach there was no way to end a session: the browser stayed up, `_APPLY_LOCK`
  stayed held, and every later "Open prefilled" refused until `serve` was restarted. `POST
  /api/session/close` sets `Session.closing`, the hold loop reads it in the tick it was already
  doing, and `fill_application` closes the context on the way out. **Deliberately not a
  `live.Command`** — the vocabulary is what a request may do to the *form* — and **deliberately
  not conditional on the phase**, because a session stuck mid-fill is exactly the one holding
  the lock. The refusal on the dashboard names the job and points at the page.
- **"Open prefilled" is also the way *back* to a window already open** (2026-08-26). "A window
  is already open" is three situations and only one is a collision; refusing all three made the
  button worse than the constraint it enforced, since it is the only route to `/apply`.
  `live.Session.holds` answers it: **the same posting** returns `ok:true, href:/apply` and
  starts nothing, **a different posting** is a swap (`_close_open_window` asks, waits for the
  browser thread to release `_APPLY_LOCK`, and takes its place), and **the lock held with no
  readable session** — the moment between acquiring it and `live.start` — is still a refusal,
  because there is nothing to ask to close. `CLOSED` is deliberately not "held".
  - **The lock is the proof, and nothing launches without it.** `_close_open_window` returns it
    *held*, because the caller's next act is to launch into it; releasing in between is a gap a
    second click walks through, and two threads racing for the one Chromium profile directory
    fail on the worker thread where nobody sees it.
  - **It blocks the request thread, bounded** (`SWAP_TIMEOUT_S`, 15s) — the same trade
    `/api/company`'s verification makes: the answer decides whether the click succeeds, so
    nothing on another thread can give it. A window still *filling* does not read `closing`
    until the fill lands, so a long form can outrun the timeout, and the refusal says which
    window and where the button is rather than waiting indefinitely.
- **This did not remove `DISPLAY` or Xvfb.** Chromium still draws somewhere and will not launch
  headful without a display. What it removed, as of 2026-08-22, was every *routine* reason to
  look at it. **Do not retire the viewer units on gx10** (`jobtracker-x11vnc`,
  `jobtracker-novnc`) — this file said they could be, and 2026-08-29 reversed that:
  `JOBTRACKER_BROWSER_VIEW_URL` is read again and `/apply` renders a link to them.
  `jobtracker-xvfb` was never retirable. Captchas are still stuck in the window, and so is every
  widget no write will move; the difference is that there is now a way to get there — see
  `docs/prefill.md`.

## Slug repair

`jobtracker repair`, documented in `docs/repair.md`. The **second** of the model's five bounded
roles as DESIGN.md §8 numbers them, and the only one where the model is a *fallback* rather
than the mechanism: deterministic regexes read the careers page first, and the model is asked
only about pages they could not parse. (The count went five → four when question matching was
removed on 2026-08-25 and back to five when `tailor` arrived on 2026-09-01; this role has been
number 2 throughout. What moved was `inbox`, fifth until the removed role vacated fourth.)

- **The trigger set is narrower than `is_degraded()` on one axis and wider on another.**
  `IDENTITY_DRIFT` fires immediately (a deterministic assertion over two stable strings).
  `FETCH_FAILED` needs `REPAIR_FAILURE_THRESHOLD` = 2 consecutive *nights* — `fetch.py` already
  burned `MAX_RETRIES` inside the run, so anything transient is absorbed a layer down, and
  rewriting a hand-verified slug because a CDN hiccuped once is the expensive mistake. Alerting
  `SUSPECT_EMPTY` is included because a dead board never presents as `FETCH_FAILED`.
- **Non-alerting `SUSPECT_EMPTY` never triggers.** dbt Labs and Root Insurance would otherwise
  be "repaired" every night. A test is named after them.
- **`manual` and `aggregator` companies are never targets.** A careers page is a scrape.
- **Nothing is proposed on the strength of a string.** Every candidate — regex or model — is
  fetched through the real `Source` adapter and rejected if the board is empty (the
  `greenhouse/hubspot` rule) or its identity does not match (the `ashby/cedar` rule). Both have
  named regression tests; do not weaken them.
- **`no_identity` is checked before `wrong_company`.** `health.identity_matches` returns True
  when either side is empty — right for the nightly loop, catastrophic here, where it would read
  "the identity endpoint 500'd" as "verified".
- **Ashby and Lever identity is tautological for a candidate slug**, being derived from the job
  URL and so just restating the candidate. Those proposals are accepted on *provenance* (the
  link came off the company's own careers page), labelled `evidence_kind: provenance` with
  sample titles attached. Never let that comparison read as identity proof.
- **A model slug must appear on the page it was shown**, after a `/` or `=`, case-sensitively.
  A bare substring test is not enough: the company name is all over its own careers page, so
  every invented slug would ground.
- **`repair` never writes without `--write`,** and `--write` moves `expected_board_name` along
  with the slug — leaving the dead board's name behind would make the repaired board drift on
  the very next run.
- **The companies.yaml writer is line-oriented, not a YAML round-trip.** PyYAML re-folds long
  strings to its own width, so a round-trip to change one `slug:` re-wraps `notes:` prose on
  unrelated entries — measured, ten of them. The deliverable is a diff somebody reads, and
  reflow noise destroys that. `verify-slugs --write` shares the writer; `add-company` was still
  round-tripping until 2026-08-19 and shares it now. All of it lives in `jobtracker/curation.py`.
- **Known blind spot, do not "fix" it in the regexes.** A careers page that renders its board
  link in JavaScript often contains no identifier at all — HubSpot's is 519 KB with neither
  `greenhouse` nor `hubspotjobs` in it. Those report `no_candidates` and stay visible.

## Adding a company

`jobtracker add-company`, and the `/companies` page under `serve`. Full guide in
`docs/companies.md`. Added 2026-08-19. Both doors share `jobtracker/curation.py` — one appender
and one validator, because two implementations of "append a curated entry" is how the button and
the terminal end up disagreeing about the file they both own.

- **`companies.yaml` has five writers, and every one is something you did on purpose.**
  `migrate` (once), `add-company`, `verify-slugs --write`, `repair --write`, and
  `POST /api/company`. No *scheduled* run writes it — the invariant DESIGN.md §2.3 protects.
  `serve` is a foreground process you started, and the write happens on a click you made.
- **The click is allowed here and still refused on a repair proposal.** Adding **appends an
  entry that did not exist**, from values you typed, and renders the exact unified diff it
  applied, computed from the same string handed to `safewrite`. Applying a repair **rewrites a
  hand-verified slug** on the machine's say-so, where the reviewable diff has to come *before*
  the write. `dashboard._proposal_cell` still has no apply button and must not grow one.
- **Appending is a third operation, not the existing writer.** `_edit_entry` cannot create a
  `- name:` block — an unknown name is a `KeyError`, by design — so `insert_entry` renders the
  new block alone and splices it in. Every pre-existing line survives byte-for-byte, with a test
  asserting exactly that, the same property `test_the_write_touches_only_the_lines_it_changes`
  guards for edits.
- **Placement is "before the first entry that sorts after me"**, not "after the last entry with
  my tier, else end of file". The second has no last entry for a tier the file does not use yet,
  so a tier-4 company falls past everything and lands under the untiered aggregator feeds.
  Untiered entries sort last, which is where all three already are.
- **`has_inline_comments` deliberately does not guard an append.** It exists for a round-trip
  that discards comments; an append rewrites no line and has none to lose. Adding it "for
  consistency" refuses a write that is provably safe.
- **`validate_new` is stricter than `load_companies`, and a test keeps it honest.** The loader
  must keep loading whatever is on disk; a new entry gets the strict pass, because
  `check_method: api` on an ats with no adapter is a board skipped behind one log line —
  indistinguishable from a board with nothing open. But **stricter than the live file is a rule
  that gets deleted the first time it fires**: two were already wrong when written. A `slug` on
  a `manual` entry is documentation (Red Hat's is a Workday tenant triple), and an aggregator
  with no `board_url` is parked on purpose.
- **A typed slug is labelled `reachable`, never `provenance`.** `judge_board` is the rule body
  shared with `judge_candidate`, taking the weak-evidence label as a *parameter* because the two
  callers claim different things. `provenance` means a careers page served the link. Nothing
  served a slug you typed, so the honest claim is only "the board answered and it is not empty"
  — and the page says so every time, with sample titles. Only Greenhouse gives real `identity`.
- **"Not verified" never renders as "verified".** A skipped check — `manual`, or *Add without
  verifying* — writes `expected_board_name: null` and says so. Writing the typed name would make
  the first nightly run either drift-alert on a name nobody checked or, because
  `identity_matches` returns True when either side is empty, silently pass. A verified save
  seeds it from **the name the ATS returned**, as `verify-slugs --write` does, so the fuzzy
  comparison happens once under human eyes.
- **`/api/company` is the one endpoint on this server that opens a socket, and it is bounded.**
  Everything else is CPU + SQLite (`_rebuild_plan`) or hands the blocking work to a daemon
  thread (`_api_apply_to`), because `HTTPServer` runs one request at a time. Verification cannot
  go on a thread: it decides whether the write happens, and nothing on a thread can answer the
  click that started it. So it stays inline and is capped — `Fetcher(max_workers=1, timeout=8,
  max_retries=1)`, at most two requests. `min_interval` is **not** overridden; per-host pacing
  is not something a waiting page gets to skip. A second verification is **refused, not queued**
  (`_VERIFY_LOCK`). A test pins the bound — widen it there first.
- **`ok` and `saved` are two axes.** A refused *board* is `ok:true, saved:false` with the
  evidence attached, because every `_JS` handler here opens `if (!res.ok) alert()` — return
  `ok:false` and the page swallows the escape hatch it is supposed to offer. `ok:false` is for a
  refused *request*, and validation failures have no "add anyway".
- **The file is re-read after verification, not before.** A fetch takes seconds, and a `repair
  --write` landing in that window would otherwise be clobbered by a splice computed against
  stale text.
- **Both buttons are rendered server-side**, `co-force` merely `hidden` until the script reveals
  it. The parity test reads button classes off the markup, so a button the JS mints is one
  nothing checks has a handler — the mechanism `.tabs` and `.cotoggle` use.

## Paged boards

`jobtracker/sources/workday.py`, added 2026-08-31. The first source whose board does not arrive
in one call, which is why `Source` grew `page_size`, `jobs_page_url`, `jobs_body` and
`jobs_page_error`, and why `fetch.py` grew `_fetch_paged` and a request body. Everything else
about it is an ordinary adapter: pure, registered by an import line.

The hooks are on the base class rather than inside the adapter because a second paged vendor
was built the same day (Amazon — see the manual-rule audit for why it was dropped) and hit the
*same* traps independently. These are properties of paged boards, not of Workday.

**Every trap below returns something that looks like an empty board.** None errors, none 404s,
and `sync_postings` would read each as "every posting closed". Each has a test named after it.

- **A page over the vendor's cap is not an empty page.** Workday caps at 20 rows: `limit: 50`
  returns HTTP 200, valid JSON, and **no `jobPostings` key at all** — not a clamp, not an error,
  a different shape. (Amazon does the same at its cap of 100, answering `result_limit=200` with
  `"jobs": null`.) Both parse to zero rows under the obvious `.get(..., [])`, which is what
  `jobs_page_error` exists to refuse. **Zero rows on a well-formed page is still allowed
  through** — a genuinely empty board is a different fact and belongs to `health.py`.
- **`total` is only populated on page one.** Workday reports `total: 0` on every request
  carrying a non-zero offset. A paging loop bounded by `total` stops after one page and keeps 20
  of 2,000 reqs, silently. Do not "simplify" the loop back to a total.
- **An offset past the end wraps to the beginning.** The other half of the same trap, and why a
  short-page rule is not sufficient alone. Measured against Nvidia 2026-08-31: `offset` 2000,
  3000, 4000 and 5000 all return the *same first row* as `offset=0`, twenty rows each, with
  `total` helpfully repopulated to 2000 — no short page and no error, ever. The first run of
  this adapter collected **4,000 postings for a board of 2,000**, the second half being the
  first half again, stopping only at the page cap ~100 requests later. `_fetch_paged` therefore
  has **two** stopping rules: a short page, and *a page that adds no posting id we do not
  already hold*. The second terminates Nvidia. Ids are the check because they identify a
  posting; a page that adds none has told us nothing, whatever its `total` claims. Tested.
- **`postedOn` is relative prose.** `"Posted Today"`, `"Posted 2 Days Ago"`, `"Posted 30+ Days
  Ago"` — resolved against `today`, which is why `normalize_posted_at` takes it as a parameter.
  `"30+"` is floored at exactly 30: a bound rather than a date, and 30 is its honest edge.
  Verified 2026-08-31: the detail payload's `startDate` is the day the prose counts from (a
  posting reading "Posted 2 Days Ago" carries `2026-08-29`), so the description fetch upgrades
  an approximation to the real date at no extra request, as Greenhouse's `first_published` does.
- **Workday can never claim identity.** Nothing in either payload names the employer. The only
  company-ish string is the tenant inside `externalUrl`, which restates the slug we asked for —
  the Ashby/Lever tautology that `ashby/cedar` sails through. `identity_from_jobs` returns
  `None` on purpose, `expected_board_name` stays null, and a Workday board is only ever
  evidenced as reachable. Do not "fix" this by reading the URL back.

Five more things that are not traps but will surprise you:

- **A Workday slug is a triple**, `tenant/dc/site`. No single string identifies the board: the
  data centre (`wd1`, `wd5`, `wd12`) is part of the hostname and has no relationship to the
  tenant name — Capital One is `wd12` while everything else tracked is `wd5`.
  `curation.validate_new` checks the triple's shape via the adapter's own `parse_slug`, so the
  validator and the fetcher cannot disagree. A wrong tenant answers **422**; a right tenant with
  a wrong site answers **401**, which is how Intuit's tenant was confirmed to exist while its
  site name is still unknown.
- **`parse_jobs` cannot build a Workday URL.** The row carries a site-relative `externalPath`
  and the payload names no host, so the URL can only come from the slug — which `parse_jobs` is
  not given. `Source.posting_url` fills it in from `fetch_company`, guarded on emptiness so it
  is a no-op for every adapter that already has one.
- **`externalPath` does not follow a rename, and the report will look wrong because of it.**
  Red Hat carries a req titled *"Account Solution Architect - FSI"* at
  `/job/Tokyo/Senior-Account-Solution-Architect---FSI_R-051257-2`, and two distinct reqs both
  titled *"AI Driven Development Consultant"* at `Agile-Development-Coach` and
  `Agile-Development-Lead`. The path is minted from the title the req had when created and then
  frozen; `title` is current. Both checked against the raw API 2026-08-31 — Workday's data, not
  a mis-paired parse. It is also why the path is the right `ats_job_id`: stable across the
  renames that would otherwise churn a posting as closed-and-new.
- **`appliedFacets` stays empty.** Workday will filter server-side, and doing so would be a role
  or location gate applied *before* any title is read — the `locations_exclude` mistake, which
  discarded 390 postings in 2026-07. Everything is fetched; `match.py` decides.
- **The cost is latency, and it is real.** Measured 2026-08-31: the `cxs` endpoint answers in
  ~2.4s and 20 rows at a time, so a 2,000-req board is ~100 requests and ~4 minutes. Five boards
  across four workers pulled 5,323 postings in **564s wall with 0.0s slept in the per-host
  limiter** — none of it our pacing, and none of it will tune away. A nightly run that was ~29s
  is now several minutes. Affordable for a 01:00 batch job, and worth knowing before someone
  goes looking for a regression.

## Aggregator sources

`jobtracker/sources/aggregator.py`. Community new-grad list repos (SimplifyJobs-style) are the
highest-yield source for new-grad roles specifically — they aggregate across every company,
including ones not on our list (DESIGN.md §9). One `check_method: aggregator` entry with a
`board_url` = one feed. The adapter parses the README's HTML `<table>`; the fetch is
`Fetcher.fetch_aggregator` → `_request_text` (text, not JSON), then it flows through the same
health/`sync_postings`/`match` loop as any board in `cmd_check`.

- **The feed is the `company`, the employer is in the title.** One feed lists many employers, so
  the feed name stays `Posting.company` (one stable diff namespace per feed) and
  `title = "Employer — Role"`. The title-only matcher reads that fine and the employer stays
  visible in the dashboard without a schema change. Caveat: an employer name containing a
  title-shaped exclude token would be conservatively rejected — near-zero risk, not yet observed.
- **`ats_job_id` is the Simplify `/p/<uuid>` when present, else a hash of employer+role.**
  Stable across runs so `sync_postings` recognizes the same row and closes a dropped one.
- **Closed rows (`🔒`) are skipped** — a filled req is not an opening. At last check 1,745 of
  2,072 rows were closed; ~318 open.
- **A missing `board_url` skips the feed** rather than failing the run. That is why the
  unverified Ouckah/CVrve entry costs nothing — the subsystem is generic, so it works the moment
  a confirmed URL is set (same table format).
- **Parsing tolerates garbage** (`[]` on any unexpected shape) — these repos rename by cycle and
  restyle the table; an empty feed is a visible SUSPECT_EMPTY, never a crash.

## Deployment

`docs/deployment.md`. **This repo does not know about orchestrators** — no Kubernetes manifests,
no systemd units in-tree. The deliverable is a container plus a documented contract; the units
live on the machine that runs them.

**CD publishes that container; it does not deploy it (2026-08-15).** Green `main` pushes
`ghcr.io/ida314/job-tracker:{sha-<sha>,latest}` from the `publish` job, built on
`ubuntu-24.04-arm` — arm64 only, matching the DGX Spark target and this aarch64 laptop. The
host **pulls**; nothing in CI holds credentials to a machine.

**There are two images, and the split is not cosmetic (2026-08-20).** `Dockerfile.serve` builds
`ghcr.io/ida314/job-tracker-serve` from `mcr.microsoft.com/playwright/python:v1.62.0-noble`;
`publish-serve` ships it beside the batch image. `serve` drives a real Chromium at a real ATS
form and the browsers are ~1.9GB against the batch image's 177MB — folding them together would
multiply the nightly pull by ten to carry something `check`, `work` and `prepare` never open.
The bases differ too: the app image is Debian trixie, which `playwright install --with-deps`
does not support.

Three things about that image that break silently if changed:

- **The base tag and the pinned `playwright` version are one unit.** `v1.62.0-noble` ships
  `chromium-1234`, the revision `playwright==1.62.0`'s driver launches. Bump one without the
  other and the driver looks for a browser that is not there.
- **`ENV JOBTRACKER_BROWSER_PROFILE` and `JOBTRACKER_RESUMES` must point into `/data`.**
  `config.py` resolves both relative to the package root, which is `/app` in a container — so
  the persistent Chromium profile (and any candidate-account login in it) and every uploaded
  resume would be written into a layer deleted with the container. The batch image needs
  neither, which is why this is the serve image's problem alone.
- **CI asserts `browser.unavailable_reason() is None` on the published image**, not just
  `import playwright`. `serve` reports a missing browser as a message on a card and carries on,
  so an image that lost Playwright would look exactly like a working deployment until someone
  clicked Open prefilled.

- **`sir-client` is baked in, and CI asserts `import sir_client` on the published image.**
  Without it `work` is a silent nightly no-op that still exits 0 — the same failure-is-absence
  shape as the `response_format` regression, reproduced at the deploy layer.
- **The PR-time `docker` job builds with `SIR_CLIENT` too**, and repeats publish's three
  assertions on the image it just built. The bare build exercises none of the SDK install path,
  which is the half that has broken; without the arg that break would surface on `main`,
  blocking publish after the merge that caused it. Publish still re-asserts against what it
  pushed — this is earlier, not instead.
- **The test job runs without `sir-client`,** because it is not on PyPI and pulling it from git
  would make the unit suite depend on another repo staying reachable. So no test may assume the
  SDK imports: `is_configured()` answers "is there an address" *and* "is there anything to dial
  it with", and two address tests asserting the first were red for runs 7–9, from the push that
  landed the sir-client migration until 2026-08-15. Force the world you mean with the
  `sdk_installed` fixture; the SDK-absent world has its own test, which builds an SDK-less
  module rather than reading the environment.

  **`publish` was added during that red window and so had never once run.** The pipeline was
  not slow to deploy, it was structurally incapable of it, and nothing said so: `needs: [test,
  docker]` skips silently, so the missing publish reads as ordinary gating rather than as "no
  image has ever been pushed". Check the registry, not the workflow file, when asking whether CD
  works.
- **Two Dockerfile traps, both fixed 2026-08-15.** `python:*-slim` carries **no git**, so the
  `SIR_CLIENT="git+ssh://…"` invocation this file documented could never have worked; git is now
  installed and purged inside one `RUN` (+8MB net). And a credential must **never** be a build
  arg — build args are readable with `docker history` on the published image. The Dockerfile
  takes an optional BuildKit secret instead and wipes `/root/.gitconfig` in the same layer. The
  router repo is public today, so no secret is needed; if it goes private the clone fails loudly
  rather than shipping an SDK-less image.
- **Every run names its own build**: `jobtracker 0.1.0+<sha12>`, from `JOBTRACKER_REVISION`
  (`ARG GIT_SHA`), logged by `main()` for every subcommand and set as `service.version`. A host
  that failed to pull is otherwise indistinguishable from one that succeeded — identical
  runtime, identical report, exit 0. A bare `0.1.0` means not running from a published image.
- **Rollback is by tag.** Pin `Image=` to a good `:sha-…`. `podman auto-update`'s rollback needs
  a healthcheck, which a 32-second batch job does not have.
- Forward image updates are safe unattended: `store.py` does `CREATE TABLE IF NOT EXISTS` plus
  additive column migrations on connect, so a new image upgrades `state.db` in place.
- **The host half is not written yet.** gx10 is a DGX Spark on the tailnet; whether it runs
  podman/quadlet (like the laptop) or Docker (DGX OS's default) is unconfirmed, and that decides
  the unit shape. Nothing is deployed there today.

- `check` exits 0 (clean), 2 (a board needs attention), or 1 (could not run).
- `serve` is the one long-running process, so it carries the service affordances: `/healthz`
  (liveness — a constant, never touches the DB), `/readyz` (readiness — 503 until state.db opens
  *and* criteria.yaml parses, payload names which failed), and it drains the in-flight request
  and exits 0 on SIGTERM/SIGINT. The app ships the endpoints; the deployment repo decides how to
  poll them.
- Exit 2 is narrower than `status != OK` — see `health.is_degraded()`. dbt Labs and Root
  Insurance are permanently `suspect_empty` and must never fail a run.
- **Containers must set `TZ`.** The image is UTC; `date.today()` drives `first_seen`, the
  report's `since` window, and `manual_due()`. A UTC container running at 21:00 local stamps
  tomorrow onto everything.
- `otel/stack.sh run` mounts the repo's `./data` — a **real run against real state.db**, not a
  smoke test. Point `JOBTRACKER_DB` at a scratch copy to exercise the stack without writing
  history.

## Repo conventions

- Each change to the tracker is its own commit, grouped by *failure class* (not by tier), so one
  class of mistake can be reverted with `git revert` without losing the rest.
- The commit before any correction is the `pre-audit baseline`; it exists purely as a revert
  target.
