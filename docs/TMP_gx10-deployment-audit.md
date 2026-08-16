# Job Tracker on gx10 — Deployment Audit

**Host:** gx10 · aarch64 · Linux 6.17.0-1021-nvidia
**Audited:** 2026-08-16 18:04 UTC
**As user:** dylan (no passwordless sudo)
**Host timezone:** Etc/UTC

Read-only audit. No unit was started, stopped, enabled, disabled or reloaded; no image was
pulled or built; no file was edited. Commands were limited to `systemctl cat/show/status/list-*`,
`docker images/ps/inspect/network ls`, `journalctl`, `ss`, a single `curl` to
`localhost:8000/v1/models` and two to the dashboard's health endpoints, plus file reads.

---

## Summary

- **Runtime — Docker 29.2.1 only; podman is not installed on this box.** Everything is
  scheduled from **rootless systemd `--user`** units under `dylan` (`Linger=yes`, so they
  run without a login session). No jobtracker units exist in system scope, and there is no
  cron anywhere.

- **Schedule** — one timer, `jobtracker-tomorrow.timer`, at `02:00 America/New_York` with
  `Persistent=true` and a 10-minute randomized delay. It starts `jobtracker-tomorrow.service`,
  whose `Requires=`/`After=` pull in `jobtracker-nightly.service` first. Two units, one clock.

- **Image** — `ghcr.io/ida314/job-tracker:latest`, pulled by the wrapper at the top of every
  run. Currently `sha256:de1c0b40…`, built 2026-08-15 23:39 UTC, stamping
  `jobtracker 0.1.0+ab7f8c7b5390`. **No `AutoUpdate=` anywhere** — there are no quadlets at
  all; the wrapper's `docker pull` is the update mechanism.

- **Model — the nightly model pass is working.** Both wrappers set
  `JOBTRACKER_LLM_URL=http://sir:8000`, and last night's run logged
  `llm ready: http://sir:8000 (model=nvidia/Qwen3.6-27B-NVFP4)` six times and applied
  **57 prefill units**.

- **Premise — the silent model no-op was real but is narrower than described: it cost
  exactly one night, and it is already fixed.** The old wrapper dialled `http://vllm:8000`
  and attached container `vllm-qwen36-27b-nvfp4`, which was retired at 2026-08-15 00:41 UTC
  when the `sir` stack took over :8000. The 2026-08-15 02:04 ET run logged
  `llm unreachable at http://vllm:8000` and exited 0. New wrappers landed the same evening.
  See §8.

- **Live drift — the whole observability stack is down and every run is exporting into a
  black hole.** `otel-collector`, Prometheus, Grafana and Jaeger all exited 2026-08-16
  00:04 UTC, but both wrappers still set `JOBTRACKER_TELEMETRY=otlp`. Last night's journal
  is dominated by `Failed to resolve 'otel-collector'` retries. No metrics or traces have
  been recorded for ~18 hours, and nothing says so.

- **Guard — neither live wrapper has a liveness or guard check of any kind.** The only one
  that ever existed is in the retired `run-rank.sh`, and it dialled a different address than
  its workload did — `localhost:8000` on the host vs `http://vllm:8000` inside the container
  network. Details in §5.

- **Router — confirmed.** `sir` (image `sir:0.1.0`) owns `0.0.0.0:8000`, up 41 hours,
  fronting `sir-vllm-qwen36` on loopback :8001. It is attached to **both** `sir_default` and
  `job-tracker_otel` with alias `sir`, so the wrappers' re-attach step is currently a no-op.

- **Dashboard** — `jobtracker-serve` no longer runs the published image. It is a **separate
  `--user` unit running the repo venv** directly (for Playwright + an X display), so it logs
  a bare `jobtracker 0.1.0` with no `+sha`. Here that is expected, not a failed pull — but it
  is the one place the version-stamp rule reads as a false alarm.

- **Exit state** — last night: `nightly` exit **2** (dbt Labs + Root Insurance 404, tolerated
  by `SuccessExitStatus=0 2`, unit reports success); `tomorrow` exit **2** and genuinely
  **failed** — Sentry has no prefill plan because Ashby does not publish its form. That is
  the alert working as designed.

---

## 1. Runtime

```
$ which podman
(no output — not installed)

$ docker --version
Docker version 29.2.1, build a5c7197

$ whoami; hostname; uname -m
dylan
gx10
aarch64
```

**Searched:** `which podman`, `podman --version` — not found. Podman is not present on this
host in any form.

**Scopes.** All Job Tracker units are **rootless user scope**. System scope contains
`docker.service`, `containerd.service`, `docker.socket` and `nv-docker-gpus.service`, all
enabled, but **no unit whose name, `ExecStart` or environment mentions jobtracker**.

```
$ loginctl show-user dylan -p Linger
Linger=yes
```

The user manager runs without a login session, which is what lets a 02:00 timer fire on an
unattended box.

### Cron

```
$ crontab -l
no crontab for dylan

$ ls /etc/cron.d/
anacron  e2scrub_all  .placeholder  sysstat

$ ls /etc/cron.daily/
0anacron  apport  apt-compat  dpkg  logrotate  man-db  .placeholder  quota  sysstat
```

**Searched:** user crontab, `/etc/cron.d`, `/etc/cron.daily`. **No jobtracker entries.**
`/var/spool/cron/crontabs` (other users' crontabs, incl. root) was **permission denied** —
see §12.

---

## 2. Units and timers

Eight units in user scope. The two that run the pipeline are `jobtracker-nightly` and
`jobtracker-tomorrow`; the other five plus a target are the VNC viewer stack that gives
Playwright a screen.

| Unit | State | Enabled | Last run | Exit |
|---|---|---|---|---|
| jobtracker-tomorrow.timer | active (waiting) | enabled | 2026-08-16 06:00:38 UTC | — |
| jobtracker-nightly.service | inactive (dead) | disabled | 2026-08-16 06:15:11 UTC | 2 → success |
| jobtracker-tomorrow.service | inactive (dead) | disabled | 2026-08-16 06:15:12 UTC | 2 → **failed** |
| jobtracker-serve.service | active (running) | enabled | 2026-08-16 16:56 UTC | — |
| jobtracker-display.service | active (running) | enabled | — | — |
| jobtracker-x11vnc.service | active (running) | enabled | — | — |
| jobtracker-novnc.service | active (running) | enabled | — | — |
| jobtracker-viewer.target | active | enabled | — | — |

> **Reading the "disabled" column.** Both pipeline services are `disabled` and that is
> correct, not a misconfiguration: the **timer** is what starts them. Their
> `[Install] WantedBy=default.target` means enabling either would additionally fire it at
> every boot.

```
$ systemctl --user list-timers --all
NEXT                        LEFT  LAST                        PASSED   UNIT                       ACTIVATES
Mon 2026-08-17 06:04:05 UTC 11h   Sun 2026-08-16 06:00:38 UTC 12h ago  jobtracker-tomorrow.timer  jobtracker-tomorrow.service
```

(Three unrelated user timers omitted: firmware-notifier, launchpadlib-cache-clean, p80-backup.)

### systemctl --user cat jobtracker-tomorrow.timer

```ini
# /home/dylan/.config/systemd/user/jobtracker-tomorrow.timer
[Unit]
Description=Run the Job Tracker pipeline once daily
Documentation=file:///home/dylan/Projects/job-tracker/docs/deployment.md

[Timer]
# One timer drives the whole chain. It starts jobtracker-tomorrow.service, whose
# Requires= pulls in jobtracker-nightly.service and whose After= holds it until the
# sweep has finished. Two units, one schedule — there is no second clock to keep in sync
# and no duration to guess at.
Unit=jobtracker-tomorrow.service

# 02:00 New York. The timezone suffix is explicit because this host runs on Etc/UTC —
# without it systemd would read 02:00 as UTC, i.e. 22:00 the previous day locally, and
# the run would stamp a different date than the one the operator is living in.
# (systemd >= 252 supports the timezone suffix; this box is 255.)
OnCalendar=*-*-* 02:00:00 America/New_York

# Run on the next boot if the machine was off at 02:00 — a missed night is a silent gap
# in first_seen history that never backfills.
Persistent=true

# Spread load off the exact minute. The sweep is ~35s of work plus deliberate pacing
# against boards-api.greenhouse.io, which throttles on burst.
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
```

### systemctl --user cat jobtracker-nightly.service

```ini
# /home/dylan/.config/systemd/user/jobtracker-nightly.service
[Unit]
Description=Job Tracker nightly board sweep (check -> work x6)
Documentation=file:///home/dylan/Projects/job-tracker/docs/deployment.md
# The sweep needs the network up. The OTel collector and the `sir` router are plain
# containers started by docker with restart=unless-stopped, so there is nothing further
# to order against here — the wrapper re-asserts the router's network attachment itself.
After=network-online.target docker.service
Wants=network-online.target

[Service]
# Load-bearing. The default (notify) is right for a daemon and wrong for a batch job.
Type=oneshot
ExecStart=/home/dylan/jobtracker/run-nightly.sh

# Exit 2 means "ran, but a board is degraded" — the run completed and stored data. It is
# listed as success here, which is the opposite of what this unit used to do, and the
# change is the point of splitting the pipeline in two.
#
# dbt Labs 404s every single night (a dead Greenhouse slug behind a JS-shell careers
# page, documented in CLAUDE.md). A unit that is red every night is indistinguishable
# from having no signal at all, and the night a board really breaks would look like every
# other night. The alerting unit is jobtracker-tomorrow.service; this one is the plumbing.
# Board health is still visible — in the report, the dashboard's Boards tab, and Grafana.
SuccessExitStatus=0 2

# Do NOT add Restart=. Re-running the whole sweep because one board 404'd is exactly the
# wrong response, and the model work is resumable by construction: each unit commits on
# its own and the queue is derived from state.db rather than carried between runs.
#
# check is ~35s. The three `work` lines are the long pole — a first run against a fresh
# backlog can take an hour of GPU, and it shares the card with whatever else `sir` has
# resident. Generous rather than tuned: a timeout here kills a run mid-drain for no gain.
TimeoutStartSec=6h

# Nothing else is needed to serialize runs: systemd will not start a second instance of
# this unit while one is active, which is what keeps two writers off the SQLite WAL
# database. The wrapper's flock covers hand-runs.

[Install]
WantedBy=default.target
```

> **Stale comment inside the live unit.** The `Description=` says **work x6** but the
> `TimeoutStartSec` comment three lines down still says **"The three `work` lines are the
> long pole"**. The wrapper runs six. Cosmetic, but this file is the thing people read to
> understand the schedule.

### systemctl --user cat jobtracker-tomorrow.service

```ini
# /home/dylan/.config/systemd/user/jobtracker-tomorrow.service
[Unit]
Description=Job Tracker: make tomorrow's picks ready to apply to (prepare -> dashboard)
Documentation=file:///home/dylan/Projects/job-tracker/docs/prefill.md
After=network-online.target docker.service
Wants=network-online.target

# The chain. Requires= pulls the sweep in when this unit is started (the timer starts
# THIS one, not the sweep), and After= holds this back until the sweep's oneshot has
# actually exited. If the sweep exits 1 — could not run at all — this does not run
# either, which is right: preparing picks from a database nothing wrote today is a lie.
# Exit 2 from the sweep counts as success (SuccessExitStatus there), so a degraded board
# still gets you a prepared morning.
Requires=jobtracker-nightly.service
After=jobtracker-nightly.service

[Service]
Type=oneshot
ExecStart=/home/dylan/jobtracker/run-tomorrow.sh

# Deliberately NO SuccessExitStatus=0 2 here, unlike the sweep. This is the unit whose
# failure means something specific and actionable: exit 2 is `prepare` reporting that at
# least one of tomorrow's picks has no prefill plan at all — you would open a blank form.
# That is the one thing worth alerting on, and it is only legible because the noisy half
# of the pipeline is a different unit.
#
# Unanswered questions never cause exit 2; `prepare` enforces that itself. A form with
# gaps is the normal state and must not make this permanently red.

# prepare rescores and prefills at most 3 picks — minutes, not hours, even cold.
TimeoutStartSec=1h

[Install]
WantedBy=default.target
```

### systemctl --user cat jobtracker-serve.service

```ini
# /home/dylan/.config/systemd/user/jobtracker-serve.service
# Generated by ~/jobtracker/viewer-install.sh --with-serve from
# units/jobtracker-serve.service.in. Edit the template, not this copy.
[Unit]
Description=Job Tracker dashboard (venv, with a browser it can actually open)
Documentation=file:///home/dylan/Projects/job-tracker/docs/prefill.md
# Replaces the `jobtracker-serve` *container*. That container runs the published image,
# which ships no Playwright and has no display, so "Open prefilled" there can only ever
# report that there is no browser to drive. This runs the repo's venv, which has both.
#
# The nightly pipeline still runs from the published image — this changes the dashboard
# process only, and `viewer-install.sh --uninstall` puts the container back.
BindsTo=jobtracker-display.service
After=jobtracker-display.service network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/dylan/Projects/job-tracker

# The display the application window opens on. Without it, Playwright's headful launch has
# nowhere to draw and fails on the worker thread — which the page now reports rather than
# hanging on "Opening…", but reporting it is not the same as it working.
Environment=DISPLAY=:100

# /data in the container, the repo's data/ here. The same two files either way.
Environment=JOBTRACKER_DB=/home/dylan/Projects/job-tracker/data/state.db
Environment=JOBTRACKER_ANSWERS=/home/dylan/Projects/job-tracker/data/answers.yaml

# Where to watch that display. Only a link on the Today card — the app never starts the
# viewer, never checks it, and does not care what is on the other end.
Environment="JOBTRACKER_BROWSER_VIEW_URL=https://job-tracker.tail2e282c.ts.net/vnc/vnc.html?autoconnect=1&resize=remote&path=vnc/websockify"

# This host is UTC and you are Eastern; date.today() drives first_seen, the report's
# since-window and manual_due(), so the zone is named explicitly here as everywhere else.
Environment=TZ=America/New_York

ExecStart=/home/dylan/Projects/job-tracker/.venv/bin/jobtracker serve --port 8765
# serve drains its in-flight request and exits 0 on SIGTERM, which is what makes a plain
# `systemctl --user restart` safe in the middle of a page load.
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

### systemctl --user cat jobtracker-display.service

```ini
# /home/dylan/.config/systemd/user/jobtracker-display.service
# Generated by ~/jobtracker/viewer-install.sh from units/jobtracker-display.service.in.
# Edit the template, not this copy — a reinstall overwrites it.
[Unit]
Description=Job Tracker: the X display prefilled application windows open on
Documentation=file:///home/dylan/Projects/job-tracker/docs/prefill.md
# This host has no screen (XDG_SESSION_TYPE=tty, no wayland socket), and Playwright draws
# a real window on the machine running `serve` — not on the machine reading the dashboard.
# So the window has always existed and never been visible. This gives it a display.
#
# One long-lived :100 rather than `xvfb-run` per launch: a viewer connects to a
# display, so the display has to outlive any one application window, and outlive the
# dashboard process restarting under it.
PartOf=jobtracker-viewer.target

[Service]
Type=simple
# -nolisten tcp: nothing speaks X over the network here. The only way in is x11vnc on
# loopback, which is the one door to keep watch on.
#
# The screen is sized to the browser window, not the other way round: there is no window
# manager on :100, so nothing can maximize or move a window, and every pixel beyond
# the window is dead black space in the viewer. Chromium under Playwright comes up at
# ~1296x864 (a 1280x720 viewport plus its own chrome), so this is that with a margin.
ExecStart=/usr/bin/Xvfb :100 -screen 0 1360x920x24 -nolisten tcp
Restart=on-failure
RestartSec=2

[Install]
WantedBy=jobtracker-viewer.target
```

### systemctl --user cat jobtracker-x11vnc.service

```ini
# /home/dylan/.config/systemd/user/jobtracker-x11vnc.service
# Generated by ~/jobtracker/viewer-install.sh from units/jobtracker-x11vnc.service.in.
# Edit the template, not this copy — a reinstall overwrites it.
[Unit]
Description=Job Tracker: VNC server for the application-window display (:100)
Documentation=file:///home/dylan/Projects/job-tracker/docs/prefill.md
# BindsTo, not just After: if the display dies there is nothing to serve, and a VNC server
# pointed at a dead display still accepts connections and shows nothing — the same
# absence-read-as-success shape the pipeline is built to avoid.
BindsTo=jobtracker-display.service
After=jobtracker-display.service
PartOf=jobtracker-viewer.target

[Service]
Type=simple
# -localhost is the access control. Nothing reaches this port except websockify on the
# same host, and the only public door is Tailscale, which is HTTPS and tailnet-
# authenticated. That is why -nopw is acceptable here and would not be if this bound
# 0.0.0.0: a password on a loopback socket protects against nothing that can already
# reach loopback.
#
# -forever: keep serving after a viewer disconnects. Closing the laptop tab must not end
# the session — the application window on the other side is mid-review.
# -shared: a second tab attaches instead of stealing the session.
ExecStart=/usr/bin/x11vnc -display :100 -localhost -rfbport 5900 -forever -shared -nopw -noxdamage -quiet
Restart=on-failure
RestartSec=2

[Install]
WantedBy=jobtracker-viewer.target
```

### systemctl --user cat jobtracker-novnc.service

```ini
# /home/dylan/.config/systemd/user/jobtracker-novnc.service
# Generated by ~/jobtracker/viewer-install.sh from units/jobtracker-novnc.service.in.
# Edit the template, not this copy — a reinstall overwrites it.
[Unit]
Description=Job Tracker: noVNC (the application window, in a browser tab)
Documentation=file:///home/dylan/Projects/job-tracker/docs/prefill.md
BindsTo=jobtracker-x11vnc.service
After=jobtracker-x11vnc.service
PartOf=jobtracker-viewer.target

[Service]
Type=simple
# websockify serves noVNC's static files and bridges the browser's websocket to the VNC
# socket. Bound to loopback for the same reason x11vnc is: Tailscale is the only way in,
# so the tailnet's identity is the authentication and nothing is exposed to the LAN.
#
# This tailnet publishes apps as Tailscale *Services* (svc:job-tracker, svc:grafana,
# svc:jaeger), not node ports, so noVNC gets one of:
#
#   tailscale serve --service=svc:job-tracker --set-path=/vnc http://127.0.0.1:6080
#   tailscale serve --service=svc:job-tracker-vnc --https=443 http://127.0.0.1:6080
#
# svc:job-tracker itself is untouched either way — this is a second program on the box,
# not a second copy of the dashboard.
ExecStart=/usr/bin/websockify --web=/usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900
Restart=on-failure
RestartSec=2

[Install]
WantedBy=jobtracker-viewer.target
```

### systemctl --user cat jobtracker-viewer.target

```ini
# /home/dylan/.config/systemd/user/jobtracker-viewer.target
# Generated by ~/jobtracker/viewer-install.sh from units/jobtracker-viewer.target.in.
# Edit the template, not this copy — a reinstall overwrites it.
#
# The group that gives the prefilled application window a screen and a way to watch it.
# Deliberately lists no members: each unit declares `WantedBy=jobtracker-viewer.target`,
# so enabling it is what puts it in the group. Same shape as p80.target, and for the same
# reason — a machine that exposes the display some other way can skip a member without a
# `Wants=` line naming a unit that is not installed.
#
# Separate from the dashboard on purpose. An X display and a VNC bridge are host
# infrastructure, not Job Tracker: the app names a URL and nothing more. Restarting the
# dashboard must not drop the session you are reviewing a form in, and a dead websockify
# must not touch the nightly pipeline.
[Unit]
Description=Job Tracker application-window viewer (display, VNC, noVNC)
Documentation=file:///home/dylan/Projects/job-tracker/docs/prefill.md

[Install]
WantedBy=default.target
```

---

## 3. Image specification

> **There are no quadlets and no compose in the run path.** **Searched:**
> `/etc/containers/systemd`, `~/.config/containers/systemd`, `/usr/share/containers/systemd`
> — **none of the three directories exists.** The container is specified entirely by
> `docker run` argument arrays inside the two wrapper scripts. The repo's `compose.yaml`
> covers only the observability stack (§7), not the app.

Both wrappers build the same `common=(…)` array. Verbatim, from `run-nightly.sh`:

```bash
IMAGE=ghcr.io/ida314/job-tracker:latest
NETWORK=job-tracker_otel

common=(
  --rm
  --user "$(id -u):$(id -g)"
  -e HOME=/tmp
  --network "$NETWORK"
  -v "$REPO/data:/data"
  -v "$REPO/criteria.yaml:/app/criteria.yaml:ro"
  -v "$REPO/profile.yaml:/app/profile.yaml:ro"
  -e JOBTRACKER_INSTANCE_ID=gx10
  -e JOBTRACKER_ANSWERS=/data/answers.yaml
  -e TZ=America/New_York
  -e JOBTRACKER_TELEMETRY=otlp
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
)

llm=(
  -e JOBTRACKER_LLM_URL=http://sir:8000
)
```

`run-nightly.sh:39–110`. `run-tomorrow.sh:47–66` is byte-identical apart from line numbers.

| Item | Value | Note |
|---|---|---|
| Image reference | `ghcr.io/ida314/job-tracker:latest` | Registry namespace `ghcr.io/ida314`. Pulled at the top of `run-nightly.sh` only; `run-tomorrow.sh` deliberately does not pull. |
| `AutoUpdate=` | **absent** | Not applicable — no quadlets exist. Updates come from the wrapper's `docker pull`, which is non-fatal on failure. |
| Network | `job-tracker_otel` | Bridge, `172.20.0.0/16`. Currently the **only** member is `sir` (172.20.0.6). |
| Volumes | `…/data → /data`; `…/criteria.yaml → /app/criteria.yaml:ro`; `…/profile.yaml → /app/profile.yaml:ro` | One directory mount plus two read-only file mounts. |
| Restart policy | `--rm` | One-shot per subcommand. Eight containers per night (1 check + 6 work + 1 prepare/dashboard pair). |
| User | `--user $(id -u):$(id -g)` | 1000:1000, with `HOME=/tmp` so the dropped uid has somewhere writable. |

### Environment variables passed to the container

| Variable | Value | Passed to |
|---|---|---|
| `JOBTRACKER_INSTANCE_ID` | `gx10` | all |
| `JOBTRACKER_ANSWERS` | `/data/answers.yaml` | all |
| `TZ` | `America/New_York` | all |
| `JOBTRACKER_TELEMETRY` | `otlp` | all |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4318` | all — **dead endpoint** |
| `HOME` | `/tmp` | all |
| `JOBTRACKER_LLM_URL` | `http://sir:8000` | `work`, `prepare` only — **not** `check` or `dashboard` |

---

## 4. Images and containers present

### Images matching jobtracker

| Repository:tag | Image ID | Digest | Created | Size |
|---|---|---|---|---|
| ghcr.io/ida314/job-tracker:latest | ca5277f0f629 | sha256:de1c0b40… | 2026-08-15 23:39 UTC | 177MB |
| ghcr.io/ida314/job-tracker:sha-5cd4ad76b8c2… | aa1b714c8b22 | sha256:4e74f75f… | 2026-08-15 15:16 UTC | 177MB |
| jobtracker:ci-rename | f3c4596a1e2b | `<none>` | 2026-08-15 15:14 UTC | 179MB |
| jobtracker:ci-sdk | a27729f24049 | `<none>` | 2026-08-15 14:58 UTC | 179MB |
| jobtracker:ci | 3564041ac03e | `<none>` | 2026-08-15 14:58 UTC | 174MB |
| jobtracker:latest | f0be0f7b7ef0 | `<none>` | 2026-08-02 21:59 UTC | 171MB |

> **Locally-built images exist alongside the registry one.** **Four local builds are
> present** — `jobtracker:latest` (2026-08-02) and three `jobtracker:ci-*` tags — none of
> which carries a registry digest. **The live units reference none of them.** Both wrappers
> name `ghcr.io/ida314/job-tracker:latest` only.
>
> `jobtracker:latest` is two weeks stale and is **still named by the retired `run-rank.sh`**
> (`IMAGE=jobtracker:latest`). If that wrapper were ever re-wired it would silently run
> 2026-08-02 code.

### Containers, running and not

| Name | Image | Status | Ports |
|---|---|---|---|
| sir | sir:0.1.0 | **Up 41 hours** | 0.0.0.0:8000→8000 |
| sir-vllm-qwen36 | eugr/spark-vllm:latest | **Up 41 hours (healthy)** | 127.0.0.1:8001→8000 |
| jobtracker-serve | ghcr.io/ida314/job-tracker:latest | Exited (0) ~1h ago | — |
| job-tracker-otel-collector-1 | otel/…-collector-contrib:0.116.1 | **Exited (0) 18h ago** | — |
| job-tracker-prometheus-1 | prom/prometheus:v3.1.0 | **Exited (0) 18h ago** | — |
| job-tracker-grafana-1 | grafana/grafana:11.4.0 | **Exited (0) 18h ago** | — |
| job-tracker-jaeger-1 | jaegertracing/all-in-one:1.65.0 | **Exited (0) 18h ago** | — |
| vllm-qwen36-27b-nvfp4 | eugr/spark-vllm:latest | Exited (0) 41h ago | — |
| vllm | eugr/spark-vllm:latest | Exited (0) 2 weeks ago | — |
| open-webui | ghcr.io/open-webui/open-webui:main | Exited (0) 6 weeks ago | — |

### The stopped `jobtracker-serve` container

Worth calling out because it is the previous dashboard and its config differs from the live
one in three ways that matter.

```
$ docker inspect jobtracker-serve
Image:          ghcr.io/ida314/job-tracker:latest
ImageID:        sha256:c49df74b1dca…       ← NOT the current :latest (ca5277f0f629)
Created:        2026-08-15T23:23:30Z
Finished:       2026-08-16T16:37:04Z
Cmd:            ["serve"]
Networks:       host
RestartPolicy:  {"Name":"no","MaximumRetryCount":0}

Env:
  HOME=/tmp
  JOBTRACKER_DB=/data/state.db
  JOBTRACKER_ANSWERS=/data/answers.yaml
  JOBTRACKER_LLM_URL=http://localhost:8000     ← host network, so this DID reach sir
  TZ=America/New_York
  JOBTRACKER_REVISION=7aad99b2004be4760d3d903529d8b1e87232b68b

Mounts:
  /home/dylan/Projects/job-tracker      -> /app
  /home/dylan/Projects/job-tracker/data -> /data
```

Pinned to image **c49df74b** / revision **7aad99b2**, two commits behind the current
`:latest`. `RestartPolicy: no`, so it stays down. Superseded by the venv unit at 16:37 UTC
today.

---

## 5. Wrapper scripts

**Where they live:** `/home/dylan/jobtracker/`. **None of them is in any git repo** —
`git ls-files` in `/home/dylan/Projects/job-tracker` returns nothing for `run-nightly`,
`run-tomorrow`, `run-rank`, `viewer-install` or `units/`. They exist only on this host.

```
$ ls -la /home/dylan/jobtracker
-rw-rw-r--  1 dylan dylan     0 Aug 16 06:15 .pipeline.lock
drwxrwxr-x  2 dylan dylan  4096 Aug 16 06:00 reports/
drwxrwxr-x  2 dylan dylan  4096 Aug 15 23:23 retired-units/
-rwxrwxr-x  1 dylan dylan  8477 Aug 15 23:43 run-nightly.sh
-rwxrwxr-x  1 dylan dylan  3087 Aug 15 23:22 run-tomorrow.sh
drwxrwxr-x  2 dylan dylan  4096 Aug 16 16:33 units/
-rwxrwxr-x  1 dylan dylan 13927 Aug 16 16:56 viewer-install.sh
```

Two live wrappers. `viewer-install.sh` is an **installer**, not runtime — it generates the
four viewer units from `units/*.in` templates and is referenced by no `ExecStart`.
`retired-units/` holds the previous generation (§10).

### Address analysis

| Wrapper | Subcommands | Model address | Namespace | Guard check |
|---|---|---|---|---|
| `run-nightly.sh` | `check --max-descriptions 400`; `work --budget 400` ×6 | `http://sir:8000` | `job-tracker_otel` | **none** |
| `run-tomorrow.sh` | `prepare`; `dashboard --output /data/dashboard.html` | `http://sir:8000` | `job-tracker_otel` | **none** |
| `retired/run-rank.sh` | `rank --limit 40`; `dashboard` | `http://vllm:8000` | `job-tracker_otel` | **mismatched** |

> **Answering the guard question directly.**
>
> **Neither live wrapper has a liveness or guard check.** No `curl`, no probe, no
> reachability test appears in `run-nightly.sh` or `run-tomorrow.sh`. What both do instead
> is re-assert the router's **network attachment** — a `docker network inspect | grep -qw sir`
> that connects `sir` to `job-tracker_otel` if absent. That is a topology check, not a
> liveness check: it verifies the container is *attached*, never that it *answers*.
>
> The design compensates: every model path treats an unreachable router as "no new
> information" and exits 0, and the app logs `llm ready:` or `llm unreachable at …` per
> invocation. So liveness is observable after the fact, in the journal, rather than gated
> before the fact.
>
> **The only guard that ever existed is in the retired `run-rank.sh:57`, and it dialled a
> different address than its own workload.** The guard hit `http://localhost:8000` — the
> **host** namespace, where vLLM was published. The workload ran with
> `JOBTRACKER_LLM_URL=http://vllm:8000` — the **container** namespace, resolved by a network
> alias. Two different names for what was then one server, so the guard could pass while the
> workload's alias failed to resolve. That is precisely the failure mode that landed on
> 2026-08-15 (§8).

### run-nightly.sh — in full

```bash
#!/usr/bin/env bash
# Job Tracker nightly run — the long half. Lives on the machine, not in the repo: the
# repo ships a container and the contract in docs/deployment.md, and deliberately does
# not know what schedules it.
#
# Sequence: check (network) -> work x3 (local model). `check` is the only step that
# touches an ATS; it caches a description for every match/uncertain posting, which is
# what lets everything after it read state.db and speak only to the router.
#
# Three `work` lines rather than a loop because each drains one stage — level, then
# judge, then prefill. That is the pipeline's dependency order: each produces what the
# next consumes. `work` exits 0 even with no router, so a down GPU skips the model work
# without failing the unit.
#
# `rank` is deliberately NOT here any more. `work` rescores after every run and
# `prepare` rescores before choosing tomorrow's picks, so the nightly path no longer
# needs it. It stays the interactive "I changed a weight in profile.yaml" command.
#
# Exit code follows `check`, because check is the run:
#   0  ran, nothing needs attention
#   2  ran, >=1 board is degraded  -> the unit tolerates this (SuccessExitStatus=0 2)
#   1  did not run                 -> real failure
#
# The alerting half is jobtracker-tomorrow.service, which runs after this one. Keeping
# exit 2 non-fatal here is the whole point of the split: dbt Labs 404s every night, and
# a unit that is red every night is indistinguishable from having no signal at all.

set -uo pipefail

REPO=/home/dylan/Projects/job-tracker
IMAGE=ghcr.io/ida314/job-tracker:latest
NETWORK=job-tracker_otel
ROUTER_CONTAINER=sir
LOCK=/home/dylan/jobtracker/.pipeline.lock

export TZ=America/New_York
DATE="$(date +%F)"
REPORT="/home/dylan/jobtracker/reports/${DATE}.md"

# Held for the whole sweep so anything else on this box defers to it — they write the
# same SQLite WAL database. This one BLOCKS (no -n): the sweep is the run that matters
# and must not be skipped because something short happened to be mid-flight. Released
# when the script exits.
exec 9>"$LOCK"
flock 9

# The host pulls; nothing in CI reaches into this machine. Non-fatal on purpose — a
# registry hiccup should run last night's image, not skip the night. This is visible
# rather than silent because every run's first log line names its own build
# (`jobtracker 0.1.0+<sha>`); a bare 0.1.0 or yesterday's sha means the pull failed and
# the image is not the thing to debug.
echo "=== pull  $(date -Is) ==="
docker pull "$IMAGE" || echo "pull failed — running the last good image" >&2

# `sir` lives on its own compose network; the jobtracker containers run on the otel
# network. Attaching it is idempotent and does not survive the router being recreated,
# so re-assert it every run rather than assume it. The failure is silent by design —
# every model pass treats an unreachable router as "no new information" and exits 0 —
# which is exactly why this cannot be left to a one-time setup step. It is how the
# whole model path sat dead for two days after the router landed.
if ! docker network inspect "$NETWORK" --format '{{range .Containers}}{{.Name}} {{end}}' \
     2>/dev/null | grep -qw "$ROUTER_CONTAINER"; then
  echo "=== attaching ${ROUTER_CONTAINER} to ${NETWORK} (alias: sir) ==="
  docker network connect --alias sir "$NETWORK" "$ROUTER_CONTAINER" \
    || echo "could not attach sir — the model steps will no-op this run" >&2
fi

# Shared container arguments.
#
# TZ: the image is UTC and so is this host, but the user is in New York. date.today()
# drives first_seen, the report's `since` window, manual_due(), snooze expiry and the
# ranking's recency term; leaving it UTC stamps tomorrow onto an evening run.
#
# JOBTRACKER_INSTANCE_ID: telemetry.py pins service.instance.id to the nodename, which
# inside a --rm container is a fresh container ID every run. That would mint a new
# Prometheus series nightly — the exact opposite of the pinning's intent.
#
# --user: the image runs as root, so state.db and dashboard.html would come out
# root-owned on a bind mount. Invisible until you run `jobtracker dashboard` from the
# repo as yourself and it cannot write the database it just read. HOME is redirected
# because /nonexistent is not writable for the dropped uid.
#
# JOBTRACKER_ANSWERS lives under /data rather than at the repo root, even though the
# repo root is where the default is. Two reasons, both structural: /data is already a
# *directory* mount in every container here, and safewrite's atomic candidate.replace()
# is a rename — across a single-file bind mount that fails. The resume lands beside it
# as /data/resume.pdf, which is what `resume: ./resume.pdf` resolves to.
common=(
  --rm
  --user "$(id -u):$(id -g)"
  -e HOME=/tmp
  --network "$NETWORK"
  -v "$REPO/data:/data"
  -v "$REPO/criteria.yaml:/app/criteria.yaml:ro"
  -v "$REPO/profile.yaml:/app/profile.yaml:ro"
  -e JOBTRACKER_INSTANCE_ID=gx10
  -e JOBTRACKER_ANSWERS=/data/answers.yaml
  -e TZ=America/New_York
  -e JOBTRACKER_TELEMETRY=otlp
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
)

# The router, not the vLLM backend. `sir` decides which model is resident and forwards
# bodies untouched; dialling vLLM directly would bypass the arbitration it exists to do.
# The model tag is discovered from /v1/models, so there is deliberately no
# JOBTRACKER_LLM_MODEL here — one less string to drift.
llm=(
  -e JOBTRACKER_LLM_URL=http://sir:8000
)

echo "=== check  $(date -Is) ==="
# stdout is the markdown report and nothing else; stderr is progress and goes to the
# journal. Report is written per-day rather than appended so a rerun overwrites cleanly.
#
# --max-descriptions bounds the per-posting Greenhouse fetches. 400 at ~0.6s each is
# ~4 minutes worst case; the remainder is picked up tomorrow. Raise it while a backlog
# is draining, lower it if the nightly window gets tight.
docker run "${common[@]}" "$IMAGE" check --max-descriptions 400 > "$REPORT"
check_rc=$?
echo "check exit=${check_rc}  report=${REPORT}"

if [ "$check_rc" -eq 1 ]; then
  echo "check could not run — skipping the model work" >&2
  exit 1
fi

# SIX lines, not three, and the extra three are not slack — they are the difference
# between prefill running and prefill never running.
#
# `work` drains ONE task per invocation, and the scheduler always picks the earliest
# stage that still has pending units. A level unit the model genuinely cannot settle
# stays pending until it has failed MAX_ATTEMPTS=3 times, and each attempt is a separate
# invocation. So a night with even one unanswerable level unit spends invocations 1-3 on
# level alone and never reaches judge, let alone prefill. Measured here on 2026-08-15:
# 23 level units, 13 settled, 10 unanswerable — invocations 2 and 3 did nothing but burn
# those 10 down to set-aside, and prefill did not run.
#
# level(3) + judge(1) + prefill(1) = 5 is the floor; the sixth is headroom. An invocation
# with nothing to do costs about a second, so over-provisioning here is free and
# under-provisioning is silent.
#
# --budget bounds each so a pathological night cannot run until the timeout. Submitting
# units back to back is deliberate: `sir` has min_residency_seconds=300 and a swap on
# this box costs ~5 minutes, so a contiguous batch cooperates with that hysteresis where
# dribbling defeats it. That is why the hourly rank catch-up was retired rather than
# repointed.
#
# Concurrency is left at the default 4, NOT raised to 8, and that is a correction to the
# obvious reading of docs/deployment.md. The flag counts *units*, but a unit is not one
# model call: level and judge ask once, while prefill asks once per unmatched question —
# ~18 for a real Greenhouse form. So `--concurrency 8` submits ~144 calls against a
# router configured for max_concurrent_requests=64, every unit's slowest call lands
# behind the queue, and no unit finishes early. Measured 2026-08-15: at 8, a 57-unit
# prefill wrote nothing at all for its first seven minutes; at 4, four units landed in
# 46 seconds. It does not fail, it just stops showing progress — which is the worse of
# the two, because it reads exactly like a hang.
for stage in 1 2 3 4 5 6; do
  echo "=== work (${stage}/6)  $(date -Is) ==="
  docker run "${common[@]}" "${llm[@]}" "$IMAGE" work --budget 400
  echo "work exit=$?  (non-fatal by design)"
done

echo "=== done  $(date -Is)  propagating check exit=${check_rc} ==="
exit "$check_rc"
```

> **Two stale comments in the live wrapper.** The header says **"Sequence: check (network)
> -> work x3"** and **"Three `work` lines rather than a loop"**, but the loop below runs
> **six** and its own comment block explains at length why six. The header was not updated
> when the count changed. Same defect as the unit-file comment in §2 — the file explains
> itself twice and disagrees.

### run-tomorrow.sh — in full

```bash
#!/usr/bin/env bash
# Job Tracker nightly run — the short half, and the one whose failure means something.
#
# Runs after run-nightly.sh (ordered by systemd, not by guessing at durations). Two
# steps, no ATS, and at most a handful of model calls:
#
#   prepare     rescore, take the postings `today` will surface, make sure each has a
#               prefill plan. Exit 2 = at least one pick has NO plan at all, i.e. you
#               would open a blank form tomorrow morning.
#   dashboard   render state.db to a static HTML file.
#
# This is a separate unit from the sweep for exactly one reason: bundled together, a red
# unit could mean anything from "a board 500'd" to "tomorrow has no prefills". Split,
# jobtracker-tomorrow going red means precisely one thing. Alert on this one.
#
# Note what does NOT fail it: an unanswered question. A form with gaps is the normal
# state — especially in the first weeks — and failing on it would leave this permanently
# red for a condition only the user can clear, the same trap as flagging dbt Labs'
# legitimately empty board. `prepare` enforces that distinction itself.
#
# Exit code follows `prepare`. `dashboard` runs regardless: a pick with no plan is still
# worth looking at, and the page is how you look at it.

set -uo pipefail

REPO=/home/dylan/Projects/job-tracker
IMAGE=ghcr.io/ida314/job-tracker:latest
NETWORK=job-tracker_otel
ROUTER_CONTAINER=sir
LOCK=/home/dylan/jobtracker/.pipeline.lock

export TZ=America/New_York

# Same lock as the sweep, and blocking for the same reason. systemd already orders this
# after run-nightly.sh; the lock is what protects against a hand-run overlapping.
exec 9>"$LOCK"
flock 9

# No pull here — the sweep that just ran did it, and pulling twice in one chain could
# swap the image mid-sequence. Re-assert the router attachment though: this unit can be
# started on its own.
if ! docker network inspect "$NETWORK" --format '{{range .Containers}}{{.Name}} {{end}}' \
     2>/dev/null | grep -qw "$ROUTER_CONTAINER"; then
  docker network connect --alias sir "$NETWORK" "$ROUTER_CONTAINER" 2>/dev/null \
    || echo "could not attach sir — prefill will report itself unavailable" >&2
fi

# See run-nightly.sh for why each of these is here; they are the same set.
common=(
  --rm
  --user "$(id -u):$(id -g)"
  -e HOME=/tmp
  --network "$NETWORK"
  -v "$REPO/data:/data"
  -v "$REPO/criteria.yaml:/app/criteria.yaml:ro"
  -v "$REPO/profile.yaml:/app/profile.yaml:ro"
  -e JOBTRACKER_INSTANCE_ID=gx10
  -e JOBTRACKER_ANSWERS=/data/answers.yaml
  -e TZ=America/New_York
  -e JOBTRACKER_TELEMETRY=otlp
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
)

llm=(
  -e JOBTRACKER_LLM_URL=http://sir:8000
)

echo "=== prepare  $(date -Is) ==="
docker run "${common[@]}" "${llm[@]}" "$IMAGE" prepare
prepare_rc=$?
echo "prepare exit=${prepare_rc}"

echo "=== dashboard  $(date -Is) ==="
docker run "${common[@]}" "$IMAGE" dashboard --output /data/dashboard.html
echo "dashboard exit=$?"

echo "=== done  $(date -Is)  propagating prepare exit=${prepare_rc} ==="
exit "$prepare_rc"
```

---

## 6. Model address contract

Every hit for the requested variables across `/home/dylan/jobtracker/` and
`~/.config/systemd/user/`:

| Variable / string | Value | File:line | Status |
|---|---|---|---|
| `JOBTRACKER_LLM_URL` | `http://sir:8000` | `run-nightly.sh:108` | **live** |
| `JOBTRACKER_LLM_URL` | `http://sir:8000` | `run-tomorrow.sh:65` | **live** |
| `JOBTRACKER_LLM_MODEL` | (comment only — deliberately unset) | `run-nightly.sh:106` | by design |
| `JOBTRACKER_LLM_URL` | `http://vllm:8000` | `retired-units/run-daily.sh:88` | retired |
| `JOBTRACKER_LLM_PROVIDER` | `vllm` | `retired-units/run-daily.sh:87` | retired |
| `JOBTRACKER_LLM_URL` | `http://vllm:8000` | `retired-units/run-rank.sh:77` | retired |
| `JOBTRACKER_LLM_PROVIDER` | `vllm` | `retired-units/run-rank.sh:76` | retired |
| `localhost:8000` | curl guard | `retired-units/run-rank.sh:57` | **mismatch, retired** |
| `JOBTRACKER_LLM_URL` | `http://vllm:8000` | `retired-units/run-daily.sh.bak-2026-08-02:82` | backup |
| `JOBTRACKER_LLM_PROVIDER` | `vllm` | `retired-units/run-daily.sh.bak-2026-08-02:81` | backup |
| `JOBTRACKER_LLM_URL` | `http://localhost:8000` | container `jobtracker-serve` env (stopped) | stopped |

> **The variable name is correct.** `JOBTRACKER_LLM_URL` is genuinely what the app reads —
> confirmed in source, not docs: `jobtracker/llm/client.py:75` defines
> `ENV_URL = "JOBTRACKER_LLM_URL"`, and `client.py:87` resolves `cli_url` →
> `$JOBTRACKER_LLM_URL` → `$SIR_BASE_URL` in that order.
>
> **Searched and not found anywhere on this host's deployment surface:** `SIR_BASE_URL`,
> `SIR_ENDPOINTS`, `vllm:8000` in a live file, `127.0.0.1:8000`. Neither SDK variable is
> set; the app reaches the router purely through `JOBTRACKER_LLM_URL`.

> **Not passed where you might expect.** `JOBTRACKER_LLM_URL` is in the `llm=(…)` array,
> applied only to `work` and `prepare`. It is **not** passed to `check` or `dashboard`
> (correct — neither uses a model), and **not set at all on the live
> `jobtracker-serve.service`**. The running dashboard therefore has no model address
> configured.

---

## 7. The router

**Confirmed.** `sir` owns :8000, and it answers.

```
$ ss -ltnp | grep 8000
LISTEN 0 4096   0.0.0.0:8000   0.0.0.0:*
LISTEN 0 4096      [::]:8000      [::]:*
LISTEN 0 4096 127.0.0.1:8001   0.0.0.0:*     ← sir-vllm-qwen36, loopback only

$ curl -s -m 5 localhost:8000/v1/models
{"object":"list","data":[{"id":"nvidia/Qwen3.6-27B-NVFP4","object":"model",
 "created":1786903510,"owned_by":"sir","root":"nvidia/Qwen3.6-27B-NVFP4"}]}
```

Note `"owned_by":"sir"` — the response comes from the router, not from vLLM directly. The
:8000 socket has no visible PID because it is a Docker proxy, not a host process.

```
$ docker inspect sir --format '{{json .Config.Cmd}}'
["sir","serve","-c","/etc/sir/config.yaml"]

$ docker inspect sir --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{end}}'
/home/dylan/Projects/stupid--inference-router/deploy/config.yaml -> /etc/sir/config.yaml (ro)
```

Config is bind-mounted from a **second repo on this box**, `stupid--inference-router` (two
hyphens). CLAUDE.md refers to it as `../stupid-inference-router`, one hyphen — the on-disk
name differs.

### Networks

| Network | Driver | Members (running) |
|---|---|---|
| `job-tracker_otel` | bridge | `sir` (172.20.0.6) — **only member** |
| `sir_default` | bridge | `sir` (172.21.0.3), `sir-vllm-qwen36` (172.21.0.2) |
| `bridge` / `host` / `none` | — | Docker defaults |
| `open-webui_default` | bridge | empty |
| `vllm_default` | bridge | empty |

`sir` is already on `job-tracker_otel` with alias `sir`, so the wrappers' re-attach block is
a no-op on current state. It matters after any `docker compose up` that recreates the router.

> **The observability half of that network is gone.** `job-tracker_otel` was created by the
> repo's `compose.yaml` to hold collector + Prometheus + Grafana + Jaeger. **All four are
> stopped** — exited `0` at 2026-08-16 00:04 UTC despite `restart: unless-stopped`, which
> means they were deliberately stopped rather than crashed. The network survives because
> `sir` is attached to it.
>
> Both wrappers still export to `http://otel-collector:4318`. Last night's journal is thick
> with the consequence:
>
> ```
> Transient error HTTPConnectionPool(host='otel-collector', port=4318):
> Max retries exceeded with url: /v1/traces (Caused by NameResolutionError(
> "Failed to resolve 'otel-collector' ([Errno -3] Temporary failure in name
> resolution)")) encountered while exporting span batch, retrying in 4.25s.
> ```
>
> Every `docker run` pays several retry cycles on shutdown. No metric or trace has been
> recorded since 00:04 UTC, and nothing in the pipeline's own exit codes reflects that — the
> telemetry is absent, and absence is what this system is built to notice.

---

## 8. Version actually running

| Stamp | Occurrences | What it is |
|---|---|---|
| `jobtracker 0.1.0+ab7f8c7b5390` | 18 | Current `:latest`. All pipeline containers, 2026-08-15 23:45 onward. |
| `jobtracker 0.1.0+7aad99b2004b` | 4 | Previous image, from the now-stopped `jobtracker-serve` container. |
| `jobtracker 0.1.0` | 3 | **No `+sha`** — the venv `serve` unit, today 16:37 / 16:38 / 16:56 UTC. |

> **The bare 0.1.0 is expected here — but only for one process.** The rule is "a bare
> `0.1.0` means it is not running from a published image", and that is exactly right:
> `jobtracker-serve.service` runs `/home/dylan/Projects/job-tracker/.venv/bin/jobtracker`
> from a working tree at commit `bd3fff8`, which is **one commit ahead of the image**
> (`ab7f8c7`). Deliberate — the image ships no Playwright and has no display. But it means
> the version-stamp check now has a standing exception, and a reader grepping for bare
> `0.1.0` will find one every time and must not treat it as a failed pull. **The nightly
> pipeline still stamps `+ab7f8c7b5390` and is genuinely running the published image.**

```
$ docker pull ghcr.io/ida314/job-tracker:latest      (from last night's run)
latest: Pulling from ida314/job-tracker
Digest: sha256:de1c0b40e38034bcdb326bbb353f6751d04ce8c5b6ed75419d8e7d1d1cbb3217
Status: Image is up to date for ghcr.io/ida314/job-tracker:latest
```

The pull succeeded and was already current. The registry namespace resolves and CD is
delivering.

### The model-pass timeline

This is the part that most needs correcting against the premise. Reconstructed from
container timestamps and the journal:

| When (UTC) | Event | Model outcome |
|---|---|---|
| …–08-14 | Old `jobtracker.service` + `run-daily.sh`, dialling `http://vllm:8000`, attaching container `vllm-qwen36-27b-nvfp4` | **working** — e.g. 08-14: `resolve complete: 151 considered · 1 → match` |
| 08-15 00:41:33 | `vllm-qwen36-27b-nvfp4` stopped (exit 0) — the `sir` cutover | — |
| 08-15 00:49:39 | `sir` started; `sir-vllm-qwen36` at 00:56:17 | — |
| 08-15 06:04:38 | Nightly run, **still on the old wrapper**. Logged `=== attaching vllm-qwen36-27b-nvfp4 … ===` against a container that no longer runs | **silent no-op** — `llm unreachable at http://vllm:8000 — the uncertain queue will be left as-is`, then `resolve exit=0`, `rank exit=0` |
| 08-15 23:23 | New split units + `run-nightly.sh`/`run-tomorrow.sh` installed, pointing at `http://sir:8000` | **recovered** — `level: 23 attempted · 13 applied` |
| 08-16 06:00:38 | First timer-driven run of the new chain | **working** — `prefill: 57 attempted · 57 applied · 0 error` |

> **Correcting the premise.** The silent no-op was real, and the mechanism is exactly as
> described — a wrapper dialling an address that had moved, in a path where an unreachable
> model is a legitimate no-op that exits 0. **But it cost one nightly run, not weeks:**
> 2026-08-15 02:04 ET, the single night between the `sir` cutover and the wrapper rewrite
> that evening. **It is already fixed, and the fix is live.**
>
> The journal on this boot reaches back to 2026-07-24 and shows healthy
> `llm ready: vllm at http://vllm:8000` lines every night through 08-14, so there is no
> longer-running silent window hiding behind retention.

### Last run — the tail

```
=== work (1/6)  2026-08-16T02:01:42-04:00 ===
jobtracker 0.1.0+ab7f8c7b5390
telemetry enabled (mode=otlp, service.name=jobtracker)
llm ready: http://sir:8000 (model=nvidia/Qwen3.6-27B-NVFP4)
  … prefill runs 13 minutes, logging 181 unanswered questions …
work complete: prefill: 57 attempted · 57 applied · 0 no answer · 0 error
  (2 3/14 · 5 3/15 · 1 3/16 · 6 3/17 · 3 3/18 · 1 3/20 · 2 3/21 · 1 3/29 · 2 4/12
   · 2 4/14 · 4 4/15 · 1 4/16 · 2 4/18 · 4 4/21 · 1 5/16 · 1 5/28 · 1 5/29
   · 10 5/35 · 8 6/33)
  181 question(s) still need an answer from you — see the end of /data/answers.yaml
work exit=0  (non-fatal by design)

=== work (2/6)  2026-08-16T02:15:08-04:00 ===
llm ready: http://sir:8000 (model=nvidia/Qwen3.6-27B-NVFP4)
Task queue, in the order the scheduler considers it:
   10  level      nothing to do
   20  judge      nothing to do
   30  prefill    nothing to do
Nothing to do — every task is drained or waiting on something else.
work exit=0  (non-fatal by design)

  … invocations 3, 4, 5, 6 identical, ~0.6s each …

=== done  2026-08-16T02:15:11-04:00  propagating check exit=2 ===
Finished jobtracker-nightly.service.
```

OTel retry lines stripped for legibility; in the raw journal they interleave throughout.
Invocation 1 did all the work; 2–6 correctly reported a drained queue in about three seconds
total, which is the headroom behaving as the wrapper's comment describes.

```
=== prepare  2026-08-16T02:15:11-04:00 ===
Tomorrow: 2/3 ready to apply to
  1. Twilio — Software Engineer, Platform Engineering (L2)
       prefill 6/33 fields · 27 need you
  2. MongoDB — Software Engineer 2
       prefill 5/35 fields · 30 need you
  3. Sentry — Software Engineer, New Grad (2027)
       NOT READY — ashby does not publish its form — run
       `jobtracker apply-to Sentry 5c3196c7-f3d6-4dba-9c41-c886df4b2421` once to learn it
A pick with no plan opens as a blank form. See docs/prefill.md.
prepare exit=2
=== dashboard  2026-08-16T02:15:12-04:00 ===
dashboard written to /data/dashboard.html (839442 bytes)
dashboard exit=0
jobtracker-tomorrow.service: Failed with result 'exit-code'.
```

**The one genuinely actionable alert.** The unit is red because Sentry's Ashby form has never
been learned — clearable by one interactive `apply-to` run.

```
$ cat /home/dylan/jobtracker/reports/2026-08-16.md
# Job tracker — 2026-08-16

_verdicts to date: 423 match · 1476 uncertain · 10131 reject_

## New matches (0)
_None since 2026-08-16._

## Uncertain — needs a human (0)
_None._

## Board failures (2)
- **Root Insurance** (greenhouse/root) — `fetch_failed`: HTTP 404
- **dbt Labs** (greenhouse/dbtlabsinc) — `fetch_failed`: HTTP 404

## Check by hand (0 due of 35 manual)
_None due this week._
```

Today's report is 408 bytes against ~13 KB yesterday — because the 08-15 evening hand-runs
already consumed the day's new postings. Both board failures are the documented permanent
ones. Reports run unbroken daily back to at least 2026-08-05.

---

## 9. State and environment

| Item | Value |
|---|---|
| state.db path | `/home/dylan/Projects/job-tracker/data/state.db` |
| Size | 19,177,472 B (18.3 MiB) |
| Last modified | 2026-08-16 16:56 UTC |
| WAL / SHM | `state.db-wal` 16,512 B (17:10), `state.db-shm` 32,768 B (17:09) |
| Backup | `state.db.bak-20260725-030222` — 5,074,944 B, 2026-07-25 |
| Mounted into containers as | `/data/state.db` |

The `16:56` mtime is the venv `serve` process, not the nightly run — `serve` restarted at
16:56 and opened the DB. Both the container pipeline and the host venv write the same file;
they are serialized by `.pipeline.lock` only for the wrappers, and by SQLite WAL otherwise.

```
$ ls -la /home/dylan/Projects/job-tracker/data/
-rw-rw-r-- 1 dylan dylan    48833 Aug 16 17:10 answers.yaml
-rw-rw-r-- 1 dylan dylan    48374 Aug 16 17:09 answers.yaml.bak
drwxrwxr-x 10 dylan dylan    4096 Aug 16 17:46 browser/
-rw-r--r-- 1 dylan dylan   839442 Aug 16 06:15 dashboard.html
-rw-r--r-- 1 dylan dylan 19177472 Aug 16 16:56 state.db
-rw-r--r-- 1 dylan dylan  5074944 Jul 25 03:02 state.db.bak-20260725-030222
```

Files are `dylan:dylan` throughout — the `--user` flag is doing its job. `answers.yaml` was
edited at 17:10 today (Settings tab), after the nightly run.

### Timezone

| Context | TZ | Set where |
|---|---|---|
| Host | `Etc/UTC` | `timedatectl`; NTP synced |
| Pipeline containers | `America/New_York` | `-e TZ=` in both `common=()` arrays |
| Wrapper shell | `America/New_York` | `export TZ=` at the top of both wrappers |
| Timer | `America/New_York` | `OnCalendar=… America/New_York` |
| `serve` unit | `America/New_York` | `Environment=TZ=` |

**Consistent everywhere.** The 02:00 ET timer fired at 06:00 UTC and the app logged
`2026-08-16T02:00:38.703-04:00` — correct offset, correct date stamp.

### Instance ID

`JOBTRACKER_INSTANCE_ID=gx10` is set on **every pipeline container** in both wrappers. It is
**not** set on `jobtracker-serve.service`, which also sets no `JOBTRACKER_TELEMETRY` — so the
dashboard emits nothing and needs no instance identity. Consistent.

### Service health

```
$ curl -s localhost:8765/healthz    → 200
$ curl -s localhost:8765/readyz
{"status": "ready", "checks": {"db": "ok", "criteria": "ok"}}
```

The dashboard is up on 127.0.0.1:8765 and ready. Loopback only; Tailscale fronts it.

---

## 10. Anything surprising

1. **Podman is entirely absent**, though CLAUDE.md and the memory notes describe
   podman/quadlet as a possibility for this host. The deployment is Docker + rootless
   systemd, and the host half was recorded as "not written yet" — it has since been written,
   in a shape the repo docs do not describe.

2. **A whole retired generation sits next to the live one** in `~/jobtracker/retired-units/`:
   `jobtracker.service`, `jobtracker.timer`, `jobtracker-rank.service`,
   `jobtracker-rank.timer`, `run-daily.sh`, `run-rank.sh`, and
   `run-daily.sh.bak-2026-08-02`. None is installed; `systemctl --user list-unit-files` shows
   no trace. They are the archive, not shadow config — but `run-rank.sh` is the file the
   audit request went looking for, and it is the one carrying the mismatched guard and the
   stale `jobtracker:latest` image reference.

3. **The dashboard changed substrate today.** The container was replaced by a venv unit at
   16:37–16:56 UTC, hours after the nightly run. Anyone reading only the nightly journal
   would not see this.

4. **The whole observability stack was stopped 18 hours ago** and nothing restarted it
   despite `restart: unless-stopped`, which means a deliberate `docker stop` / `compose
   down`. Meanwhile both wrappers still export to it. This is the single largest live gap.

5. **The router's repo directory is `stupid--inference-router` with two hyphens**, while
   CLAUDE.md documents `../stupid-inference-router` with one. The bind mount uses the
   two-hyphen path, so the running config comes from the real directory — but a script
   written from the docs would miss.

6. **Three `jobtracker:ci-*` images from 2026-08-15** are local build artifacts of the CI/SDK
   debugging, never referenced by anything. ~530 MB of dead weight.

7. **Stale comments in two live files** (§2, §5) still say "work x3" where the code runs six.
   Given that this system's failure mode is people reading descriptions instead of behaviour,
   a live file that describes itself wrongly is worth a one-line fix.

8. **No `.env` files anywhere** in `~/jobtracker` or the repo — searched. All configuration is
   inline in the wrappers and units.

---

## 11. Not found

Everything asked about that does not exist here, and where the search ran.

| Looked for | Where | Result |
|---|---|---|
| podman | `which podman`, `podman --version` | Not installed |
| Rootless *and* root podman contexts | `systemctl` / `systemctl --user` | N/A — no podman. Docker root daemon + rootless user units instead |
| System-scope jobtracker units | `systemctl list-units --all`, `list-unit-files` | None |
| Cron entries | `crontab -l`, `/etc/cron.d`, `/etc/cron.daily` | None (root crontab unreadable — see §12) |
| Quadlet files | `/etc/containers/systemd`, `~/.config/containers/systemd`, `/usr/share/containers/systemd` | All three directories absent |
| `AutoUpdate=` | all unit files, both wrappers | Not present anywhere |
| Compose in the app run path | both wrappers, all units | None — repo `compose.yaml` is observability-only and is not invoked by any unit |
| `SIR_BASE_URL` | `~/jobtracker/**`, `~/.config/systemd/user/**` | Zero hits |
| `SIR_ENDPOINTS` | `~/jobtracker/**`, `~/.config/systemd/user/**` | Zero hits |
| `JOBTRACKER_LLM_MODEL` as a set value | `~/jobtracker/**`, `~/.config/systemd/user/**` | Comment only — deliberately unset, tag discovered from `/v1/models` |
| `JOBTRACKER_LLM_PROVIDER` in live config | both live wrappers, all units | Zero hits — exists only in retired files |
| Liveness / guard check in live wrappers | `run-nightly.sh`, `run-tomorrow.sh` | **None.** No curl, no probe. Only a network-attachment assertion |
| A running jobtracker container | `docker ps` | None — every app container is `--rm` and one-shot; `jobtracker-serve` is stopped |
| Second checkout of the repo | `find /home/dylan /opt /srv` | One only: `/home/dylan/Projects/job-tracker`. `~/jobtracker` is the wrapper dir, not a checkout |
| `.env` files | repo and `~/jobtracker`, depth 3 | None |
| state.db outside the repo | `find /home /opt /srv /var` | Only `~/.hermes/state.db`, an unrelated application |
| Wrappers in git | `git ls-files` | None tracked — all host-only, as expected |

---

## 12. Open questions

1. **Root and other users' crontabs were not readable.** `/var/spool/cron/crontabs` returned
   *Permission denied* and there is no passwordless sudo for `dylan`. System-wide cron
   directories were readable and clean, and every observed run is accounted for by the
   systemd timer, so a hidden root cron job is unlikely — but **it was not ruled out**. To
   close it: `sudo ls -la /var/spool/cron/crontabs && sudo grep -rl jobtracker /var/spool/cron/`.

2. **Why the observability stack was stopped is unknown.** Exit code 0 with
   `restart: unless-stopped` means an explicit stop, not a crash — but nothing on the box
   records who or why. Whether it should come back up (and whether `JOBTRACKER_TELEMETRY=otlp`
   should stay set in the meantime) is a decision, not a finding.

3. **Journal history stops at 2026-07-24** — the current boot. Anything before that boot is
   gone, so pre-07-24 run behaviour cannot be checked here. The per-day report files in
   `~/jobtracker/reports/` reach further back and are the better archive.

4. **I did not verify the `sir` router end to end from inside the container network.**
   Confirming that `http://sir:8000` resolves from a container on `job-tracker_otel` would
   require running one, which the investigate-only constraint forbids. The evidence is
   indirect but strong: last night's six invocations each logged a successful
   `GET http://sir:8000/v1/models "HTTP/1.1 200 OK"`.

5. **Whether the Sentry Ashby form has since been learned** is unknown. `data/browser/` was
   modified at 17:46 today, after the run, which suggests an interactive `apply-to` happened
   — but confirming it means querying `state.db`, and tonight's run will answer it anyway.
