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

Every run logs the resolved `companies=`/`criteria=`/`db=` paths as its first line. When
something behaves as though your config changed nothing, read that line first.

## Volumes

- `/data` — **required.** Holds `state.db`. The image is disposable; this is not.
- `/app/criteria.yaml` — optional, but mount it once you start tuning. See the schema-skew
  trap below.

---

# Example: systemd user units with Quadlet

Verified on podman 5.8.3. These files go in `~/.config/containers/systemd/` on the
machine that runs the job — **not** in this repo.

```ini
# ~/.config/containers/systemd/jobtracker.container
[Unit]
Description=Job Tracker nightly board sweep

[Container]
Image=localhost/jobtracker:latest
Exec=check
Volume=%h/jobtracker/data:/data:Z
Volume=%h/jobtracker/criteria.yaml:/app/criteria.yaml:Z,ro
Environment=JOBTRACKER_INSTANCE_ID=%H
Environment=TZ=America/New_York
LogDriver=none

[Service]
Type=oneshot
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

### 5. Lingering disabled → the timer stops when you log out

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
