"""Command-line entry point.

Subcommands:
  migrate       backend-newgrad-2027-tracker.md -> companies.yaml (one-time)
  check         the daily pipeline: fetch -> health -> store -> match -> report
  verify-slugs  fetch each API board's identity; --write seeds expected_board_name
  report        re-render the latest state from state.db without fetching
  add-company   append a curated entry to companies.yaml

Only `check` touches the network in the normal daily path; `report` is offline.

Progress goes to stderr via `logging`; the report goes to stdout. `check > out.md` is
therefore still clean, and you can watch a run without waiting for it to finish.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

import yaml
from opentelemetry import metrics

from . import config, report as report_mod, store, telemetry, tuning
from .criteria import load_criteria
from .tuning import apply_override
from .fetch import Fetcher
from .health import evaluate, is_degraded
from .match import match
from .migrate import migrate as run_migrate
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


def _setup_logging(verbose: bool, quiet: bool) -> None:
    """stderr only. INFO by default so a run is watchable without extra flags."""
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
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

    log.info(
        "loaded %d companies: %d fetchable, %d manual/aggregator (never scraped)",
        len(companies),
        len(api),
        len(companies) - len(api) - len(skipped),
    )

    fetcher = Fetcher()
    try:
        results = fetcher.fetch_all(api)
    finally:
        fetcher.close()

    config.ensure_data_dir()
    conn = store.connect(config.DB_PATH if args.db is None else Path(args.db))
    by_name = {c.name: c for c in api}
    # One dict for the whole run: ~9k postings, a handful of overrides.
    overrides = store.load_overrides(conn)

    stats = {"companies": len(api), "ok": 0, "failed": 0, "new_postings": 0, "matches": 0}
    degraded: list[str] = []  # boards worth failing the run over — see EXIT_DEGRADED
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
            new_postings, _ = store.sync_postings(conn, company.name, res.postings, today)
            stats["new_postings"] += len(new_postings)
            for posting in res.postings:
                verdict = apply_override(match(posting, criteria), overrides)
                store.record_verdict(conn, verdict, today)
                if verdict.decision.value == "match" and posting in new_postings:
                    stats["matches"] += 1
        else:
            stats["failed"] += 1
            log.warning("%s unhealthy: %s (%s)", company.name, health.status.value, res.error or "—")
            if is_degraded(health):
                degraded.append(f"{company.name}={health.status.value}")

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
    path = Path(path)
    data = yaml.safe_load(path.read_text())
    for entry in data:
        name = entry.get("name")
        if name in observed:
            entry["expected_board_name"] = observed[name]
    from .migrate import _HEADER

    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    path.write_text(_HEADER + body)


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
        "SELECT company, ats_job_id, title, location, url, posted_at "
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


# -- add-company -------------------------------------------------------------------
def cmd_add_company(args: argparse.Namespace) -> int:
    path = Path(args.companies or config.COMPANIES_YAML)
    data = yaml.safe_load(path.read_text()) if path.exists() else []
    if any(e.get("name") == args.name for e in data):
        print(f"error: {args.name!r} already present", file=sys.stderr)
        return 1
    entry = {"name": args.name, "ats": args.ats}
    if args.slug:
        entry["slug"] = args.slug
    if args.tier is not None:
        entry["tier"] = args.tier
    if args.category:
        entry["category"] = args.category
    entry["check_method"] = args.check_method
    if args.notes:
        entry["notes"] = args.notes
    entry["expected_board_name"] = None
    data.append(entry)
    from .migrate import _HEADER

    path.write_text(_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))
    print(f"added {args.name} to {path}")
    return 0


# -- arg parsing -------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobtracker", description=__doc__)
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
    c.set_defaults(func=cmd_check)

    v = sub.add_parser("verify-slugs", help="fetch board identities; --write seeds them")
    v.add_argument("--write", action="store_true")
    v.set_defaults(func=cmd_verify_slugs)

    rm = sub.add_parser("rematch", help="re-apply criteria to stored postings (no network)")
    rm.add_argument("--criteria", default=str(config.CRITERIA_YAML))
    rm.add_argument("--db", default=None)
    rm.add_argument("--since", default=None)
    rm.set_defaults(func=cmd_rematch)

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

    a = sub.add_parser("add-company", help="append a curated entry")
    a.add_argument("--name", required=True)
    a.add_argument("--ats", required=True)
    a.add_argument("--slug", default="")
    a.add_argument("--tier", type=int, default=None)
    a.add_argument("--category", default="")
    a.add_argument("--check-method", dest="check_method", default="manual")
    a.add_argument("--notes", default="")
    a.set_defaults(func=cmd_add_company)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.quiet)
    # Must happen before any traced code runs, but note that fetch.py grabbed its tracer
    # at import time and still picks this up — providers resolve lazily, by design.
    telemetry.configure(args.telemetry)
    try:
        return args.func(args)
    finally:
        telemetry.shutdown()  # flush queued spans; atexit is a backstop, not a plan


if __name__ == "__main__":
    raise SystemExit(main())
