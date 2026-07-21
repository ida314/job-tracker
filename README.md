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

`check` writes to stdout by default; use `--output report.md` to write a file.

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
0 8 * * *  cd /home/dylan/Projects/Job-Tracker && podman run --rm -v "$PWD/data:/data:Z" jobtracker:latest check >> data/cron.log 2>&1
```

To change targets or rules, edit `companies.yaml` / `criteria.yaml` and rebuild (they are
baked into the image), or bind-mount them for iteration:
`-v "$PWD/companies.yaml:/app/companies.yaml:ro"`.

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
