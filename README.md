# Job Tracker

A deterministic pipeline that checks ~89 companies daily for backend new-grad (2027)
openings and reports new matches. It replaces a v1 that used an LLM as the runtime; the
rationale for the rewrite is in [`DESIGN.md`](DESIGN.md), the operating rules in
[`CLAUDE.md`](CLAUDE.md).

Fetching, parsing, diffing, matching, and storing are ordinary tested code. A language
model is **not** in the loop. Titles the rules cannot honestly judge land in an
`UNCERTAIN` bucket, which an optional, entirely local pass can resolve by reading job
descriptions — see [`docs/llm.md`](docs/llm.md). With no model configured, nothing
about the pipeline changes.

## Layout

```
companies.yaml   curated targets — human-authored, git-tracked, NEVER machine-written
criteria.yaml    match rules — validated on load (the v1 bug was invalid, unparsed YAML)
profile.yaml     what you are optimizing for — prose for the model, weights for the sort
jobtracker/      the package (models, sources, fetch, match, health, store, rank, cli)
jobtracker/llm/  optional local inference providers — same registry shape as sources/
data/state.db    run state — SQLite, gitignored, lives on a mounted volume in the container
tests/           unit + integration suite
otel/            optional observability stack (collector, Prometheus, Grafana configs)
docs/            reference notes — see the table below
```

| Doc | Covers |
|---|---|
| [`docs/deployment.md`](docs/deployment.md) | Running it unattended: the container contract, exit codes, and five silent failure modes |
| [`docs/tuning.md`](docs/tuning.md) | Fixing bad matches so they stay fixed — decisions, `eval`, suggestions |
| [`docs/llm.md`](docs/llm.md) | The optional local ambiguity pass, and the router the model calls go through |
| [`docs/tasks.md`](docs/tasks.md) | The task queue: what the model works on next, and why that order |
| [`docs/prefill.md`](docs/prefill.md) | Opening an application with your answers already in it |
| [`docs/applications.md`](docs/applications.md) | The outer loop: stages, reminders, and what became of what you sent |
| [`docs/companies.md`](docs/companies.md) | Adding a target: what is validated, what is verified against the live board, and what is written |
| [`docs/mail.md`](docs/mail.md) | Reading your job-search mailbox for replies, and proposing what they mean |
| [`docs/ranking.md`](docs/ranking.md) | Picking the three to apply to tomorrow, and tuning it without a GPU |
| [`docs/observability.md`](docs/observability.md) | Traces, metrics, and the query idioms that are not obvious |

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
python -m jobtracker.cli dashboard       # render state.db to data/dashboard.html (no network)
python -m jobtracker.cli add-company --name X --ats greenhouse --slug x --tier 2 \
    --careers-page https://x.example/careers   # see docs/companies.md

# slug repair — see docs/repair.md
python -m jobtracker.cli repair          # broken boards -> careers page -> verified proposal
python -m jobtracker.cli repair --write  # apply the proposals (nothing is written without it)

# tuning — see docs/tuning.md
python -m jobtracker.cli decide Stripe 7966029 reject --note "operations, not engineering"
python -m jobtracker.cli eval            # replay criteria against your judgments; exits 1 on a regression
python -m jobtracker.cli serve           # live tuning UI on http://127.0.0.1:8765

# the model tasks — see docs/tasks.md
python -m jobtracker.cli work --dry-run               # what it would work on, and why
python -m jobtracker.cli work --llm-url http://HOST:PORT
python -m jobtracker.cli work --task prefill --budget 20

# ranking — see docs/ranking.md
python -m jobtracker.cli rank --llm-url http://HOST:PORT
python -m jobtracker.cli today                        # the three to apply to
python -m jobtracker.cli today --applied Stripe 7966029
python -m jobtracker.cli today --snooze  Stripe 7966029 --days 14

# applications, the outer loop — see docs/applications.md
python -m jobtracker.cli applications                 # what you applied to, and what it needs
python -m jobtracker.cli apply Stripe 7966029 --status interview --note "round 2"
python -m jobtracker.cli apply "Some Startup" --manual --title "Backend Eng (Referral)"

# what the employers said back — see docs/mail.md
export JOBTRACKER_MAILDIR=~/Mail/jobs                 # read-only, never written
python -m jobtracker.cli mail                         # narrow the mailbox, no model
python -m jobtracker.cli work --task inbox            # read the candidates
python -m jobtracker.cli mail --list                  # proposals awaiting your ruling

# prefilled applications — see docs/prefill.md
cp answers.example.yaml answers.yaml                  # gitignored; holds your details
python -m jobtracker.cli prepare                      # make tomorrow's picks ready
python -m jobtracker.cli apply-to Cloudflare 7695702  # opens a browser, fills, stops
```

**The nightly sequence is `check` → `work` → `prepare` → `dashboard`,** and only `check`
touches an ATS. It caches a description for every match/uncertain posting, which is what
lets the rest read `state.db` and talk to nothing but the local inference router.

`work` picks the task itself, in the pipeline's own dependency order — settle uncertain
postings, judge the matches that produces, prefill the best of those. Run it more than
once to drain more than one stage. `prepare` then makes sure tomorrow's three picks each
have a prefill plan, and exits 2 if one does not.

`check` writes the report to **stdout**; progress goes to **stderr**, so `check > report.md`
stays clean. `--output report.md` writes the file directly.

**Exit codes.** `check` returns `0` when no board needs attention, `2` when at least one
does, and `1` if it could not run at all. Exit `2` is narrower than "something was not
OK": dbt Labs and Root Insurance are correct slugs with genuinely zero reqs, so a healthy
run reports `60 ok, 2 failed` and still exits `0`. Details in
[`docs/deployment.md`](docs/deployment.md).

**When a board does break,** `repair` reads its careers page for the new slug, verifies
the candidate against the live API, and prints a diff. It never writes `companies.yaml`
without `--write`, so it is safe to run unattended after a degraded night. See
[`docs/repair.md`](docs/repair.md).

## Three views

They answer different questions and are independent — each works without the others.

| | `jobtracker dashboard` | `jobtracker serve` | Grafana (`otel/grafana-dashboard.json`) |
|---|---|---|---|
| Question | *What should I apply to today?* | *Why did this match, and how do I fix it? What came of what I sent?* | *Did last night's run work?* |
| Source | `state.db` | `state.db` + `criteria.yaml` | Prometheus metrics |
| Needs | nothing — one HTML file | a local process | the tier-3 stack up |
| Writes | never | only on POST | — |

`serve` does not replace `dashboard`. The static file is a snapshot you can mail to
yourself and open offline years from now; that property is worth keeping, so `serve` is
a second surface for the one thing a static file cannot do — write back. It has five
pages: the dashboard, `/applications` (add a job by hand, move a stage, set a reminder),
`/companies` (the tracked list, and a form that verifies a board before adding it),
`/tuning`, and `/settings`.

```bash
python -m jobtracker.cli dashboard      # -> data/dashboard.html, open it with file://
```

Four tabs. **Today** is the landing screen: the three jobs to apply to, each with the
model's one-line reasoning, its fit/growth/risk breakdown, and an Apply link.
**Applications** is what came of the ones you sent — stages, history, and what needs
following up. **All postings** holds the full open-match and uncertain lists, filterable
by tier / ATS / location / text. **Boards** holds flagged boards and the manual companies
that are never scraped.

Self-contained: no server, no network at view time, works offline, and readable with
JavaScript disabled — every panel is rendered server-side and the script only hides the
inactive ones. It is a pure read; unlike `report`, it never writes to the database. The
applied/skip/snooze buttons appear only under `serve`, which is the surface that can
write.

**Location ranks, it never disqualifies.** Results come out NYC first, then the rest of the
US, then unspecified, then abroad — but nothing is dropped for where it is, and the location
filter defaults to "Anywhere". See `criteria.yaml` for the lists and `match.location_rank()`
for the ordering (including why an unspecified location outranks an explicitly foreign one).

Global flags (before the subcommand): `-v` for per-request DEBUG, `-q` for warnings only,
`--telemetry {off,console,otlp}`.

## Local dev

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
JOBTRACKER_DB=data/state.db python -m jobtracker.cli check
```

Filling application forms is an optional extra, because it pulls a browser:

```bash
pip install 'jobtracker[browser]' && playwright install chrome
```

Without it, `apply-to` and `serve`'s "Open prefilled" button say so; everything else
works unchanged, and the `browser`-marked tests skip. The window opens **on the machine
running the command**, so a headless host needs a display (`Xvfb :100`).

Under `serve` you never touch that window. "Open prefilled" opens it and sends you to
**`/apply`**, which mirrors every field of the real form onto a page with no latency
between you and the keyboard, plus a still of the whole page as the browser sees it.
Typing there fills the real form and deleting there empties it. The window draws on a
display because Chromium has to; nothing links you to it. See `docs/prefill.md`.

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

To change targets, rules, or what the ranking optimizes for, edit `companies.yaml` /
`criteria.yaml` / `profile.yaml` and rebuild (they are baked into the image), or
bind-mount them for iteration:
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

Grafana comes up with the **Job Tracker — daily run** dashboard already provisioned: a
"did last night's run work" row and an "is anything degrading" row. Every panel has a
description explaining what it means and when *not* to act on it.

Three things that surprise people, all because this is a 30-second batch job rather than
a server — see `docs/observability.md` for why:

- Metrics are **pushed**, never scraped. Nothing is running when a scrape lands.
- Prometheus instant queries look back only 5 minutes, so a daily job's series read as
  "No data" almost always.
- The counters are **cumulative** by the time Prometheus sees them, so `last_over_time`
  gives the all-time total rather than last night's run. Use `increase(...[24h])`.

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
