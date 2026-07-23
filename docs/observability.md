# Observability

Three signals, all off by default. `CLAUDE.md` has the design rules; this is the reference.

```
jobtracker ──OTLP──> collector ──┬── traces ──> Jaeger      (one run, in detail)
 (30s, exits)                    └── metrics -> Prometheus  (trends across runs)
                                                    └─────> Grafana (draws both)
```

| Mode | Logs | Traces | Metrics |
|---|---|---|---|
| default | stderr | — | — |
| `--telemetry console` | stderr | stderr (JSON) | stderr (JSON) |
| `--telemetry otlp` | stderr | collector | collector |

Env equivalents: `JOBTRACKER_TELEMETRY`, `OTEL_EXPORTER_OTLP_ENDPOINT`.

## Logs

`logging` to **stderr**; the report goes to stdout, so `check > out.md` stays clean.
INFO gives one line per board, `-v` is DEBUG (per HTTP attempt), `-q` is warnings only.

## Spans

One trace per run. `jobtracker/fetch.py` is the only instrumented module.

```
fetch.all                    companies.count, max_workers, failed.count,
│                            retries.count, rate_limited.seconds
└── fetch.company            company.name, company.ats, company.slug,
    │                        fetch.postings.count, fetch.ok, board.observed_name
    └── http.request         http.request.method, url.full, server.address,
        │                    http.response.status_code, http.resend_count
        │                    events: rate_limited, retry
        └── GET              auto-instrumented (opentelemetry-instrumentation-requests)
```

Retries are span **events**, not extra spans — one `http.request` covers all attempts.
Span status is set to ERROR on failure; an unset status means "no opinion", not "fine".

## Metrics

OTel names use dots; Prometheus converts to underscores and appends unit + `_total`.

| OTel | Type | Attributes | In Prometheus |
|---|---|---|---|
| `jobtracker.fetch.duration` | histogram (s) | `ats`, `outcome` | `jobtracker_fetch_duration_seconds_{bucket,sum,count}` |
| `jobtracker.fetch.postings` | histogram | `ats` | `jobtracker_fetch_postings_{bucket,sum,count}` |
| `jobtracker.fetch.retries` | counter | `server.address` | `jobtracker_fetch_retries_total` |
| `jobtracker.fetch.rate_limited.time` | counter (s) | `server.address` | `jobtracker_fetch_rate_limited_time_seconds_total` |
| `jobtracker.run.duration` | histogram (s) | — | `jobtracker_run_duration_seconds_{bucket,sum,count}` |
| `jobtracker.boards` | counter | `health.status`, `ats` | `jobtracker_boards_total` |
| `jobtracker.postings.new` | counter | — | `jobtracker_postings_new_total` |
| `jobtracker.matches` | counter | — | `jobtracker_matches_total` |

A counter that never fires has **no series at all** — `jobtracker_fetch_retries_total`
is absent on a clean run. Absent ≠ zero.

## Three batch-job traps

This is a 30-second process, not a server. Each of these fails silently if undone.

1. **Metrics are pushed, never scraped.** Nothing is up when a scrape lands.
2. **Counters export as DELTA**, reassembled by the collector's `deltatocumulative`.
   Cumulative would reset to zero every run and read as a decrease.
3. **`service.instance.id` is pinned to the hostname**, or every run mints a new series.

## Querying

Two separate traps stack here. Both were verified empirically against a live stack.

**Trap 1 — the 5-minute lookback.** Instant queries look back only 5 minutes, so a
30-second daily job's series read as "No data" ~99.6% of the day.

**Trap 2 — the counters are cumulative by the time Prometheus sees them.** The job
exports DELTA and the collector's `deltatocumulative` rebuilds a monotonic series. So
`last_over_time` on a counter returns the **all-time total**, not last night's run.
Wrapping in `last_over_time` fixes trap 1 and walks straight into trap 2:

```promql
# WRONG — blank ~99.6% of the day (trap 1)
jobtracker_boards_total

# ALSO WRONG — 62 boards a night reads 62, then 124, then 186 (trap 2)
sum by (health_status) (last_over_time(jobtracker_boards_total[24h]))

# RIGHT — how much the counter rose inside the window
sum by (health_status) (increase(jobtracker_boards_total[24h]))

# Histogram averages: divide the sum's rise by the count's rise
increase(jobtracker_run_duration_seconds_sum[24h]) / increase(jobtracker_run_duration_seconds_count[24h])

# Rate/histogram quantiles over a long window need no wrapping.
# `le` MUST stay in the `by` clause or the result is silently meaningless.
histogram_quantile(0.95, sum by (le, ats) (rate(jobtracker_fetch_duration_seconds_bucket[24h])))
```

`increase()` has one cold-start caveat: it cannot see a rise that happened before the
series' first sample, so on a freshly-wiped Prometheus the first day reads 0. It
self-corrects on day two.

**"How long since the last run?"** is its own trap, and it is the one query that catches
a job that stopped running — every other panel keeps happily showing last night's
numbers forever. The obvious idiom is silently broken:

```promql
# BROKEN — always returns 0. last_over_time re-stamps the sample at evaluation time,
# so the panel reads "just ran" even when the job has been dead for a week.
time() - timestamp(last_over_time(jobtracker_run_duration_seconds_count[24h]))

# RIGHT — the subquery evaluates timestamp() at 1m steps and keeps the newest real one.
# Verified at 885s against a true 14m45s, long past the 5m lookback.
time() - max_over_time(timestamp(jobtracker_run_duration_seconds_count)[24h:1m])
```

## The dashboard

`otel/grafana-dashboard.json` is provisioned by `otel/grafana-dashboards.yml`, so it
comes up with the stack — no import step. It encodes every query above, and each panel's
**description field carries the reasoning** (hover the ⓘ): why `suspect_empty: 2` is the
expected steady state, why an empty retries panel is healthy, what a suspiciously *fast*
run means.

Two panel-level decisions worth keeping:

- **Counter panels set no-data to `0`.** A counter that never fires has no series at all,
  so `jobtracker_fetch_retries_total` is absent on a clean run. Absent is correct but
  renders identically to a broken panel.
- **Row 1 is pinned to a fixed 24h, not the dashboard time range.** "Did last night's run
  work" is a question about last night regardless of what the time picker says.

Editing it: the file is the source of truth. `allowUiUpdates: true` lets you experiment
with a panel live, but a restart overwrites anything not saved back into the JSON.

## Running the stack

```sh
otel/stack.sh up      # collector + Jaeger + Prometheus + Grafana
otel/stack.sh run     # a check run through it (rebuild the image first)
otel/stack.sh down
```

Jaeger http://localhost:16686 · Grafana http://localhost:3000 · Prometheus http://localhost:9090

`compose.yaml` describes the same stack, but needs a compose provider installed.
