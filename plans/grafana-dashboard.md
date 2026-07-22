# Plan: provisioned Grafana dashboard

**Status:** proposed, not started
**Depends on:** tier-3 stack (`compose.yaml`, `otel/`), metrics in `fetch.py` / `cli.py`
**Reference:** `docs/observability.md` for signal and metric definitions

## Why

The query knowledge for this stack currently exists nowhere in the repo. Someone opening
Grafana sees 13 metric series and no indication which matter, and the three things that
took real effort to get right would have to be rediscovered:

1. `last_over_time(...[24h])` wrapping — without it panels are blank ~99.6% of the day,
   because Prometheus instant queries look back only 5 minutes and this job runs 30s/day.
   The natural conclusion on hitting this is "the pipeline is broken," which is wrong.
2. The `histogram_quantile(0.95, sum by (le, ...) (rate(...)))` shape, which is easy to
   get subtly and silently wrong.
3. Post-conversion label names — `health_status`, not `health.status`.

A dashboard encodes all three in a form that executes. It also records an opinion about
*which* of the 13 series are the job's vital signs, which is a judgment currently written
down nowhere.

Scope note: this documents **what** to watch. The **why** stays in `CLAUDE.md` — see the
panel-descriptions item below, which is the one mechanism bridging the two.

## Deliverable

- `otel/grafana-dashboard.json` — the dashboard
- `otel/grafana-dashboards.yml` — provider config pointing Grafana at it
- three small edits: volume mounts in `compose.yaml` and `otel/stack.sh`

Provisioned like the datasources already are, so it comes up with the stack and needs no
import step. No changes to `jobtracker/` — this is presentation over existing metrics.

## Panels

Two rows, matching the two questions the stack answers.

### Row 1 — "Did last night's run work?"

Every query wrapped in `last_over_time(...[24h])`.

| Panel | Query basis | Why |
|---|---|---|
| Boards by health status | `sum by (health_status) (last_over_time(jobtracker_boards_total[24h]))` | The daily glance. Stat tiles; `ok` green, others amber/red |
| Last run duration | `last_over_time(jobtracker_run_duration_seconds_sum[24h])` | ~30s is normal; 90s means retries |
| New postings / matches | `jobtracker_postings_new_total`, `jobtracker_matches_total` | Zero new postings across all boards is suspicious |
| Time since last run | `time() - timestamp(last_over_time(...))` | **Most important panel.** Catches "the job stopped running", which every other panel misses — they all just keep showing the last good value |

### Row 2 — "Is anything degrading?"

Time series over long windows; no `last_over_time` wrapping needed.

| Panel | Query basis |
|---|---|
| p95 fetch duration by `ats` | `histogram_quantile(0.95, sum by (le, ats) (rate(jobtracker_fetch_duration_seconds_bucket[24h])))` |
| Retries by host | `sum by (server_address) (increase(jobtracker_fetch_retries_total[24h]))` |
| Pacing vs wall time | `jobtracker_fetch_rate_limited_time_seconds_total` against run duration — makes the pacing-vs-breakage question permanently answerable |
| Postings per board | `histogram_quantile(0.5, ...jobtracker_fetch_postings_bucket...)` — detects boards trending toward empty |

## Two design decisions

**Empty panels must render `0`, not "No data".** A counter that never fires has no series
at all, so `jobtracker_fetch_retries_total` is absent on a healthy run. Correct behavior,
but visually identical to a broken panel. Set each counter panel's no-data state to zero.

**Panel descriptions carry the why.** Grafana panels have a description field shown on
hover. That is where facts like "dbt Labs and Root Insurance are legitimately empty — do
not 'fix' them" belong. Without this the dashboard shows `suspect_empty: 2` and invites
someone to repair two healthy boards.

## Verification

Not "the JSON parses". Panels must return values against real data:

1. Bring the stack up; run `check` several times, including one forced failure (bogus
   slug) so error paths and the retries counter have data.
2. Query each panel's expression through the Grafana API and assert non-empty results.
3. Confirm the "time since last run" panel increases as expected between runs.

## Estimated size

~250 lines of JSON, one provider YAML, three small edits. Half a session.

## Open questions

- Alerting: worth adding Grafana alert rules (e.g. `suspect_empty > 3`, no run in 36h),
  or is a dashboard enough for a personal job tracker? Alerts need a notification channel,
  which is real setup.
- Should the dashboard include a Jaeger panel (Grafana has the datasource provisioned) to
  jump from a slow run straight into its trace? Nice, but couples the dashboard to Jaeger
  being up.
