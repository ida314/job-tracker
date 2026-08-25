# Running Job Tracker unattended

Job Tracker does not know what schedules it, and it should stay that way. It ships a
container and the contract below; systemd, Kubernetes, a CI runner, or `sh` at a
terminal are all equally valid callers. Nothing in this repo names an orchestrator —
the example units here are meant to be copied onto the machine that runs them, not
committed.

The contract is small enough to state in full:

| | |
|---|---|
| **Command** | `check` — the only subcommand that touches the network |
| **Exit 0** | Ran, and no board needs attention |
| **Exit 2** | Ran, but ≥1 board is broken. The run still completed and stored data |
| **Exit 1** | Did not run. Bad config, unreadable DB — a traceback on stderr |
| **stdout** | The markdown report, and nothing else. `check > out.md` stays clean |
| **stderr** | Progress logs. `-q` reduces to warnings and errors |
| **Writes** | `$JOBTRACKER_DB` only. Nothing else on disk is modified |
| **Runtime** | ~32s for 62 boards, of which ~117s summed across 4 workers is deliberate pacing |

Runs are idempotent. A second run against the same database re-fetches every board and
reports `0 new postings`; it does not double-count.

## Where the image comes from

Every green push to `main` publishes one, to two tags:

```
ghcr.io/ida314/job-tracker:sha-<full-sha>   immutable; what you pin to and roll back to
ghcr.io/ida314/job-tracker:latest           moving; what an auto-updating host follows
```

Built on `ubuntu-24.04-arm`, so the image is **arm64 only** — that matches the DGX Spark
it deploys to and the aarch64 laptop it is developed on. A host on amd64 has no image to
pull; making one means adding a platform to the buildx call, not a second job.

`publish` is gated on `test` and `docker` both passing and on the ref being `main`. The
pipeline pushes an artifact and stops there: **nothing in CI reaches into a machine.**
The host pulls. That is a deliberate choice for a homelab target with no stable inbound
address — a registry read token on the box is a far smaller blast radius than deploy
credentials for the box living in a CI provider.

`sir-client` is baked in and CI **verifies it imports** before the tag is considered
good. Publishing an image without it would leave `work` no-opping in silence every
night, since a model it cannot reach is a legitimate exit-0 condition. That is the one
failure this pipeline is specifically shaped to prevent.

## Which build is running

The `check`/`work`/`prepare`/`dashboard` sequence looks identical whether or not last
night's image was actually pulled: same runtime, same report, exit 0. So every run
opens by naming its own build —

```
jobtracker 0.1.0+b64427938711
```

— from `JOBTRACKER_REVISION`, stamped by the image build (`ARG GIT_SHA`). It is also
`service.version` on the OTel resource, so a Grafana panel can show the deployed
revision alongside the run it produced.

A bare `0.1.0` with no `+sha` means the process is **not** running from a published
image — a working tree, or a locally-built one. Outside a build there is no commit to
name and the stamp says so rather than guessing.

To check the host from here:

```sh
journalctl --user -u jobtracker.service -n 200 | grep -o 'jobtracker 0\.[0-9.]*+[0-9a-f]*' | tail -1
```

Compare against `git rev-parse --short=12 origin/main`. If they differ, the pull did not
happen; the image is not the thing to debug.

Rollback is by tag, not by tooling: pin `Image=` to a known-good `:sha-…` and reload.
`podman auto-update`'s own rollback keys off a container healthcheck, which a batch job
that exits in 32 seconds does not have — do not expect it to catch a bad build.

Schema changes need no coordination: `store.py` applies `CREATE TABLE IF NOT EXISTS`
plus additive column migrations on every connect, so a newer image upgrades `state.db`
in place on first run, and an older one ignores columns it does not know about.

## Exit 2 is narrower than "something was not OK"

Two boards — dbt Labs and Root Insurance — are correct slugs with genuinely zero open
reqs. They report `suspect_empty` on **every** run and always will. A healthy nightly
run therefore logs `60 ok, 2 failed` and exits **0**.

That is deliberate. If those two failed the run, the unit would be red every night,
which is indistinguishable from having no signal at all — and the night a board really
breaks would look like every other night. `health.is_degraded()` draws the line:
`fetch_failed` and `identity_drift` always count; `suspect_empty` counts only once it is
`alerting`, which by construction requires the board to have been populated before. A
board that went from 500 postings to zero is an emergency. A board that was always zero
is a fact about the company.

## Environment

| Variable | Required | Notes |
|---|---|---|
| `JOBTRACKER_INSTANCE_ID` | **In containers, yes** | See below. Getting this wrong is silent |
| `TZ` | **In containers, yes** | See below. Getting this wrong is silent |
| `JOBTRACKER_DB` | No | Defaults to `/data/state.db` in the image |
| `JOBTRACKER_CRITERIA` | No | Defaults to the copy baked into the image |
| `JOBTRACKER_COMPANIES` | No | Defaults to the copy baked into the image |
| `JOBTRACKER_TELEMETRY` | No | `off` (default), `console`, or `otlp` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | With `otlp` | Where the collector is |
| `JOBTRACKER_LLM_URL` | For `work`/`rank` | `http://HOST:PORT` of the inference router. The model tag is discovered from `/v1/models`. Absent → `work` is a no-op; `rank` still re-scores from stored judgments |
| `SIR_BASE_URL` / `SIR_ENDPOINTS` | Alternative | The SDK's own variables, honoured so a host already pointing services at the router need not repeat itself |
| `JOBTRACKER_PROFILE` | No | Defaults to the copy baked into the image. Mount it to tune the ranking without rebuilding |
| `JOBTRACKER_ANSWERS` | For `prefill`/`apply-to` | Your answer bank. Not in the image — it is personal data. Absent → `jobtracker prefill` refuses and says so, `prepare` says it cannot prefill, and nothing else notices |
| `JOBTRACKER_BROWSER_PROFILE` | For `apply-to` | Persistent browser profile. Put it on a volume or every run starts logged out |
| `DISPLAY` | For `apply-to`/`serve`'s button | Not ours, but load-bearing: Playwright draws a real window on the host running the command. A headless host needs an X display (`Xvfb :100`) or the launch fails. Nobody looks at it — `/apply` is where you type — but it still has to exist |
| `JOBTRACKER_RESUMES` | No | Where per-posting resumes are stored. Defaults to `./data/resumes`, so a mounted `/data` already covers it |
| `JOBTRACKER_MAILDIR` | For `mail`/`inbox` | Your job-search mailbox. **Mount it read-only** (`:ro`) — the code never writes to it and the mount should say so too. Absent → `mail` exits 1 and the `inbox` task reports itself unavailable, which is a different state from "nothing to do" |

Every run logs the resolved `companies=`/`criteria=`/`db=` paths as its first line. When
something behaves as though your config changed nothing, read that line first.

## Volumes

- `/data` — **required.** Holds `state.db`. The image is disposable; this is not.
- `/app/criteria.yaml` — optional, but mount it once you start tuning. See the schema-skew
  trap below.
- `/app/profile.yaml` — optional. Mount it to change what the ranking optimizes for
  without rebuilding the image.

## The nightly sequence

`check` is the one command that has to run; the rest turn on the parts of the pipeline
that drain, order, and display what `check` finds. A complete nightly run is four
one-shot commands against the same `$JOBTRACKER_DB`, in order:

```sh
jobtracker check              # fetch → health → store → match → cache descriptions → report
jobtracker work               # the next model task; repeat until it reports nothing to do
jobtracker prepare            # rescore, then prefill tomorrow's picks. No model needed.
jobtracker dashboard          # render state.db → a static HTML file
```

`jobtracker prefill` is not in that list, because `prepare` runs it over exactly the
postings tomorrow will surface — which is the set that matters, and a much smaller one
than "every open match". Run it standalone when you want plans for more than the picks.

`rank` is deliberately not in that list any more. `work` refreshes scores after every run
and `prepare` refreshes them before choosing the picks, so the nightly path no longer
needs it. It stays as the interactive command for "I changed a weight in profile.yaml,
re-sort the queue now" — which costs no model calls at all.

- **`check` is the only step that touches an ATS.** It caches a description for every
  match/uncertain posting, which is what lets everything after it read `state.db` and talk
  to nothing but the local model. `--max-descriptions` (default 400) bounds that work; the
  remainder is picked up the next night, so a first run after a criteria change drains
  over a few days rather than in one long job.
- **`work` is the automated drain, and it picks its own task.** `check` leaves every
  no-level-token title `uncertain`; the `level` task reads the description and settles
  what it can (→ match/reject), leaving only the genuinely ambiguous. When that queue is
  empty it moves to `judge` — the pipeline's dependency order, so one command keeps
  every stage drained. Run it several times per night to drain more than one stage.
  (A third task, `prefill`, was in that chain until 2026-08-25. It needs no model, so it
  left the queue and `prepare` calls it directly — which is why `prepare` now does useful
  work on a host with no GPU, where it used to build nothing.) Without `JOBTRACKER_LLM_URL` it is a safe no-op that prints the queue, so it
  is always fine to include in the sequence. Every failure path leaves a posting where it
  was, so a down router never corrupts a verdict — and each unit commits on its own, so a
  container killed mid-run keeps everything that already landed.
- **`prepare` is the last thing to run and the one whose exit code matters.** It
  rescores, takes the postings `today` will surface in the morning, and makes sure each
  has a prefill plan. Exit 2 means at least one pick has no plan at all — the state that
  leaves you opening a blank form — and the output names why for each one. **Unanswered
  questions never cause exit 2**: a form with gaps is the normal state, especially in the
  first weeks, and failing on it would leave the job permanently red for a condition only
  you can clear. Same reasoning as dbt Labs' legitimately empty board.
- **`apply-to` is interactive and does not belong in an unattended sequence.** It opens a
  browser window and waits for you. Nothing about it is safe to schedule, and it never
  submits anything on its own.
- **`rank` orders what survived, and degrades further than `work` does.** With no model
  it still re-scores from the judgments it already holds, so yesterday's ordering stands
  rather than the queue emptying. It exits 0 either way. `--limit` bounds the model calls
  (~8s each); scoring always covers everything and needs no model at all.
- **Order matters only in that each step reads what the previous one wrote.** Each is
  independently idempotent and re-runnable.
- **Sequencing is the caller's job, not the app's.** Four `Exec=` lines, four cron
  entries, or four steps in one wrapper — the container does not care. Keep each a
  separate invocation so one failing does not abort the others (a model being down should
  not stop the dashboard from rendering).

## Scheduling, and sharing a GPU with other services

### How often: the queue is fed by `check`, so pace everything off that

Model work has no new input except what `check` brings in. Between two checks the queue
can only shrink. So a `work` loop on a short timer is not "keeping up" — it is asking a
question whose answer cannot have changed, and it does it while holding a model resident
that something else may want.

**Once a night is the right default.** Measured shape on this corpus:

| | first ~4 nights | steady state |
|---|---|---|
| new descriptions cached | 400 (the `--max-descriptions` cap) | ~30–40 |
| `level` units | a few hundred | ~30–40 |
| `judge` units | tens | single digits |
| model time | ~30–60 min | ~5–10 min |

The backlog is the only reason the first week is long, and it is bounded by the
description budget rather than by the model. Once it drains, the whole nightly model
window is under ten minutes and there is nothing to gain from running it more often.

Run `check` twice a day only if you want fresher postings; the boards themselves do not
move fast enough to justify more, and `boards-api.greenhouse.io` throttles a run that
pushes too hard (see the parallelism note in CLAUDE.md).

### Contention: let the router arbitrate, do not schedule around it

`sir` exists to decide which model is resident. Trying to also solve that with a
timetable means two schedulers disagreeing, and the timetable is the one with no idea
what is actually queued. So:

- **Give job-tracker the lowest `priority` in the router config.** It is a batch job with
  nobody waiting on it. Anything interactive should preempt it, and the router's
  `max_wait_seconds` ceiling guarantees this still cannot starve.
- **Batch into one contiguous window rather than dribbling.** The thing that actually
  costs you is *swap thrash*: alternating requests between two models that cannot
  co-reside makes the GPU spend its time loading rather than generating. `sir`'s
  `min_residency_seconds` hysteresis is what absorbs that, and it works by letting a
  resident model keep the GPU while it still has work. A run that submits 200 units back
  to back cooperates with that; a cron entry that submits one unit every five minutes
  defeats it.
- **Raise `--concurrency` before raising frequency.** More units in flight fills the
  window the model is already resident for. The default of 4 matches `fetch.MAX_WORKERS`;
  8–16 is reasonable against a local GPU with nothing else running.
- **Do not add retry timers.** A unit that fails is retried on the next night's run and
  set aside after three consecutive failures. That is already the backoff.

### Two units, and why they are separate

```systemd
# jobtracker-nightly.service — the long one. Everything that needs the GPU.
ExecStart=/usr/local/bin/jobtracker check
ExecStart=/usr/local/bin/jobtracker work --budget 400 --concurrency 8
ExecStart=/usr/local/bin/jobtracker work --budget 400 --concurrency 8
SuccessExitStatus=0 2
```

```systemd
# jobtracker-tomorrow.service — the short one. Runs after, and its result is the one
# you actually care about in the morning.
ExecStart=/usr/local/bin/jobtracker prepare
ExecStart=/usr/local/bin/jobtracker dashboard
```

Repeated `work` lines rather than a loop because each drains one stage: `level`, then
`judge`. `systemd` runs `ExecStart=` lines in order and stops on failure, which is the
behaviour you want — and `work` exits 0 even with no router, so a down GPU skips the model
work without failing the unit.

**A host that ran the pre-2026-08-25 shape needs one fewer of these**, not one more:
prefill was the third stage and is inside `prepare` now. A host that also ran the model
pass wants a single `jobtracker forget-learned --write` before the next `prepare`, or it
keeps filling forms from what that pass guessed — see docs/prefill.md.

They are separate units for one reason: **`prepare` is the one whose failure means
something to you.** Bundled together, a red unit could mean anything from "a board 500'd"
to "tomorrow has no prefills". Split, `jobtracker-tomorrow` failing means exactly one
thing — open the dashboard and something will be a blank form. Alert on that one.

Order them with `After=`/`Requires=` on the timer, not by guessing at durations:

```systemd
# jobtracker-tomorrow.service
After=jobtracker-nightly.service
Requires=jobtracker-nightly.service
```

Pick the window from what else uses the GPU, not from the clock. If the box is otherwise
idle overnight, anything from 02:00 works. If another service has a nightly batch of its
own, put job-tracker *after* it rather than beside it — two batch jobs alternating is the
thrash case, two batch jobs in sequence is not.

The **weekly** cadence is separate: `manual` companies are surfaced for hand-checking at
most once per week (rate-limited by `last_checked`), and the aggregator feeds
(`check_method: aggregator`) are worth a look on the same weekly beat. Both ride along
inside `check`; there is no separate command.

---

# Example: systemd user units with Quadlet

Verified on podman 5.8.3. These files go in `~/.config/containers/systemd/` on the
machine that runs the job — **not** in this repo.

```ini
# ~/.config/containers/systemd/jobtracker.container
[Unit]
Description=Job Tracker nightly board sweep

[Container]
Image=ghcr.io/ida314/job-tracker:latest
AutoUpdate=registry
Exec=check
Volume=%h/jobtracker/data:/data:Z
Volume=%h/jobtracker/criteria.yaml:/app/criteria.yaml:Z,ro
Volume=%h/jobtracker/profile.yaml:/app/profile.yaml:Z,ro
Environment=JOBTRACKER_INSTANCE_ID=%H
Environment=TZ=America/New_York
LogDriver=none

[Service]
Type=oneshot
```

`Image=` names the registry, not `localhost/`: the host **pulls** what CI published, and
building on the box would defeat the point of publishing at all. `AutoUpdate=registry`
is what makes `podman auto-update` follow `:latest` — without it the tag is resolved once
and the box runs that layer forever, which looks exactly like a host that is up to date.

`JOBTRACKER_INSTANCE_ID=%H` is not optional here. `telemetry.py` pins
`service.instance.id` to the container's nodename, which under `--rm` is a fresh random
ID every night; `%H` is the host's name and keeps one continuous Prometheus series.

If the package is private, the pull needs a credential — a read-only token, since nothing
on the box ever writes to the registry:

```sh
podman login ghcr.io -u <user> --password-stdin <<<"$GHCR_READ_TOKEN"
```

To pin a build instead of following `:latest` — which is what rollback is — replace the
tag with an immutable one and drop the auto-update line, so nothing quietly moves the box
back off the version you pinned it to:

```ini
Image=ghcr.io/ida314/job-tracker:sha-<full-sha>
```

```ini
# ~/.config/containers/systemd/jobtracker.timer
[Unit]
Description=Run the Job Tracker sweep nightly

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
```

`Type=oneshot` is load-bearing. Quadlet's default is `Type=notify`, which is right for a
service that stays up and wrong for a batch job that exits after 32 seconds.

Quadlet generates `jobtracker.service` from the `.container` file, which is what the
timer activates. Install and test:

```sh
mkdir -p ~/jobtracker/data
cp criteria.yaml ~/jobtracker/criteria.yaml
podman pull ghcr.io/ida314/job-tracker:latest   # first run only; the unit pulls after
systemctl --user daemon-reload
systemctl --user start jobtracker.service     # run once, now
systemctl --user show jobtracker.service -p Result -p ExecMainStatus
journalctl --user -u jobtracker.service -n 40
```

A good result is `Result=success`, `ExecMainStatus=0`, and a `run complete:` line. Only
then enable the schedule:

```sh
systemctl --user enable --now jobtracker.timer
systemctl --user list-timers jobtracker.timer
```

To inspect the generated unit without installing anything:
`/usr/libexec/podman/quadlet -dryrun -user`.

---

# Five ways this breaks silently

Each of these was hit while building the example above. None produces an error at the
time; all of them corrupt something you only notice much later.

### 1. `JOBTRACKER_INSTANCE_ID` unset → a new Prometheus series every night

`telemetry.py` pins `service.instance.id` to `os.uname().nodename` so a daily job keeps
one continuous series. Inside a container that nodename is the **container ID**, which is
a fresh random value on every `--rm` run. The pinning then does the exact opposite of its
intent, and every night mints a new series. Under a Kubernetes CronJob this is worse, not
better: the pod name is regenerated per run, so the downward API is not a fix either. It
must be a stable literal — `%H` in systemd, a hardcoded string elsewhere.

Verify after two runs — this must return `1`:

```promql
count(count by (instance) (jobtracker_boards_total))
```

The label is `instance`, **not** `service_instance_id`: the collector's
`target_info.enabled` promotes `service.name` / `service.instance.id` onto every series
as `job` / `instance`. Confirm the value is your host name and not a hex container ID:

```sh
curl -sG --data-urlencode 'query=jobtracker_boards_total' \
  localhost:9090/api/v1/query | grep -o '"instance":"[^"]*"' | sort -u
```

### 2. `TZ` unset → the container is on a different date than you

The image is UTC. `date.today()` drives `first_seen`, the report's `since` window, and
`manual_due()`'s day arithmetic. Observed directly: host `2026-07-23 21:46 EDT`, container
`2026-07-24 01:46` — a different **day**. A 02:00 local run happens to be safe (06:00 UTC,
same date), but any evening run stamps tomorrow onto everything, and the weekly
manual-check rate limiter drifts.

Since 2026-08-16 it also governs `mail_candidates.sent_on`, the normalized day a message
arrived — so a UTC container reading an evening reply files it under tomorrow, and the
`--since` floor is a day off.

### 3. Image and `criteria.yaml` skew → the run refuses to start

The image bakes in both the code and a copy of `criteria.yaml`. Mount a newer
`criteria.yaml` over an older image and the baked `criteria.py` rejects keys it has never
heard of:

```
ValueError: /app/criteria.yaml: unknown criteria keys:
  ['locations_non_us', 'locations_nyc', 'locations_us']
```

This is the validating loader doing its job (DESIGN.md §2.1) — it fails loudly and names
the keys, rather than silently ignoring three lists and quietly disabling location
ranking. **Rebuild the image whenever `criteria.py`'s schema changes.** Tuning that only
adds tokens to existing lists needs no rebuild.

### 4. `LogDriver` left at its default → every line journaled twice

systemd already captures the process's stdout and stderr. Letting podman also log to
journald writes each line twice, doubling journal usage for nothing. `LogDriver=none`
leaves systemd as the single path. Since the container is `--rm`, there is no `podman
logs` to lose.

### 5. A root-in-container image → a state.db you cannot write

The image does not declare a `USER`, so it runs as root. Under a runtime that does not
remap uids — Docker, or rootful podman — every file it creates on the `/data` bind
mount comes out owned by root. The nightly run keeps working, because it is also root.

You notice when you run `jobtracker serve` or `jobtracker dashboard` from the repo as
yourself and the read succeeds but the write does not: SQLite needs to write `-wal` and
`-shm` alongside the database just to open it for writing, so this surfaces as a
confusing "unable to open database file" on a file you can plainly read.

Pass the host uid; the process only ever writes `/data`, so it costs nothing:

```sh
docker run --user "$(id -u):$(id -g)" -e HOME=/tmp ...
```

`HOME` is redirected because the image's home directory is not writable by the dropped
uid. Rootless podman is not affected — it already maps the container root to you.

### 6. Lingering disabled → the timer stops when you log out

`systemctl --user` timers only run while the user has a session. On a server that means
the nightly sweep silently stops the moment you disconnect. Check and fix:

```sh
loginctl show-user "$(id -un)" -p Linger    # Linger=no is the problem
sudo loginctl enable-linger "$(id -un)"
```

---

# Metrics are pushed, never scraped

The collector remote-writes into Prometheus; nothing scrapes Job Tracker. This is not an
oversight to correct — a 30-second batch job is essentially never running when a scrape
interval elapses, so a scrape target would return nothing almost every time. The same
reasoning holds under any orchestrator, so do not add scrape annotations when this moves
to a cluster.

See `docs/observability.md` for the collector, the delta-temporality decision, and the
query idioms.

# Moving to another orchestrator

Everything above the "Example" heading is the contract; everything below it is one
implementation of it. Porting means re-expressing the same five inputs — image, `check`,
two volumes, the environment, and a schedule — in whatever the new system uses.

Two things to carry across specifically:

- **Serialize runs.** `state.db` is SQLite in WAL mode. Two overlapping runs contend. A
  systemd `oneshot` gives you this for free; Kubernetes needs
  `concurrencyPolicy: Forbid` stated explicitly.
- **Exit 2 means degraded, not failed.** A scheduler that retries on any non-zero status
  will re-run the whole sweep because one board 404'd. Treat 2 as "alert, do not retry".
