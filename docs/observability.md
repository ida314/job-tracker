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

Prometheus instant queries look back only **5 minutes**, so a daily job's series are
stale almost always. Wrap point-in-time reads:

```promql
# WRONG — blank ~99.6% of the day
jobtracker_boards_total

# RIGHT
sum by (health_status) (last_over_time(jobtracker_boards_total[24h]))

# Rate/histogram queries over a long window need no wrapping
histogram_quantile(0.95, sum by (le, ats) (rate(jobtracker_fetch_duration_seconds_bucket[24h])))
```

## Running the stack

```sh
otel/stack.sh up      # collector + Jaeger + Prometheus + Grafana
otel/stack.sh run     # a check run through it (rebuild the image first)
otel/stack.sh down
```

Jaeger http://localhost:16686 · Grafana http://localhost:3000 · Prometheus http://localhost:9090

`compose.yaml` describes the same stack, but needs a compose provider installed.
