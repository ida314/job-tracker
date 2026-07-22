# Job Tracker

A deterministic pipeline that checks ~89 companies daily for backend new-grad (2027)
openings and reports new matches. It replaces a v1 that used an LLM as the runtime; the
rationale for the rewrite is in [`DESIGN.md`](DESIGN.md), the operating rules in
[`CLAUDE.md`](CLAUDE.md).

Fetching, parsing, diffing, matching, and storing are ordinary tested code. A language
model is **not** in the loop — the residual it would handle is surfaced as an
`UNCERTAIN` bucket for now (DESIGN.md §6).

## Layout

```
companies.yaml   curated targets — human-authored, git-tracked, NEVER machine-written
criteria.yaml    match rules — validated on load (the v1 bug was invalid, unparsed YAML)
jobtracker/      the package (models, sources, fetch, match, health, store, report, cli)
data/state.db    run state — SQLite, gitignored, lives on a mounted volume in the container
tests/           unit + integration suite
otel/            optional observability stack (collector, Prometheus, Grafana configs)
docs/            reference notes — see docs/observability.md
```

State (`postings`, `verdicts`, `board_health`, `runs`) is separate from curation, so
`companies.yaml`'s git history stays a clean record of curation decisions.

## Commands

```bash
python -m jobtracker.cli migrate         # backend-newgrad-2027-tracker.md -> companies.yaml (one-time)
python -m jobtracker.cli verify-slugs    # fetch each board's identity; print observed names
python -m jobtracker.cli verify-slugs --write   # + seed expected_board_name for drift detection
python -m jobtracker.cli check           # the daily run: fetch -> health -> store -> match -> report
python -m jobtracker.cli rematch         # re-apply criteria to stored postings (no network)
python -m jobtracker.cli report          # re-render the latest state (no network)
python -m jobtracker.cli add-company --name X --ats greenhouse --slug x --tier 2
```

`check` writes the report to **stdout**; progress goes to **stderr**, so `check > report.md`
stays clean. `--output report.md` writes the file directly.

Global flags (before the subcommand): `-v` for per-request DEBUG, `-q` for warnings only,
`--telemetry {off,console,otlp}`.

## Local dev

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
JOBTRACKER_DB=data/state.db python -m jobtracker.cli check
```

## Container

State lives on a mounted volume, so the image is disposable.

```bash
podman build -t jobtracker:latest .          # or: docker build
mkdir -p data
podman run --rm -v "$PWD/data:/data:Z" jobtracker:latest check
```

The `:Z` suffix relabels the volume for SELinux (required on Fedora); on Docker/non-SELinux
hosts it is harmless but can be dropped.

Daily via cron (host):

```cron
0 8 * * *  cd /home/dylan/Projects/Job-Tracker && podman run --rm -v "$PWD/data:/data:Z" jobtracker:latest check --output /data/report-$(date +\%F).md 2>> data/cron.log
```

`--output` keeps the report out of the log; only progress lines land in `cron.log`. (Cron
needs `%` escaped as `\%`.)

To change targets or rules, edit `companies.yaml` / `criteria.yaml` and rebuild (they are
baked into the image), or bind-mount them for iteration:
`-v "$PWD/companies.yaml:/app/companies.yaml:ro"`.

## Observability

Progress logging is always on. Traces and metrics are opt-in and off by default — the
pipeline runs identically without any of the stack below. Full reference:
[`docs/observability.md`](docs/observability.md).

```
22:01:29 INFO  fetching 56 boards (4 workers)
22:01:30 INFO  [ 1/56] Stripe                   greenhou  518 jobs (1.1s)
...
22:01:54 INFO  fetched 56 boards in 28.9s — 0 failed, 0 retries, 106.3s in per-host pacing
```

That last line is the one to read: ~29s wall with ~106s of pacing summed across 4 workers
means the run was rate-limiter-bound, not broken. Compare before assuming breakage.

### Traces and metrics

`--telemetry console` prints spans and metrics to stderr — no infrastructure needed, and
the quickest way to see what is instrumented.

`--telemetry otlp` ships to a collector. The stack — collector → Jaeger for traces,
Prometheus for metrics, Grafana over both — is described in `compose.yaml`, but
`podman compose` needs a compose provider installed, so `otel/stack.sh` runs the same
four containers with plain podman:

```bash
./otel/stack.sh up                   # plain podman — no compose provider needed
podman build -t jobtracker:latest .  # rebuild first: the OTel packages are new deps
./otel/stack.sh run                  # a check run through the stack
./otel/stack.sh down
```

Jaeger <http://localhost:16686> · Grafana <http://localhost:3000> · Prometheus
<http://localhost:9090>

Two things that surprise people, both because this is a 30-second batch job rather than a
server — see `docs/observability.md` for why:

- Metrics are **pushed**, never scraped. Nothing is running when a scrape lands.
- Prometheus instant queries look back only 5 minutes, so a daily job's series read as
  "No data" almost always. Wrap point-in-time queries in `last_over_time(...[24h])`.

## Health states (why a board is flagged)

| Status | Meaning |
|---|---|
| `ok` | reachable, identity confirmed, postings trusted |
| `suspect_empty` | reachable but zero postings — *not* "no openings"; alerts only after repeated empties on a board that was once populated |
| `identity_drift` | reachable but the board's name no longer matches `expected_board_name` (the ashby/cedar collision) — contents discarded |
| `fetch_failed` | non-200 / timeout / bad JSON — retried, reported, never counted as "no jobs" |

## Coverage

`api` companies (56) are checked automatically. `manual` companies (31 — Workday, bespoke
portals, Gem) have no keyless JSON board and are surfaced in a weekly "check by hand"
list, never scraped. The 2 aggregator sources are not yet wired (DESIGN.md §9).
