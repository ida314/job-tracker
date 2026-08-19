"""Command-line entry point.

The nightly sequence is  check -> work -> rank -> dashboard,  and only `check` touches
an ATS. Everything after it reads state.db and, at most, the local inference router.

Subcommands:
  check         the daily pipeline: fetch -> health -> store -> match -> report,
                caching descriptions so every later pass can run offline
  work          run the next available model task, or the one you name. The scheduler
                picks by priority, which is the pipeline's own dependency order
  prepare       make tomorrow's picks ready: rescore, then prefill the top N
  apply-to      open an application in a browser with your answers already filled in
  resolve       alias for `work --task level`  (kept: it is in muscle memory and cron)
  rank          score open matches against profile.yaml (a model helps; not required)
  today         the jobs to apply to today; --applied/--skip/--snooze to act on one
  dashboard     render state.db to a single self-contained HTML file
  serve         the same view, plus editing: decisions, criteria, dispositions
  report        re-render the latest state from state.db without fetching
  rematch       re-apply criteria to stored postings without fetching
  eval          replay criteria against your recorded judgments
  decide        record a judgment on one posting; --pin makes it an override
  apply         record an application and its outcome; --manual for an untracked job
  applications  list what you applied to, grouped by what needs doing
  mail          read the job-search mailbox and propose application updates; it never
                writes to the mailbox. `work --task inbox` is what reads the candidates
  verify-slugs  fetch each API board's identity; --write seeds expected_board_name
  repair        read careers pages for broken boards' new slugs; --write applies
  add-company   append a curated entry to companies.yaml
  migrate       backend-newgrad-2027-tracker.md -> companies.yaml (one-time)

Progress goes to stderr via `logging`; the report goes to stdout. `check > out.md` is
therefore still clean, and you can watch a run without waiting for it to finish.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from opentelemetry import metrics

from . import applications as apps_mod, build_version, config, health as health_mod, report as report_mod, safewrite, store, telemetry, tuning
from .criteria import load_criteria
# companies.yaml has one writer module now — `serve`'s add form needs the same appender
# `add-company` does, and two implementations of "append a curated entry" is how they
# would drift. Only the three names this file actually calls are imported; re-exporting
# the rest to keep old test imports working would be an unused import, and CI gates on
# F401.
from . import curation
from .curation import (
    companies_diff as _companies_diff,
    has_inline_comments as _has_inline_comments,
    rewrite_companies as _rewrite_companies,
)
from .tuning import apply_override
from .fetch import Fetcher
from .health import evaluate, is_degraded
from .match import match
from .migrate import _HEADER, migrate as run_migrate
from .models import Company
from .sources import get_source


log = logging.getLogger("jobtracker")

# Exit codes. When this runs unattended, the exit status is the *only* thing the
# scheduler sees, so it has to encode the operationally meaningful distinction rather
# than just "was anything non-OK". health.is_degraded() draws that line and explains
# why; this module only maps it onto a process exit status.
EXIT_OK = 0
EXIT_DEGRADED = 2

# Run-level metrics. These are the `runs` table's stats columns, expressed as something
# that can actually be graphed over time — see CLAUDE.md on retiring those columns.
_meter = metrics.get_meter("jobtracker.cli")
run_duration = _meter.create_histogram(
    "jobtracker.run.duration",
    unit="s",
    description="End-to-end wall time of a check run",
    explicit_bucket_boundaries_advisory=[5, 10, 20, 30, 45, 60, 120, 300],
)
boards_total = _meter.create_counter(
    "jobtracker.boards",
    unit="{board}",
    description="Boards evaluated, by resulting health status",
)
new_postings_total = _meter.create_counter(
    "jobtracker.postings.new",
    unit="{posting}",
    description="Postings seen for the first time",
)
matches_total = _meter.create_counter(
    "jobtracker.matches",
    unit="{posting}",
    description="New postings that satisfied the match criteria",
)
applications_total = _meter.create_counter(
    "jobtracker.applications",
    unit="{application}",
    description="Applications recorded, by status — the outer loop over `check`",
)
matches_rejected_total = _meter.create_counter(
    "jobtracker.matches.rejected",
    unit="{posting}",
    description="Rule-matched postings a human later rejected — the false-positive signal",
)


# Attributes the stdlib puts on every LogRecord. Anything on a record that is NOT one of
# these was attached by a caller via logging's `extra={...}`, and belongs in the
# structured output as its own field. Computed from a bare record so it tracks the
# stdlib rather than a hand-copied list that rots.
_RESERVED_LOGRECORD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class _JsonLogFormatter(logging.Formatter):
    """One JSON object per line — the structured format for deployed (non-TTY) runs.

    Hand-rolled on purpose: this repo installs from a two-line requirements file, and a
    structured logger is not worth breaking that (same rule that keeps a web framework
    out of server.py). Only fields that carry information are emitted; host/stream and
    other envelope data are the aggregator's job, not ours. Any `extra={...}` a caller
    attaches rides along as top-level keys, so a call site can log an event as data.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            # astimezone() attaches the offset: an aggregator must not have to guess the
            # zone, and this box runs America/New_York while UTC underneath (see CLAUDE.md).
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOGRECORD and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _log_formatter() -> logging.Formatter:
    """Structured JSON when deployed, human lines interactively.

    `JOBTRACKER_LOG_FORMAT` = json | text | auto (default auto). auto keys off whether
    stderr is a TTY: a terminal gets the readable `12:03:41 INFO  [12/56] Stripe ...`
    lines the repo is built around, while a pipe or container — where a human is not
    watching and an aggregator is — gets JSON. No machine identity is baked in; see the
    runtime contract in memory.
    """
    fmt = os.environ.get("JOBTRACKER_LOG_FORMAT", "auto").strip().lower()
    if fmt == "auto":
        fmt = "text" if sys.stderr.isatty() else "json"
    if fmt == "json":
        return _JsonLogFormatter()
    return logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")


def _setup_logging(verbose: bool, quiet: bool) -> None:
    """stderr only. INFO by default so a run is watchable without extra flags."""
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_log_formatter())
    logging.basicConfig(level=level, handlers=[handler])
    # urllib3's DEBUG chatter drowns out ours; -v is about this package.
    logging.getLogger("urllib3").setLevel(logging.INFO)


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# -- migrate -----------------------------------------------------------------------
def cmd_migrate(args: argparse.Namespace) -> int:
    entries = run_migrate(args.markdown, args.out)
    tiers: dict[object, int] = {}
    methods: dict[str, int] = {}
    for e in entries:
        tiers[e.get("tier", "—")] = tiers.get(e.get("tier", "—"), 0) + 1
        methods[e.get("check_method", "manual")] = (
            methods.get(e.get("check_method", "manual"), 0) + 1
        )
    print(f"Wrote {len(entries)} companies to {args.out}")
    print(f"  by check_method: {dict(sorted(methods.items()))}")
    print(f"  by tier:         {dict(sorted(tiers.items(), key=lambda kv: str(kv[0])))}")
    return 0


# -- check -------------------------------------------------------------------------
def _cache_descriptions(conn, fetcher, wanted, budget: int, today: str) -> None:
    """Fill in descriptions for the match/uncertain buckets, once per posting ever.

    Ashby and Lever ship `descriptionPlain` in the bulk payload, so those cost nothing
    and are simply stored. Greenhouse is the exception — we drop `?content=true` from
    the bulk call because it blows the timeout on large boards — so its descriptions
    are fetched one at a time, paced by the same per-host limiter as the sweep.

    Doing this in `check` rather than on demand later is what makes every downstream
    pass offline: `resolve` and `rank` read `state.db` and never touch an ATS. It is
    affordable because it is scoped and write-once — ~829 postings of backfill, then
    ~30-40 a night.

    Three rules hold this together:

      * NULL means never fetched, '' means fetched and empty. Only NULL is retried, so
        a description that genuinely does not exist is not re-requested every night.
      * `budget` caps requests per run so a bad night cannot turn a 30-second job into
        a 40-minute one. The remainder is simply picked up tomorrow.
      * A failure here is invisible to board health and can never produce EXIT_DEGRADED.
        A 500 on one job detail is not a broken board; the row stays NULL and retries.
    """
    if not wanted:
        return
    pending = [
        (company, p) for company, p in wanted
        if store.get_description(conn, p.company, p.ats_job_id) is None
    ]
    if not pending:
        return

    log.info("caching descriptions for %d posting(s) (budget %d)", len(pending), budget)
    stored = fetched = failed = no_detail = 0
    for company, posting in pending:
        if posting.description:  # Ashby/Lever: free, already in hand
            store.set_description(conn, posting.company, posting.ats_job_id,
                                  posting.description)
            stored += 1
            continue

        source = get_source(company.ats)
        if source is None or source.job_detail_url(company.slug, posting.ats_job_id) is None:
            # This source has no per-posting endpoint — the aggregator feeds have no
            # detail page at all, and Ashby/Lever answer from the bulk payload or not
            # at all. Record '' ("fetched and genuinely empty") rather than leaving
            # NULL, or every run re-attempts a fetch that cannot exist: 249 aggregator
            # postings were being counted as failures nightly, with no request made.
            store.set_description(conn, posting.company, posting.ats_job_id, "")
            no_detail += 1
            continue

        if fetched >= budget:
            continue
        text, posted_at = fetcher.fetch_job_detail(company, posting.ats_job_id)
        if text is None:
            failed += 1
            continue
        fetched += 1
        store.set_description(conn, posting.company, posting.ats_job_id, text)
        # The detail payload is also the only place Greenhouse exposes a true posted
        # date, so take it while we are here rather than paying for the request twice.
        source = get_source(company.ats)
        if posted_at and source is not None:
            day = source.normalize_posted_at(posted_at, today)
            if day:
                store.set_posted_on(conn, posting.company, posting.ats_job_id, day)

    remaining = len(pending) - stored - fetched - failed - no_detail
    log.info(
        "descriptions: %d free from bulk, %d fetched, %d failed, "
        "%d have no detail endpoint, %d deferred to tomorrow",
        stored, fetched, failed, no_detail, remaining,
    )


def cmd_check(args: argparse.Namespace) -> int:
    companies = config.load_companies(args.companies)
    criteria = load_criteria(args.criteria)
    today = args.since or _today()
    started = _now()
    run_started = time.monotonic()

    # Log the *resolved* input paths. In a container these can be either baked into the
    # image or mounted over, and the failure mode of getting it wrong is silent: a run
    # against a stale baked criteria.yaml ignores every rule you tuned and still exits 0.
    # One line in the log turns that into something you can actually see.
    log.info(
        "companies=%s criteria=%s db=%s",
        args.companies or config.COMPANIES_YAML,
        args.criteria,
        args.db or config.DB_PATH,
    )

    api = [c for c in companies if c.check_method == "api" and get_source(c.ats)]
    skipped = [c for c in companies if c.check_method == "api" and not get_source(c.ats)]
    for c in skipped:
        log.warning("no adapter for %s (ats=%s) — skipping", c.name, c.ats)

    # Aggregator feeds (community new-grad lists) need a board_url to fetch. An
    # aggregator entry without one — e.g. a repo whose current URL isn't yet confirmed —
    # stays in the "never scraped" bucket rather than failing the run every night.
    aggregators = [
        c
        for c in companies
        if c.check_method == "aggregator" and c.board_url and get_source(c.ats)
    ]

    log.info(
        "loaded %d companies: %d api + %d aggregator fetchable, %d manual (never scraped)",
        len(companies),
        len(api),
        len(aggregators),
        len(companies) - len(api) - len(skipped) - len(aggregators),
    )

    # Held open past matching: descriptions are fetched for the match/uncertain
    # buckets once the verdict is known, and the verdict is not known until the DB is
    # open. Closed in the `finally` below, after that pass.
    fetcher = Fetcher()
    results = fetcher.fetch_all(api)
    for c in aggregators:
        log.info("fetching aggregator %s", c.name)
        res = fetcher.fetch_aggregator(c)
        log.info(
            "aggregator %s: %s",
            c.name,
            f"FAIL {res.error}" if res.error else f"{len(res.postings)} postings",
        )
        results.append(res)

    config.ensure_data_dir()
    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    by_name = {c.name: c for c in api + aggregators}
    # One dict for the whole run: ~9k postings, a handful of overrides.
    overrides = store.load_overrides(conn)

    stats = {"companies": len(api), "ok": 0, "failed": 0, "new_postings": 0, "matches": 0}
    degraded: list[str] = []  # boards worth failing the run over — see EXIT_DEGRADED
    # Postings whose description is worth having: the match and uncertain buckets.
    # Rejects are excluded deliberately — there are ~7,100 open ones, and fetching
    # them would cost ~40 minutes and ~48MB to serve nothing that reads them.
    wanted: list[tuple] = []
    log.info("evaluating health, storing postings, and matching against criteria")
    for res in results:
        company = by_name[res.company]
        prior = store.get_health(conn, company.name)
        ever = store.ever_nonempty(conn, company.name)
        health = evaluate(company, res, prior, today, ever)
        store.upsert_health(conn, health, today)
        # 4 statuses × 4 ats values, so bounded. This is the series that tells you
        # "suspect_empty jumped from 2 to 9 overnight" without reading a report.
        boards_total.add(1, {"health.status": health.status.value, "ats": company.ats})

        if health.status.value == "ok":
            stats["ok"] += 1
            # Resolve the vendor's timestamp to a plain ISO day here, where both the
            # adapter and the run date are in hand. Adapters stay pure — `today` is an
            # argument, not a clock read — and everything downstream gets one sortable
            # format instead of three.
            source = get_source(company.ats)
            postings = res.postings
            if source is not None:
                postings = [
                    replace(p, posted_on=source.normalize_posted_at(p.posted_at, today))
                    for p in postings
                ]
            new_postings, _ = store.sync_postings(conn, company.name, postings, today)
            stats["new_postings"] += len(new_postings)
            # `postings`, not `res.postings`: new_postings holds the normalized copies,
            # and Posting is a frozen dataclass compared by value, so the membership
            # test below would silently never fire against the originals.
            for posting in postings:
                verdict = apply_override(match(posting, criteria), overrides)
                store.record_verdict(conn, verdict, today)
                if verdict.decision.value in ("match", "uncertain"):
                    wanted.append((company, posting))
                if verdict.decision.value == "match" and posting in new_postings:
                    stats["matches"] += 1
        else:
            stats["failed"] += 1
            log.warning("%s unhealthy: %s (%s)", company.name, health.status.value, res.error or "—")
            if is_degraded(health):
                degraded.append(f"{company.name}={health.status.value}")

    try:
        _cache_descriptions(conn, fetcher, wanted, args.max_descriptions, today)
    finally:
        fetcher.close()

    # Rows stored before `posted_on` existed. The raw vendor values were already there
    # and already correct — just unsortable — so this is pure re-typing, no refetch.
    # Self-draining: it only looks at posted_on IS NULL, so it is a no-op from run two.
    filled = store.backfill_posted_on(
        conn, {c.name: get_source(c.ats) for c in api + aggregators if get_source(c.ats)}, today
    )
    if filled:
        log.info("normalized posted_at -> posted_on for %d stored posting(s)", filled)

    store.record_run(conn, started, _now(), stats)
    conn.commit()
    run_duration.record(time.monotonic() - run_started)
    new_postings_total.add(stats["new_postings"])
    matches_total.add(stats["matches"])
    log.info(
        "run complete: %d ok, %d failed, %d new postings, %d new matches",
        stats["ok"],
        stats["failed"],
        stats["new_postings"],
        stats["matches"],
    )

    log.info("rendering report")
    text = report_mod.build_report(
        conn, companies, today, since=today, mark_manual=True, criteria=criteria
    )
    conn.commit()
    conn.close()

    if args.output:
        Path(args.output).write_text(text)
        log.info("report written to %s (%d bytes)", args.output, len(text))
    else:
        print(text)

    if degraded:
        # ERROR, not WARNING: this is the line that should surface in `systemctl status`
        # alongside the non-zero exit, so the two agree about what happened.
        log.error(
            "run DEGRADED — %d board(s) need attention: %s",
            len(degraded),
            ", ".join(sorted(degraded)),
        )
        return EXIT_DEGRADED
    return EXIT_OK


# -- verify-slugs ------------------------------------------------------------------
def cmd_verify_slugs(args: argparse.Namespace) -> int:
    companies = config.load_companies(args.companies)
    api = [c for c in companies if c.check_method == "api" and get_source(c.ats)]
    fetcher = Fetcher()
    observed: dict[str, str] = {}
    try:
        for c in api:
            res = fetcher.fetch_company(c)
            status = (
                f"FAIL {res.error}"
                if res.error
                else f"{len(res.postings):>4} jobs  board={res.observed_board_name!r}"
            )
            drift = ""
            if c.expected_board_name and res.observed_board_name:
                from .health import identity_matches

                if not identity_matches(c.expected_board_name, res.observed_board_name):
                    drift = "  <-- DRIFT vs expected " + repr(c.expected_board_name)
            print(f"{c.name:24} {c.ats}/{c.slug:22} {status}{drift}")
            if res.observed_board_name:
                observed[c.name] = res.observed_board_name
    finally:
        fetcher.close()

    if args.write:
        _write_expected_board_names(args.companies or config.COMPANIES_YAML, observed)
        print(f"\nSeeded expected_board_name for {len(observed)} companies.")
    return 0


def _write_expected_board_names(path: str | Path, observed: dict[str, str]) -> None:
    _rewrite_companies(
        path, {name: {"expected_board_name": value} for name, value in observed.items()}
    )


# -- repair ------------------------------------------------------------------------
def cmd_repair(args: argparse.Namespace) -> int:
    """Find and verify new slugs for boards that have been broken long enough.

    Propose-only unless `--write`. The whole point of the separation is that this can
    run unattended — after a degraded night, from a timer — without any path by which a
    scheduled process rewrites hand-verified curation data (DESIGN.md §2.3).
    """
    from . import repair as repair_mod
    from .models import BoardHealth, HealthStatus

    companies = config.load_companies(args.companies)
    companies_path = Path(args.companies or config.COMPANIES_YAML)
    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    today = args.since or _today()
    by_name = {c.name: c for c in companies}

    outcomes: list = []
    if args.company:
        # Named boards bypass the detector entirely. This is how you exercise the pass
        # on a board you already know moved, and how you test it.
        targets = []
        for name in args.company:
            company = by_name.get(name)
            if company is None:
                print(f"error: unknown company {name!r}", file=sys.stderr)
                conn.close()
                return 1
            health = store.get_health(conn, name)
            trigger = health.status.value if health else "forced"
            if not company.careers_page:
                outcomes.append(
                    repair_mod.Outcome(name, trigger, "no_careers_page",
                                       "no careers_page in companies.yaml to read")
                )
                continue
            targets.append(
                repair_mod.RepairTarget(
                    company, health or BoardHealth(name, HealthStatus.OK), trigger
                )
            )
    else:
        targets, outcomes = repair_mod.detect(
            companies, store.unhealthy_health(conn), args.min_failures
        )

    if args.limit:
        targets = targets[: args.limit]

    if not targets and not outcomes:
        print("No boards need repair.")
        conn.close()
        return EXIT_OK

    # Regex-only, since the model transport moved to sir-client (2026-08-13). The old
    # `find_board` call lived on the provider-based client that rewrite replaced, and it
    # was never the primary path — nine ordered regexes resolve most careers pages
    # outright, and the model only ever produced a *candidate* that faced the identical
    # verification a regex hit does. So losing it costs coverage on JavaScript-shell
    # pages and nothing else; those already reported `no_candidates` and still do.
    #
    # To restore it: port `find_board` onto `LlmClient.complete(system, user, schema)`,
    # which is async, and keep the grounding check in `repair._ask_model` where it is —
    # a model slug must still appear on the page it was shown.
    client = None

    fetcher = Fetcher()

    def verify(company, candidate):
        # Probe the candidate board through the real adapter for its ATS. Reusing
        # fetch_company means identity resolution, retries and the per-host limiter all
        # apply — a candidate is checked exactly the way a nightly board is.
        return fetcher.fetch_company(
            replace(company, ats=candidate.ats, slug=candidate.slug)
        )

    try:
        proposals, more_outcomes, stats = repair_mod.repair_boards(
            targets, fetcher.fetch_page, verify, client=client
        )
    finally:
        fetcher.close()
        if client is not None:
            client.close()

    outcomes = outcomes + more_outcomes
    for proposal in proposals:
        store.record_proposal(conn, proposal, today)
    conn.commit()

    text = repair_mod.render(proposals, outcomes, stats)
    if stats.no_candidate:
        text += (
            f"\n{stats.no_candidate} board(s) produced no verified candidate. Their "
            "careers pages carry no board identifier the regexes can read —\n"
            "  typically a JavaScript shell (HubSpot's is 519 KB with neither "
            "`greenhouse` nor `hubspotjobs` in it). Read the slug off the\n"
            "  rendered page by hand, then `add-company`/edit companies.yaml.\n"
        )

    updates = {
        p.company: {
            "ats": p.to_ats,
            "slug": p.to_slug,
            # Must move with the slug. Leaving the dead board's name behind would make
            # the *repaired* board fail identity on the very next run, turning a fix
            # into a new IDENTITY_DRIFT.
            "expected_board_name": p.board_name or None,
        }
        for p in proposals
    }

    if proposals:
        text += "\n## Proposed companies.yaml change\n\n```diff\n"
        text += _companies_diff(companies_path, updates)
        text += "```\n"
        if not args.write:
            text += (
                "\nNothing was written. Re-run with --write to apply, "
                "or edit companies.yaml by hand.\n"
            )

    print(text)

    if args.write and proposals:
        if _has_inline_comments(companies_path):
            log.error(
                "%s contains `#` comments outside the header; refusing to write "
                "because the YAML round-trip would delete them. Apply by hand.",
                companies_path,
            )
            conn.close()
            return 1
        _rewrite_companies(companies_path, updates)
        try:
            config.load_companies(companies_path)
        except Exception as exc:  # noqa: BLE001 — a bad write must be loud, not silent
            log.error("%s no longer parses after the write: %s", companies_path, exc)
            conn.close()
            return 1
        for proposal in proposals:
            store.mark_proposal_applied(conn, proposal.company, today)
        conn.commit()
        log.info("applied %d repair(s) to %s", len(proposals), companies_path)

    conn.close()
    # Boards that are still broken with no verified fix are exactly what a human needs
    # to look at, so the exit status says so rather than reporting a clean run.
    return EXIT_DEGRADED if outcomes else EXIT_OK


# -- rematch -----------------------------------------------------------------------
def cmd_rematch(args: argparse.Namespace) -> int:
    """Re-apply the criteria to every OPEN stored posting. No network.

    Use after tuning criteria.yaml to refresh verdicts without re-fetching boards.
    """
    from .models import Posting

    criteria = load_criteria(args.criteria)
    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    today = args.since or _today()
    overrides = store.load_overrides(conn)
    before = store.counts_by_verdict(conn)
    rows = conn.execute(
        "SELECT company, ats_job_id, title, location, url, posted_at, posted_on "
        "FROM postings WHERE closed_at IS NULL"
    ).fetchall()
    for r in rows:
        posting = Posting(
            company=r["company"],
            ats_job_id=r["ats_job_id"],
            title=r["title"],
            url=r["url"],
            location=r["location"] or "",
            posted_at=r["posted_at"],
            posted_on=r["posted_on"],
        )
        store.record_verdict(conn, apply_override(match(posting, criteria), overrides), today)
    conn.commit()
    after = store.counts_by_verdict(conn)
    conn.close()

    print(f"rematched {len(rows)} open postings ({len(overrides)} override(s) applied)")
    # Show the delta, not just the totals: after a rule change the question is always
    # "what moved", and diffing two dicts by eye is exactly the step people skip.
    for verdict in sorted(set(before) | set(after)):
        b, a = before.get(verdict, 0), after.get(verdict, 0)
        arrow = f"  {b} → {a}" + (f"  ({a - b:+d})" if a != b else "")
        print(f"  {verdict:<10}{arrow}")
    return 0


# -- work --------------------------------------------------------------------------
def _load_answers(path: Path | None):
    """answers.yaml, or None with a warning. Never fatal.

    Absent is a normal state — the file holds personal data, is gitignored, and a fresh
    checkout has none. Prefill reports itself unavailable and the other tasks are
    unaffected, which is the same posture as running with no model at all.
    """
    from .answers import load_answers

    resolved = Path(path) if path else config.ANSWERS_YAML
    if not resolved.exists():
        return None, resolved
    try:
        return load_answers(resolved), resolved
    except ValueError as exc:
        log.warning("answers.yaml did not load: %s", exc)
        return None, resolved


def _build_context(args: argparse.Namespace, today: str, fetcher=None):
    """Everything every task might need, loaded once.

    Loaded eagerly and tolerantly: a task whose configuration is missing reports itself
    unavailable at selection time, which reads as "prefill: unavailable (no answers.yaml)"
    in the report rather than as a crash three tasks later.
    """
    from . import rank as rank_mod
    from .profile import load_profile
    from .tasks import TaskContext

    criteria = load_criteria(args.criteria)
    companies = config.load_companies(args.companies)

    profile = None
    try:
        profile = load_profile(getattr(args, "profile", None) or config.PROFILE_YAML)
    except (FileNotFoundError, ValueError) as exc:
        log.warning("profile.yaml did not load: %s", exc)

    answers, answers_path = _load_answers(getattr(args, "answers", None))

    return TaskContext(
        today=today,
        criteria=criteria,
        profile=profile,
        answers=answers,
        answers_path=answers_path,
        tiers=rank_mod.tier_lookup(companies),
        companies={c.name: c for c in companies},
        fetcher=fetcher,
        # A path, not an open mailbox. `inbox` inspects it to know whether it is
        # configured at all, and reads nothing — the reading is `jobtracker mail`'s job.
        maildir=getattr(args, "maildir", None) or config.MAILDIR,
    )


def _rescore(conn, profile, criteria, tiers, today) -> tuple[int, int]:
    """Recompute every open match's score from the judgments already stored.

    Pure arithmetic — no model, no network — so it is cheap enough to run whenever
    anything might have moved. Returns (scored, unranked).

    This is deliberately not a task. It needs no model, it must run whether or not one
    is reachable, and it is arithmetic over rows the `judge` task already wrote. But it
    *is* a link in the chain: `judge` writes a ranking with a NULL score, and `prefill`
    only queues postings that have one. Without a rescore between them the queue looks
    permanently empty from `prefill`'s side, which is exactly the silent stall a derived
    queue is supposed to make impossible.
    """
    from . import rank as rank_mod

    scored = unranked = 0
    for row in store.ranked_matches(conn):
        breakdown = rank_mod.score_posting(
            row, profile, criteria, tiers.get(row["company"]), today
        )
        if breakdown is None:
            unranked += 1
            continue
        store.set_score(conn, row["company"], row["ats_job_id"], breakdown.total, today)
        scored += 1
    conn.commit()
    return scored, unranked


def _print_survey(candidates) -> None:
    print("Task queue, in the order the scheduler considers it:\n")
    for c in candidates:
        if c.unavailable:
            state = f"unavailable — {c.unavailable}"
        elif c.pending:
            state = f"{c.pending} pending"
            if c.blocked:
                state += f" ({c.blocked} set aside after repeated failures)"
        else:
            state = "nothing to do"
        print(f"  {c.task.priority:>3}  {c.task.name:<10} {state}")
        print(f"       {'':<10} {c.task.summary}")


def _refresh_gap_stubs(conn, ctx) -> int:
    """Mirror the open gaps into the tail of answers.yaml. Returns how many.

    The database is the truth; this block is a rendering of it, regenerated wholesale
    so that answering a question makes its stub disappear on the next run. Everything
    above the marker is yours and is never read, parsed, or rewritten.
    """
    from .answers import rewrite_gaps

    if ctx.answers is None or not ctx.answers_path or not Path(ctx.answers_path).exists():
        return 0
    gaps = store.open_gaps(conn)
    try:
        rewrite_gaps(ctx.answers_path, gaps)
    except OSError as exc:
        log.warning("could not update the unanswered block in answers.yaml: %s", exc)
    return len(gaps)


# `_work` has three outcomes, and two of them are "no report": the router could not be
# reached, or every task was drained. They read identically in the database and call for
# opposite responses from whoever is looking — go fix the GPU, versus nothing at all — so
# they are kept apart here rather than both surfacing as None.
UNREACHABLE = object()


async def _work(args: argparse.Namespace, conn, ctx, task_name: str | None):
    """Build a client, run one task, tear the client down. Async because the SDK is."""
    from . import llm as llm_pkg
    from .tasks import run_next

    base_url = llm_pkg.resolve_base_url(getattr(args, "llm_url", None))
    client = llm_pkg.LlmClient(model=getattr(args, "llm_model", None), base_url=base_url)
    try:
        if not await client.probe():
            # Unreachable is not an error: the queue is simply left as it was. It is
            # reported as its own state rather than folded into `run_next`'s None,
            # because "the router is down" and "there was nothing to do" ask for
            # opposite responses and used to print the same sentence.
            return UNREACHABLE
        from .tasks import DEFAULT_CONCURRENCY

        return await run_next(
            conn, client, ctx,
            task_name=task_name,
            budget=args.budget,
            concurrency=args.concurrency or DEFAULT_CONCURRENCY,
        )
    finally:
        await client.aclose()


def cmd_work(args: argparse.Namespace) -> int:
    """Run the next available model task, or the one you name.

    Selection is by priority, and priority is the pipeline's dependency chain: `level`
    turns uncertain postings into matches, `judge` scores matches, `prefill` works down
    scored matches. Draining the earliest stage that has work is therefore the same
    instruction as never starving a later one.

    Never fails for want of a model. With none configured or reachable it reports the
    queue and changes nothing, which makes it safe to run before the router is up.
    """
    from . import llm as llm_pkg
    from .tasks import survey, task_names

    if args.task and args.task not in task_names():
        print(f"error: unknown task {args.task!r}; known: {', '.join(task_names())}",
              file=sys.stderr)
        return 1

    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    today = args.since or _today()

    fetcher = None
    if args.task in (None, "prefill"):
        # Only prefill fetches, and only for a form it has never seen. Built here rather
        # than in the task so the socket ownership stays with the command, next to the
        # DB connection it is already responsible for closing.
        fetcher = Fetcher()

    try:
        ctx = _build_context(args, today, fetcher=fetcher)
        candidates = survey(conn, ctx)

        if args.dry_run:
            _print_survey(candidates)
            chosen = next((c for c in candidates if not c.unavailable and c.pending), None)
            print()
            if chosen is None:
                print("Nothing to do.")
            else:
                print(f"Would work: {chosen.task.name} "
                      f"({min(chosen.pending, args.budget or chosen.pending)} unit(s))")
            return 0

        if not llm_pkg.is_configured(args.llm_url):
            _print_survey(candidates)
            print("\nNo inference router configured — nothing was changed.")
            print("  Point at one with --llm-url http://HOST:PORT")
            print("  (or $JOBTRACKER_LLM_URL / $SIR_BASE_URL)")
            return 0

        report = asyncio.run(_work(args, conn, ctx, args.task))
        if report is UNREACHABLE:
            _print_survey(candidates)
            print("\nRouter unreachable — the queue was left as it was.")
            return 0
        if report is None:
            _print_survey(candidates)
            print("\nNothing to do — every task is drained or waiting on something "
                  "else. The survey above says which.")
            return 0

        log.info("work complete: %s", report.summary())
        print(report.summary())

        # Refresh the derived score before returning, so the next task in the chain sees
        # what this one produced. `judge` writes a ranking with a NULL score and
        # `prefill` only queues postings that have one, so without this a `work` loop
        # drains level, drains judge, and then reports "prefill: nothing to do" forever.
        if ctx.profile is not None:
            scored, _ = _rescore(conn, ctx.profile, ctx.criteria, ctx.tiers, today)
            log.debug("rescored %d open match(es)", scored)

        if report.task == "prefill":
            outstanding = _refresh_gap_stubs(conn, ctx)
            if outstanding:
                print(f"  {outstanding} question(s) still need an answer from you — "
                      f"see the end of {ctx.answers_path}, or the Settings tab.")
        return 0
    finally:
        if fetcher is not None:
            fetcher.close()
        conn.close()


def cmd_resolve(args: argparse.Namespace) -> int:
    """Settle the UNCERTAIN queue by reading descriptions. `work --task level`.

    Kept as its own command because it is in muscle memory, in the docs, and in
    whatever cron already calls it — but it is the same machinery aimed at one task,
    not a second implementation of it.
    """
    args.task = "level"
    args.dry_run = getattr(args, "dry_run", False)
    args.concurrency = getattr(args, "concurrency", None) or 4
    args.budget = args.limit
    return cmd_work(args)


# -- eval --------------------------------------------------------------------------
def cmd_eval(args: argparse.Namespace) -> int:
    """Replay the current criteria against every judgment you have recorded.

    Exits 1 when a regression exists, so this composes into a pre-apply gate rather
    than being something you have to remember to read.
    """
    criteria = load_criteria(args.criteria)
    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    decisions = store.all_decisions(conn)
    conn.close()

    if not decisions:
        print("No decisions recorded yet — nothing to evaluate.")
        print("Judge some postings first (`jobtracker decide`, or the tuning tab).")
        return 0

    report = tuning.evaluate(decisions, criteria)
    print(report.summary())

    if report.regressions:
        print(f"\nREGRESSIONS ({len(report.regressions)}) — rules contradict your judgment:")
        for c in report.regressions:
            print(f"  {c.company:<18} {c.title[:52]:<52} you:{c.yours:<8} rules:{c.rules}")
            print(f"  {'':<18} └─ fired: {c.rule_reason}")

    if report.unresolved and args.verbose:
        print(f"\nUnresolved ({len(report.unresolved)}) — rules say uncertain, you decided:")
        for c in report.unresolved:
            print(f"  {c.company:<18} {c.title[:52]:<52} you:{c.yours}")

    suggestions = tuning.suggest_rules(decisions, criteria, min_count=args.min_count)
    if suggestions:
        print(f"\nSuggested rules — phrases in ≥{args.min_count} rejects, never in an accept:")
        for s in suggestions:
            print(f"  {s.phrase!r:<28} {s.rejected} rejects   e.g. {s.examples[0][:44]!r}")

    return 1 if report.regressions else 0


# -- rank --------------------------------------------------------------------------
def cmd_rank(args: argparse.Namespace) -> int:
    """Judge open matches against profile.yaml and score the queue.

    Two phases, and only the first needs a model. Judging is the `judge` task, run
    through the same scheduler as everything else; scoring recomputes every score in
    Python. So a weights-only edit to profile.yaml re-sorts the whole queue with no
    model at all — which is why running this with the router down is useful rather than
    pointless.

    Scoring is deliberately not a task: it must run on every invocation whether or not
    a model was reachable, and it is arithmetic over rows the judge task already wrote.
    """
    from . import llm as llm_pkg, rank as rank_mod
    from .profile import load_profile

    criteria = load_criteria(args.criteria)
    try:
        profile = load_profile(args.profile)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    companies = config.load_companies(args.companies)
    tiers = rank_mod.tier_lookup(companies)
    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    today = args.since or _today()
    stats = rank_mod.RankStats()

    pending = store.matches_needing_judgment(conn, profile.prose_hash, limit=args.limit)
    if pending and not llm_pkg.is_configured(args.llm_url):
        log.info("no router configured — scoring with the judgments already held")
        print(f"No inference router configured — {len(pending)} match(es) left unjudged.")
        print("  Point at one with --llm-url http://HOST:PORT")
    elif pending:
        args.task = "judge"
        args.budget = args.limit
        args.concurrency = getattr(args, "concurrency", None) or 4
        ctx = _build_context(args, today)
        log.info("judging %d match(es) against profile %s", len(pending), profile.prose_hash)
        report = asyncio.run(_work(args, conn, ctx, "judge"))
        if report is not None and report is not UNREACHABLE:
            # `judged` and `unreadable` come off the task report rather than being
            # recounted here — the runner is what actually knows, and it already
            # committed each judgment as it landed.
            stats.considered = report.attempted
            stats.judged = report.applied
            stats.unreadable = report.no_answer + report.errors

    # Scoring is pure arithmetic over stored judgments — no model, every run.
    scored, unranked = _rescore(conn, profile, criteria, tiers, today)
    stats.scored, stats.unranked = scored, unranked

    top = rank_mod.top_n(store.ranked_matches(conn), 3, today)
    conn.close()

    log.info("rank complete: %s", stats.summary())
    print(stats.summary())
    if stats.unranked:
        # Surface the blind spot rather than hiding it: these are open matches the
        # model has not read, and they are excluded from the top 3.
        print(f"  {stats.unranked} match(es) unranked — run again with a model up.")
    if top:
        print("\nTop 3 for tomorrow:")
        for i, row in enumerate(top, 1):
            print(f"  {i}. {row['company']} — {row['title'][:60]}  [{row['score']:.1f}]")
    return 0


# -- prepare -----------------------------------------------------------------------
async def _prefill_picks(args, conn, ctx, picks) -> Optional[object]:
    """Build a prefill plan for exactly these postings. None if no router."""
    from . import llm as llm_pkg
    from .tasks import DEFAULT_CONCURRENCY, get_task, run_task

    task = get_task("prefill")
    wanted = {(p["company"], p["ats_job_id"]) for p in picks}
    units = [u for u in task.pending(conn, ctx) if (u.company, u.ats_job_id) in wanted]
    if not units:
        return None

    base_url = llm_pkg.resolve_base_url(getattr(args, "llm_url", None))
    client = llm_pkg.LlmClient(model=getattr(args, "llm_model", None), base_url=base_url)
    try:
        if not await client.probe():
            return None
        return await run_task(
            conn, task, client, ctx,
            units=units,
            concurrency=args.concurrency or DEFAULT_CONCURRENCY,
        )
    finally:
        await client.aclose()


def cmd_prepare(args: argparse.Namespace) -> int:
    """Make tomorrow morning's picks ready to apply to.

    The narrow, fast half of the nightly pipeline: rescore what is already judged, take
    the postings `today` will surface, and make sure each has a prefill plan. It reads
    no ATS board and does no level extraction — those belong to `check` and `work`,
    which this is meant to run *after*.

    **Gaps never make this fail.** A form with questions you have not answered yet is
    the normal state, especially early on, and treating it as a failure would make the
    job permanently red for a condition only you can clear — the same trap as flagging
    dbt Labs' legitimately empty board. What counts as not-ready is a pick with *no
    plan at all*, because that is the one thing that leaves you opening a blank form.
    """
    from . import rank as rank_mod

    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    today = args.since or _today()
    fetcher = Fetcher()
    try:
        ctx = _build_context(args, today, fetcher=fetcher)
        if ctx.profile is None:
            print("error: profile.yaml did not load — cannot rank or prepare",
                  file=sys.stderr)
            return 1

        _rescore(conn, ctx.profile, ctx.criteria, ctx.tiers, today)
        picks = rank_mod.top_n(store.ranked_matches(conn), args.count, today)
        if not picks:
            print("Nothing queued for tomorrow.")
            print("  Run `check` then `work` — or you have dispositioned everything.")
            return 0

        if ctx.answers is None:
            print(f"No answer bank at {ctx.answers_path} — cannot prefill.")
            print("  cp answers.example.yaml answers.yaml, then fill it in.")
        else:
            report = asyncio.run(_prefill_picks(args, conn, ctx, picks))
            if report is not None:
                log.info("prepare: %s", report.summary())
            _refresh_gap_stubs(conn, ctx)

        return _report_readiness(conn, picks, ctx)
    finally:
        fetcher.close()
        conn.close()


def _report_readiness(conn, picks, ctx) -> int:
    """Print whether each pick can actually be applied to, and exit accordingly."""
    plans = store.plans_by_posting(conn)
    ready = 0
    lines: list[str] = []

    for i, row in enumerate(picks, 1):
        plan = plans.get((row["company"], row["ats_job_id"]))
        head = f"  {i}. {row['company']} — {row['title'][:52]}"
        if plan is None:
            lines.append(f"{head}\n       NOT READY — {_why_no_plan(ctx, row)}")
            continue
        ready += 1
        filled = plan["fields"] - plan["gaps"]
        tail = f"{plan['gaps']} need you" if plan["gaps"] else "nothing left to type"
        lines.append(f"{head}\n       prefill {filled}/{plan['fields']} fields · {tail}")

    print(f"Tomorrow: {ready}/{len(picks)} ready to apply to\n")
    print("\n".join(lines))

    if ready < len(picks):
        print("\nA pick with no plan opens as a blank form. See docs/prefill.md.")
        return EXIT_DEGRADED
    return EXIT_OK


def _why_no_plan(ctx, row) -> str:
    """The actionable reason, not just the absence.

    Three different situations look identical in the database and need different things
    from you, so they are named rather than collapsed into "no plan".
    """
    from .sources import get_source

    company = ctx.companies.get(row["company"])
    if company is None:
        return f"{row['company']} is not in companies.yaml"
    if ctx.answers is None:
        return "no answer bank configured"
    source = get_source(company.ats)
    publishes = bool(
        company.slug
        and source is not None
        and source.application_form_url(company.slug, row["ats_job_id"])
    )
    if not publishes:
        return (
            f"{company.ats or 'this ATS'} does not publish its form — run "
            f"`jobtracker apply-to {company.name} {row['ats_job_id']}` once to learn it"
        )
    return "the router was unreachable, or the run was interrupted"


# -- apply-to ----------------------------------------------------------------------
def cmd_apply_to(args: argparse.Namespace) -> int:
    """Open one application with your answers already in the boxes, and stop there.

    The nearest honest thing to "a link that opens the form prefilled". It cannot be a
    link: no URL attaches a file, and only Lever honours query-parameter prefill. So the
    filling happens in a browser this command drives, and then hands to you.

    It never submits. It fills what it knows, outlines what it does not, and leaves the
    window open — see docs/prefill.md for why that boundary is where it is.
    """
    from . import browser as browser_mod, resumes

    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    today = args.since or _today()
    try:
        row = conn.execute(
            "SELECT company, ats_job_id, title, url FROM postings "
            "WHERE company=? AND ats_job_id=?",
            (args.company, args.job_id),
        ).fetchone()
        if row is None:
            print(f"error: no posting {args.company} / {args.job_id}", file=sys.stderr)
            return 1

        answers, answers_path = _load_answers(args.answers)
        if answers is None:
            print(f"error: no usable answer bank at {answers_path}.", file=sys.stderr)
            print("  Copy answers.example.yaml to answers.yaml and fill it in.",
                  file=sys.stderr)
            return 1

        companies = {c.name: c for c in config.load_companies(args.companies)}
        company = companies.get(args.company)
        if company is None:
            print(f"error: {args.company} is not in companies.yaml", file=sys.stderr)
            return 1

        plan = store.get_plan(conn, args.company, args.job_id)
        if plan is None:
            # Not fatal. The browser re-derives every rules-resolvable field itself, so
            # a plan is a head start rather than a prerequisite; without one you simply
            # lose the model's question-matching for this form.
            log.info("no prefill plan stored — filling from the rules alone")

        plan_json = plan["plan"] if plan else None
        # This posting's own resume, if it has one. Both halves, exactly as
        # `server._api_apply_to` does them — the terminal and the button must not
        # disagree about which file goes out under your name.
        override = resumes.override_for(conn, args.company, args.job_id)
        if override is not None:
            from .tasks.prefill import retarget_resume

            answers = replace(answers, resume=override)
            plan_json = retarget_resume(plan_json, str(override))
            log.info("attaching this posting's own resume: %s", override.name)

        print(f"{row['company']} — {row['title']}")
        report = browser_mod.fill_application(
            conn,
            company=company,
            ats_job_id=args.job_id,
            url=row["url"],
            answers=answers,
            today=today,
            user_data_dir=config.BROWSER_PROFILE,
            plan_json=plan_json,
            headless=args.headless,
        )
        if args.headless:
            print(report.summary())
            browser_mod._print_gaps(report)
        return 0
    except browser_mod.BrowserUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


# -- today -------------------------------------------------------------------------
def cmd_today(args: argparse.Namespace) -> int:
    """Show the three jobs to apply to today, or record what you did about one.

    A job holds its slot until you say what happened to it. That is what stops
    tomorrow from repeating today's list, and stops one you meant to apply to from
    quietly falling off the bottom.
    """
    from . import rank as rank_mod

    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    # `--since` for the same reason every other command has it: a snooze is "today plus
    # N days", so without a way to pin today, anything that asserts on the resulting date
    # is a time bomb that passes until midnight.
    today = args.since or _today()

    if args.snooze and args.days < 1:
        # A zero or negative snooze expires the moment it is written, which looks like
        # the command silently did nothing.
        print("error: --days must be at least 1", file=sys.stderr)
        conn.close()
        return 1

    action = args.applied or args.skip or args.snooze
    if action:
        company, job_id = action
        row = conn.execute(
            "SELECT company, ats_job_id, title, url, location FROM postings "
            "WHERE company=? AND ats_job_id=?",
            (company, job_id),
        ).fetchone()
        if row is None:
            print(f"error: no posting {company}/{job_id}", file=sys.stderr)
            conn.close()
            return 1
        now = _now()
        if args.applied:
            # advance_, not record_: applying is the first event in the application's
            # history. url/location are copied across because the posting row goes away
            # when the req closes.
            store.advance_application(
                conn, row["company"], row["ats_job_id"], row["title"], "applied",
                now, note=args.note, url=row["url"], location=row["location"],
                source="tracked",
                next_action=apps_mod.default_next_action(today),
                next_action_note="follow up",
            )
            applications_total.add(1)
            what = "applied"
        elif args.skip:
            store.set_deferral(conn, row["company"], row["ats_job_id"], "skipped",
                               now, note=args.note or "")
            what = "skipped"
        else:
            until = (date.fromisoformat(today) + timedelta(days=args.days)).isoformat()
            store.set_deferral(conn, row["company"], row["ats_job_id"], "snoozed",
                               now, until=until, note=args.note or "")
            what = f"snoozed until {until}"
        conn.commit()
        conn.close()
        print(f"{row['company']} — {row['title'][:60]}: {what}")
        return 0

    rows = store.ranked_matches(conn)
    conn.close()
    top = rank_mod.top_n(rows, args.count, today)
    unranked = sum(1 for r in rows if r["score"] is None)

    if not top:
        print("Nothing queued. Run `jobtracker rank` after `check`.")
        if unranked:
            print(f"  {unranked} open match(es) are unranked — the model has not read them.")
        return 0

    print(f"Top {len(top)} for {today}\n")
    for i, row in enumerate(top, 1):
        days = rank_mod.days_since(row["posted_on"], today)
        age = f"{days}d ago" if days is not None else "date unknown"
        print(f"{i}. {row['company']} — {row['title']}")
        print(f"   {row['location'] or 'location unspecified'} · posted {age} · "
              f"score {row['score']:.1f}")
        if row["why"]:
            print(f"   {row['why']}")
        print(f"   {row['url']}")
        print(f"   applied:  jobtracker today --applied {row['company']!r} {row['ats_job_id']}")
        print()
    if unranked:
        print(f"({unranked} open match(es) unranked and not shown — run `jobtracker rank`.)")
    return 0


# -- decide ------------------------------------------------------------------------
def cmd_decide(args: argparse.Namespace) -> int:
    """Record a judgment on one posting, and optionally pin it with an override."""
    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    row = conn.execute(
        "SELECT company, ats_job_id, title, location FROM postings "
        "WHERE company=? AND ats_job_id=?",
        (args.company, args.job_id),
    ).fetchone()
    if row is None:
        print(f"error: no posting {args.company}/{args.job_id}", file=sys.stderr)
        conn.close()
        return 1

    # A human reject on a posting the rules called `match` is a false positive — the
    # tuning loop's headline quality signal. Read the standing verdict before overwriting.
    if args.decision == "reject":
        prior = conn.execute(
            "SELECT verdict FROM verdicts WHERE company=? AND ats_job_id=?",
            (row["company"], row["ats_job_id"]),
        ).fetchone()
        if prior and prior["verdict"] == "match":
            matches_rejected_total.add(1)

    now = _now()
    store.record_decision(
        conn, row["company"], row["ats_job_id"], row["title"], args.decision,
        now, location=row["location"] or "", note=args.note,
    )
    if args.pin:
        store.set_override(
            conn, row["company"], row["ats_job_id"], args.decision, now,
            reason=args.note or "manual",
        )
    conn.commit()
    n = store.decision_count(conn)
    conn.close()
    pinned = " (pinned as an override)" if args.pin else ""
    print(f"recorded {args.decision}: {row['title']}{pinned}")
    print(f"corpus is now {n} decision(s) — run `jobtracker eval` before changing rules")
    return 0


# -- apply -------------------------------------------------------------------------
def cmd_apply(args: argparse.Namespace) -> int:
    """Record or advance an application — the outer loop.

    `check`/`resolve` find roles; this records which ones you acted on and what came
    of them, so tiers can eventually be re-ranked by what actually converts rather
    than by prior. Re-running with a new --status advances the same application, and
    every run appends to its history.

    `--manual` records a job the pipeline never surfaced — a referral, or something off
    LinkedIn. Those have no posting row, so the id is minted from the title; giving the
    same title twice updates the same application rather than creating a second one.
    """
    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    next_action = None
    if args.next_action:
        next_action = apps_mod.parse_day(args.next_action)
        if next_action is None:
            print(f"error: --next-action must be a date like {_today()}", file=sys.stderr)
            conn.close()
            return 1

    if args.manual:
        if not args.title:
            print("error: --manual needs --title", file=sys.stderr)
            conn.close()
            return 1
        company, job_id, title = args.company, store.manual_job_id(args.title), args.title
        url, location, source = args.url, args.location, "manual"
    else:
        if not args.job_id:
            print("error: job_id is required (or use --manual --title)", file=sys.stderr)
            conn.close()
            return 1
        row = conn.execute(
            "SELECT company, ats_job_id, title, url, location FROM postings "
            "WHERE company=? AND ats_job_id=?",
            (args.company, args.job_id),
        ).fetchone()
        if row is None:
            print(f"error: no posting {args.company}/{args.job_id}", file=sys.stderr)
            conn.close()
            return 1
        company, job_id, title = row["company"], row["ats_job_id"], row["title"]
        # An explicit flag wins; otherwise inherit the posting's own values, which are
        # the ones that disappear when the req closes.
        url = args.url or row["url"]
        location = args.location or row["location"]
        source = "tracked"

    store.advance_application(
        conn, company, job_id, title, args.status, _now(), note=args.note,
        url=url, location=location, source=source,
        next_action=next_action, next_action_note=args.next_action_note or None,
    )
    conn.commit()
    # status is a 7-value bounded set — safe as a metric attribute (CLAUDE.md).
    applications_total.add(1, {"status": args.status})
    n = store.application_count(conn)
    conn.close()
    print(f"recorded {args.status}: {title}")
    print(f"tracking {n} application(s)")
    return 0


def cmd_applications(args: argparse.Namespace) -> int:
    """List what you applied to, grouped the way the page groups it.

    The terminal mirror of `/applications`. Read-only, and it reads `applications`
    directly rather than joining through `postings`, so manual entries appear.
    """
    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    today = _today()
    apps = store.all_applications(conn)
    events_by = store.events_by_application(conn)
    conn.close()

    if not apps:
        print("no applications recorded yet")
        print("record one with `jobtracker apply --manual COMPANY --title TITLE`")
        return 0

    stats = apps_mod.summary(apps, events_by)
    print(f"{stats['total']} application(s) · {stats['active']} active · "
          f"{stats['interviewing']} interviewing · {stats['offers']} offer(s) · "
          f"{stats['response_rate']}% response rate")

    groups = apps_mod.group(apps, events_by, today)
    for key, heading in (("needs_action", "NEEDS ACTION"), ("active", "ACTIVE"),
                         ("closed", "CLOSED")):
        rows = groups[key]
        if not rows:
            continue
        print(f"\n{heading} ({len(rows)})")
        for app in rows:
            events = events_by.get((app["company"], app["ats_job_id"]), [])
            repeats = apps_mod.round_counts(events).get(app["status"], 0)
            times = f"x{repeats}" if repeats > 1 else ""
            bits = [f"{app['status']}{times}"]
            since = apps_mod.days_since(app["applied_at"], today)
            if since is not None:
                bits.append("applied today" if since <= 0 else f"applied {since}d ago")
            if apps_mod.is_stale(app, today):
                bits.append("NO MOVEMENT")
            until = apps_mod.days_until(app["next_action"], today)
            if until is not None and apps_mod.is_active(app):
                what = app["next_action_note"] or "follow up"
                bits.append(f"{what} {'overdue by ' + str(-until) if until < 0 else 'in ' + str(until)}d")
            print(f"  {app['company']} · {app['title']}")
            print(f"    {' · '.join(bits)}")
    return 0


# -- mail --------------------------------------------------------------------------
def cmd_mail(args: argparse.Namespace) -> int:
    """Read the job-search mailbox, narrow it against what you applied to, and record
    the candidates. Never writes to the mailbox.

    This is the deterministic half, and it is to `work --task inbox` exactly what `check`
    is to `level`: it does the I/O and caches what the model needs, so the task itself is
    a pure read of `state.db` and an unmounted volume cannot silently shrink a queue.

    `--list`, `--accept` and `--dismiss` never touch the maildir at all, so the whole
    loop works from a machine with no mail on it.
    """
    from . import mail as mail_mod, maildir as maildir_mod

    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    try:
        if args.accept or args.dismiss:
            return _mail_decide(conn, args)
        if args.list:
            return _mail_list(conn)

        path = Path(args.maildir) if args.maildir else config.MAILDIR
        if path is None:
            print("error: no maildir configured — set $JOBTRACKER_MAILDIR or pass "
                  "--maildir", file=sys.stderr)
            return 1
        if not maildir_mod.is_maildir(path):
            # Not "0 messages". An unmounted volume reporting zero is the
            # `greenhouse/hubspot` failure with a filesystem instead of a board.
            print(f"error: {path} is not a maildir (no cur/ and new/)", file=sys.stderr)
            return 1

        apps = store.all_applications(conn)
        if not apps:
            print("no applications recorded yet — nothing to match mail against")
            return 0
        companies = config.load_companies(args.companies)
        index = mail_mod.build_index(apps, companies)
        known = store.known_message_ids(conn)

        read = unreadable = skipped = new = 0
        for item in maildir_mod.read_messages(path, limit=args.limit):
            if not item.ok:
                unreadable += 1
                continue
            read += 1
            message = mail_mod.parse(item.raw)
            if message is None:
                unreadable += 1
                continue
            if message.message_id in known:
                skipped += 1
                continue
            cand = mail_mod.narrow(message, index, since=args.since)
            if cand is None:
                continue
            if store.record_mail_candidate(conn, cand, _today()):
                new += 1
        conn.commit()

        parts = [f"read {read:,} messages", f"{skipped:,} already known",
                 f"{new} new candidate(s)"]
        if unreadable:
            parts.append(f"{unreadable} unreadable")
        print(" · ".join(parts))
        if new:
            print("\nrun `jobtracker work --task inbox` to have them read")
        _mail_list(conn)
        # A message that could not be read must never contribute to "no new mail today".
        return EXIT_DEGRADED if unreadable else EXIT_OK
    finally:
        conn.close()


def _mail_list(conn) -> int:
    rows = store.pending_mail_proposals(conn)
    if not rows:
        print("\nno proposals awaiting review")
        return 0
    print(f"\n{len(rows)} proposal(s) awaiting review")
    for row in rows:
        where = row["ats_job_id"] or "(which application?)"
        print(f"  {row['status']:<10} {row['company']} · {where}")
        print(f"             {row['subject'][:70]!r}  {row['sent_on'] or ''}")
        if row["evidence"]:
            print(f"             “{row['evidence'][:80]}”")
        print(f"             accept: jobtracker mail --accept {row['message_id']!r}")
    return 0


def _mail_decide(conn, args: argparse.Namespace) -> int:
    message_id = args.accept or args.dismiss
    proposal = store.get_mail_proposal(conn, message_id)
    if proposal is None:
        print(f"error: no proposal for {message_id}", file=sys.stderr)
        return 1
    if proposal["resolution"] != "pending":
        print(f"error: already {proposal['resolution']}", file=sys.stderr)
        return 1

    now = datetime.now().isoformat(timespec="seconds")
    if args.dismiss:
        store.resolve_mail_proposal(conn, message_id, "dismissed", now)
        conn.commit()
        print(f"dismissed — {proposal['company']}")
        return 0

    job_id = args.job or proposal["ats_job_id"]
    if not job_id:
        print("error: this proposal does not say which application it is about; "
              "pass --job ATS_JOB_ID", file=sys.stderr)
        return 1
    app = store.get_application(conn, proposal["company"], job_id)
    if app is None:
        print(f"error: no application {proposal['company']} / {job_id}", file=sys.stderr)
        return 1

    cand = conn.execute(
        "SELECT subject, sent_on FROM mail_candidates WHERE message_id=?", (message_id,)
    ).fetchone()
    subject = (cand["subject"] if cand else "") or "(no subject)"
    sent_on = (cand["sent_on"] if cand else "") or ""
    note = f"from mail: {subject[:80]}" + (f" ({sent_on})" if sent_on else "")

    store.advance_application(conn, proposal["company"], job_id, app["title"],
                              proposal["status"], now, note=note)
    store.resolve_mail_proposal(conn, message_id, "accepted", now)
    conn.commit()
    applications_total.add(1, {"status": proposal["status"]})
    print(f"{proposal['company']} · {app['title']} → {proposal['status']}")
    return 0


# -- report ------------------------------------------------------------------------
def cmd_report(args: argparse.Namespace) -> int:
    companies = config.load_companies(args.companies)
    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    since = args.since or _today()
    criteria = load_criteria(args.criteria)
    text = report_mod.build_report(
        conn, companies, _today(), since=since, mark_manual=False, criteria=criteria
    )
    conn.close()
    print(text)
    return 0


# -- dashboard ---------------------------------------------------------------------
def cmd_dashboard(args: argparse.Namespace) -> int:
    """Render state.db as a self-contained HTML page. No network, no writes.

    Defaults to a file rather than stdout: the output is ~1 MB of HTML that you open in
    a browser, not something you pipe. `--output -` still writes to stdout if you want it.
    """
    from . import dashboard as dashboard_mod

    companies = config.load_companies(args.companies)
    criteria = load_criteria(args.criteria)
    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    html_doc = dashboard_mod.build_dashboard(conn, companies, _today(), criteria)
    conn.close()

    if args.output == "-":
        print(html_doc)
        return 0
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_doc, encoding="utf-8")
    log.info("dashboard written to %s (%d bytes)", out, len(html_doc.encode()))
    print(out)
    return 0


# -- serve -------------------------------------------------------------------------
def cmd_serve(args: argparse.Namespace) -> int:
    """Live tuning UI. The counterpart to `dashboard`, which stays a static export."""
    from . import server as server_mod

    return server_mod.serve(
        db_path=config.DB_PATH if args.db is None else Path(args.db),
        criteria_path=Path(args.criteria),
        companies_path=Path(args.companies) if args.companies else None,
        host=args.host,
        port=args.port,
        answers_path=Path(args.answers) if args.answers else config.ANSWERS_YAML,
    )


# -- add-company -------------------------------------------------------------------
def cmd_add_company(args: argparse.Namespace) -> int:
    """Append a curated entry to companies.yaml.

    Shares `curation.insert_entry` and `curation.validate_new` with the add form on
    `/companies`, so the button and the terminal write the same file the same way — the
    rule `apply-to` already follows. Before that they disagreed: this command
    round-tripped the whole document through PyYAML, which re-folds every `notes:` block
    to its own width, so adding one company produced a diff touching entries nobody had
    edited. That is also why `_has_inline_comments` is not called here any more: the
    guard exists for a round-trip that discards comments, and there is no longer one.

    It does not verify the slug — `/companies` does, and so does `verify-slugs`. Here the
    guarantee stops at "this is coherent and the name is not already taken".
    """
    path = Path(args.companies or config.COMPANIES_YAML)
    company = Company(
        name=(args.name or "").strip(),
        ats=(args.ats or "").strip(),
        slug=(args.slug or "").strip(),
        tier=args.tier,
        category=(args.category or "").strip(),
        check_method=args.check_method,
        expected_board_name=(args.expected_board_name or "").strip() or None,
        careers_page=(args.careers_page or "").strip(),
        board_url=(args.board_url or "").strip(),
        notes=(args.notes or "").strip(),
    )

    existing = config.load_companies(path) if path.exists() else []
    errors = curation.validate_new(company, existing)
    if errors:
        for message in errors:
            print(f"error: {message}", file=sys.stderr)
        return 1

    # A file that does not exist yet, or holds only its header, still has to produce a
    # valid one-entry document. The old round-trip raised TypeError on the header-only
    # case — `yaml.safe_load` returns None and `any(... for e in None)` blows up.
    text = path.read_text() if path.exists() else _HEADER
    after = curation.insert_entry(text, company)
    try:
        safewrite.write_text(path, after, config.load_companies)
    except safewrite.RefusedWrite as exc:
        print(f"error: refused invalid companies.yaml: {exc}", file=sys.stderr)
        return 1
    print(curation.diff(str(path), text, after), end="")
    print(f"added {company.name} to {path}")
    return 0


# -- arg parsing -------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    # Raw, not the default reflow: the module docstring is a subcommand table and an
    # ordered pipeline, both of which argparse otherwise rewraps into one prose blob.
    p = argparse.ArgumentParser(
        prog="jobtracker",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--companies", help="path to companies.yaml (default: repo root)")
    verbosity = p.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v", "--verbose", action="store_true", help="per-request detail (DEBUG) on stderr"
    )
    verbosity.add_argument(
        "-q", "--quiet", action="store_true", help="warnings and errors only on stderr"
    )
    p.add_argument(
        "--telemetry",
        choices=telemetry.MODES,
        default=telemetry.DEFAULT_MODE,
        help="OpenTelemetry traces: off (default), console, or otlp "
        "(honors $OTEL_EXPORTER_OTLP_ENDPOINT). Env: $JOBTRACKER_TELEMETRY",
    )
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("migrate", help="markdown -> companies.yaml")
    m.add_argument("--markdown", default="backend-newgrad-2027-tracker.md")
    m.add_argument("--out", default=str(config.COMPANIES_YAML))
    m.set_defaults(func=cmd_migrate)

    c = sub.add_parser("check", help="run the daily pipeline")
    c.add_argument("--criteria", default=str(config.CRITERIA_YAML))
    c.add_argument("--db", default=None)
    c.add_argument("--since", default=None, help="override today's date (YYYY-MM-DD)")
    c.add_argument("--output", default=None, help="write report to a file instead of stdout")
    c.add_argument(
        "--max-descriptions", type=int, default=400,
        help="cap per-posting description fetches this run (default: 400). The "
             "remainder is picked up tomorrow; only Greenhouse costs a request.",
    )
    c.set_defaults(func=cmd_check)

    v = sub.add_parser("verify-slugs", help="fetch board identities; --write seeds them")
    v.add_argument("--write", action="store_true")
    v.set_defaults(func=cmd_verify_slugs)

    rp = sub.add_parser(
        "repair", help="read careers pages for broken boards' new slugs; --write applies"
    )
    rp.add_argument("--db", default=None)
    rp.add_argument("--since", default=None, help="override today's date (YYYY-MM-DD)")
    rp.add_argument(
        "--company", action="append", default=None,
        help="repair this company regardless of health (repeatable)",
    )
    rp.add_argument(
        "--min-failures", type=int, default=health_mod.REPAIR_FAILURE_THRESHOLD,
        help="consecutive failed nights before a FETCH_FAILED board qualifies "
             f"(default: {health_mod.REPAIR_FAILURE_THRESHOLD})",
    )
    rp.add_argument("--limit", type=int, default=None, help="cap boards worked this run")
    rp.add_argument(
        "--write", action="store_true",
        help="apply verified proposals to companies.yaml (default: propose only)",
    )
    # No --llm-* flags: this pass is regex-only since the transport moved to sir-client.
    # Accepting them would advertise a fallback that no longer exists, and a flag that
    # silently does nothing is worse than an absent one.
    rp.set_defaults(func=cmd_repair)

    rm = sub.add_parser("rematch", help="re-apply criteria to stored postings (no network)")
    rm.add_argument("--criteria", default=str(config.CRITERIA_YAML))
    rm.add_argument("--db", default=None)
    rm.add_argument("--since", default=None)
    rm.set_defaults(func=cmd_rematch)

    def _llm_flags(parser) -> None:
        # One transport now, so there is no --llm-provider: the router is the thing that
        # knows which backend serves what. An address is the whole configuration.
        parser.add_argument("--llm-url", default=None,
                            help="http://HOST:PORT of the inference router. "
                                 "Env: $JOBTRACKER_LLM_URL or $SIR_BASE_URL")
        parser.add_argument("--llm-model", default=None,
                            help="model tag (default: ask the router what it serves)")

    w = sub.add_parser("work", help="run the next available model task")
    w.add_argument("--task", default=None,
                   help="pin the task instead of letting the scheduler pick")
    w.add_argument("--criteria", default=str(config.CRITERIA_YAML))
    w.add_argument("--profile", default=str(config.PROFILE_YAML))
    w.add_argument("--answers", default=None,
                   help=f"answer bank for prefill (default: {config.ANSWERS_YAML})")
    w.add_argument("--db", default=None)
    w.add_argument("--since", default=None)
    w.add_argument("--budget", type=int, default=None,
                   help="stop after N units (default: drain the task)")
    w.add_argument("--concurrency", type=int, default=None,
                   help="units in flight at once (default 4)")
    w.add_argument("--dry-run", action="store_true",
                   help="show the queue and what would be worked; change nothing")
    _llm_flags(w)
    w.set_defaults(func=cmd_work)

    rs = sub.add_parser("resolve", help="alias for `work --task level`")
    rs.add_argument("--criteria", default=str(config.CRITERIA_YAML))
    rs.add_argument("--profile", default=str(config.PROFILE_YAML))
    rs.add_argument("--answers", default=None)
    rs.add_argument("--db", default=None)
    rs.add_argument("--since", default=None)
    rs.add_argument("--limit", type=int, default=None,
                    help="stop after N postings (default: the whole queue)")
    rs.add_argument("--concurrency", type=int, default=None)
    rs.add_argument("--dry-run", action="store_true")
    _llm_flags(rs)
    rs.set_defaults(func=cmd_resolve)

    rk = sub.add_parser("rank", help="score open matches against profile.yaml")
    rk.add_argument("--criteria", default=str(config.CRITERIA_YAML))
    rk.add_argument("--profile", default=str(config.PROFILE_YAML))
    rk.add_argument("--answers", default=None)
    rk.add_argument("--db", default=None)
    rk.add_argument("--since", default=None)
    rk.add_argument("--limit", type=int, default=None,
                    help="judge at most N postings (scoring always covers all)")
    rk.add_argument("--concurrency", type=int, default=None)
    rk.add_argument("--dry-run", action="store_true")
    _llm_flags(rk)
    rk.set_defaults(func=cmd_rank)

    pr = sub.add_parser(
        "prepare",
        help="make tomorrow's picks ready to apply to (rescore + prefill the top N)",
    )
    pr.add_argument("--criteria", default=str(config.CRITERIA_YAML))
    pr.add_argument("--profile", default=str(config.PROFILE_YAML))
    pr.add_argument("--answers", default=None)
    pr.add_argument("--db", default=None)
    pr.add_argument("--since", default=None)
    pr.add_argument("--count", type=int, default=3,
                    help="how many picks to prepare (default: 3, matching `today`)")
    pr.add_argument("--concurrency", type=int, default=None)
    _llm_flags(pr)
    pr.set_defaults(func=cmd_prepare)

    at = sub.add_parser(
        "apply-to",
        help="open an application in a browser with your answers already filled in",
    )
    at.add_argument("company")
    at.add_argument("job_id")
    at.add_argument("--answers", default=None)
    at.add_argument("--db", default=None)
    at.add_argument("--since", default=None)
    at.add_argument("--headless", action="store_true",
                    help="do not show the window (discovery only; nothing is submitted "
                         "either way)")
    at.set_defaults(func=cmd_apply_to)

    td = sub.add_parser("today", help="the jobs to apply to today, or record what you did")
    td.add_argument("--db", default=None)
    td.add_argument("--since", default=None,
                    help="treat this ISO date as today (snoozes are measured from it)")
    td.add_argument("--count", type=int, default=3, help="how many to show (default: 3)")
    td.add_argument("--applied", nargs=2, metavar=("COMPANY", "JOB_ID"),
                    help="record that you applied; also logs it in `applications`")
    td.add_argument("--skip", nargs=2, metavar=("COMPANY", "JOB_ID"),
                    help="drop this one from the queue for good")
    td.add_argument("--snooze", nargs=2, metavar=("COMPANY", "JOB_ID"),
                    help="defer it; it returns on its own after --days")
    td.add_argument("--days", type=int, default=7, help="snooze length (default: 7)")
    td.add_argument("--note", default=None)
    td.set_defaults(func=cmd_today)

    e = sub.add_parser("eval", help="replay criteria against your recorded judgments")
    e.add_argument("--criteria", default=str(config.CRITERIA_YAML))
    e.add_argument("--db", default=None)
    e.add_argument("--min-count", type=int, default=3,
                   help="rejects a phrase needs before it is suggested (default: 3)")
    e.set_defaults(func=cmd_eval)

    d2 = sub.add_parser("decide", help="record your judgment on one posting")
    d2.add_argument("company")
    d2.add_argument("job_id")
    d2.add_argument("decision", choices=["match", "reject"])
    d2.add_argument("--note", default="", help="why — shown in eval output")
    d2.add_argument("--pin", action="store_true",
                    help="also set a per-posting override, so rematch cannot undo it")
    d2.add_argument("--db", default=None)
    d2.set_defaults(func=cmd_decide)

    ap = sub.add_parser("apply", help="record or advance an application")
    ap.add_argument("company")
    # Optional, because --manual mints one from the title instead.
    ap.add_argument("job_id", nargs="?", default=None)
    ap.add_argument("--manual", action="store_true",
                    help="a job the pipeline never surfaced; needs --title")
    ap.add_argument("--title", default="", help="role title, with --manual")
    ap.add_argument("--status", choices=store.APPLICATION_STATUSES, default="applied",
                    help="application state (default: applied); re-run to advance it")
    ap.add_argument("--note", default="", help="what happened, e.g. 'round 2 — sys design'")
    ap.add_argument("--url", default="", help="where to reopen it")
    ap.add_argument("--location", default="")
    ap.add_argument("--next-action", default="", help="ISO date to follow up on")
    ap.add_argument("--next-action-note", default="", help="what that follow-up is")
    ap.add_argument("--db", default=None)
    ap.set_defaults(func=cmd_apply)

    apps_p = sub.add_parser("applications", help="list what you applied to")
    apps_p.add_argument("--db", default=None)
    apps_p.set_defaults(func=cmd_applications)

    ml = sub.add_parser("mail", help="read the mailbox and propose application updates")
    ml.add_argument("--maildir", default=None,
                    help="default $JOBTRACKER_MAILDIR. Read-only, always.")
    ml.add_argument("--db", default=None)
    ml.add_argument("--since", default=None,
                    help="ignore mail older than this ISO day")
    ml.add_argument("--limit", type=int, default=None,
                    help="stop after this many messages")
    ml.add_argument("--list", action="store_true",
                    help="show pending proposals; reads no mail")
    ml.add_argument("--accept", default=None, metavar="MESSAGE_ID",
                    help="agree, and move the application")
    ml.add_argument("--job", default=None, metavar="ATS_JOB_ID",
                    help="with --accept, when the proposal does not say which")
    ml.add_argument("--dismiss", default=None, metavar="MESSAGE_ID")
    ml.set_defaults(func=cmd_mail)

    r = sub.add_parser("report", help="re-render state.db without fetching")
    r.add_argument("--criteria", default=str(config.CRITERIA_YAML))
    r.add_argument("--db", default=None)
    r.add_argument("--since", default=None)
    r.set_defaults(func=cmd_report)

    d = sub.add_parser("dashboard", help="render state.db as a self-contained HTML page")
    d.add_argument("--criteria", default=str(config.CRITERIA_YAML))
    d.add_argument("--db", default=None)
    d.add_argument(
        "--output",
        default="data/dashboard.html",
        help="output path, or '-' for stdout (default: data/dashboard.html)",
    )
    d.set_defaults(func=cmd_dashboard)

    sv = sub.add_parser("serve", help="live tuning UI on localhost")
    sv.add_argument("--criteria", default=str(config.CRITERIA_YAML))
    sv.add_argument("--answers", default=None)
    sv.add_argument("--db", default=None)
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--host", default="127.0.0.1",
                    help="default 127.0.0.1 — it has no auth and can edit criteria.yaml")
    sv.set_defaults(func=cmd_serve)

    a = sub.add_parser("add-company", help="append a curated entry")
    a.add_argument("--name", required=True)
    a.add_argument("--ats", required=True)
    a.add_argument("--slug", default="")
    a.add_argument("--tier", type=int, default=None)
    a.add_argument("--category", default="")
    a.add_argument("--check-method", dest="check_method", default="manual")
    a.add_argument("--careers-page", dest="careers_page", default="",
                   help="human-facing careers URL; the fallback when the API breaks, "
                        "and the page `repair` reads to find a moved slug")
    a.add_argument("--board-url", dest="board_url", default="",
                   help="raw feed URL, for check_method: aggregator")
    a.add_argument("--expected-board-name", dest="expected_board_name", default="",
                   help="asserted on every run to catch identity drift. Leave unset and "
                        "`verify-slugs --write` seeds it from the board itself")
    a.add_argument("--notes", default="")
    a.set_defaults(func=cmd_add_company)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.quiet)
    # Which build is running, on every subcommand rather than just `check`. A nightly
    # host runs four of them, and an image that failed to update looks exactly like one
    # that did: same duration, same output, exit 0. This line is the difference.
    log.info("jobtracker %s", build_version(), extra={"version": build_version()})
    # Must happen before any traced code runs, but note that fetch.py grabbed its tracer
    # at import time and still picks this up — providers resolve lazily, by design.
    telemetry.configure(args.telemetry)
    try:
        return args.func(args)
    finally:
        telemetry.shutdown()  # flush queued spans; atexit is a backstop, not a plan


if __name__ == "__main__":
    raise SystemExit(main())
