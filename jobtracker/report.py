"""Render the daily report: new matches, the uncertain bucket, failures, and the
weekly manual-check list.

Reporting is honest about coverage (DESIGN.md §9): manual companies are surfaced, not
hidden, and fetch failures get their own section rather than being absorbed into "no new
postings". The uncertain bucket is shown as a number so a bloated one is visible as a
signal that the rules need work, not swallowed as cost.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from . import store
from .criteria import Criteria
from .match import location_rank
from .models import Company

MANUAL_INTERVAL_DAYS = 7


def build_report(
    conn: sqlite3.Connection,
    companies: list[Company],
    today: str,
    since: str,
    mark_manual: bool = True,
    criteria: Criteria | None = None,
) -> str:
    """Render the daily report.

    `criteria` is optional only so a caller with no criteria file still gets a report;
    pass it and matches are ordered NYC first, then the rest of the US.
    """
    by_name = {c.name: c for c in companies}
    lines: list[str] = []
    lines.append(f"# Job tracker — {today}")
    lines.append("")

    counts = store.counts_by_verdict(conn)
    lines.append(
        f"_verdicts to date: {counts.get('match', 0)} match · "
        f"{counts.get('uncertain', 0)} uncertain · {counts.get('reject', 0)} reject_"
    )
    lines.append("")

    _new_matches(lines, conn, by_name, since, criteria)
    _uncertain(lines, conn, since)
    _failures(lines, conn, by_name)
    _manual(lines, conn, companies, today, mark_manual)

    return "\n".join(lines).rstrip() + "\n"


def _new_matches(lines, conn, by_name, since, criteria=None) -> None:
    rows = store.postings_with_decision(conn, "match", since)
    lines.append(f"## New matches ({len(rows)})")
    if not rows:
        lines.append("")
        lines.append(f"_None since {since}._")
        lines.append("")
        return

    by_tier: dict[object, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        company = by_name.get(r["company"])
        tier = company.tier if company and company.tier is not None else "—"
        by_tier[tier].append(r)

    for tier in sorted(by_tier, key=lambda t: (t == "—", t)):
        lines.append("")
        label = f"Tier {tier}" if tier != "—" else "Untiered"
        lines.append(f"### {label}")
        # Within a tier, NYC first, then the rest of the US, then unknown, then abroad.
        # Location never removes a row — it only decides what you read first.
        entries = by_tier[tier]
        if criteria is not None:
            entries = sorted(entries, key=lambda r: location_rank(r["location"], criteria))
        for r in entries:
            loc = f" · {r['location']}" if r["location"] else ""
            tag = ""
            if criteria is not None:
                rank = location_rank(r["location"], criteria)
                tag = "  **[NYC]**" if rank == 0 else ""
            lines.append(f"- **{r['company']}** — {r['title']}{loc}{tag}")
            lines.append(f"  {r['url']}")
    lines.append("")


def _uncertain(lines, conn, since) -> None:
    rows = store.postings_with_decision(conn, "uncertain", since)
    lines.append(f"## Uncertain — needs a human ({len(rows)})")
    lines.append("")
    if not rows:
        lines.append("_None._")
        lines.append("")
        return
    for r in rows:
        loc = f" · {r['location']}" if r["location"] else ""
        lines.append(f"- **{r['company']}** — {r['title']}{loc}  ({r['reason']})")
        lines.append(f"  {r['url']}")
    lines.append("")


def _failures(lines, conn, by_name) -> None:
    rows = store.unhealthy_boards(conn)
    lines.append(f"## Board failures ({len(rows)})")
    lines.append("")
    if not rows:
        lines.append("_All boards healthy._")
        lines.append("")
        return
    for r in rows:
        flag = " ** ALERT **" if r["alerting"] else ""
        company = by_name.get(r["company"])
        slug = f" ({company.ats}/{company.slug})" if company else ""
        lines.append(
            f"- **{r['company']}**{slug} — `{r['last_status']}`{flag}: {r['detail']}"
        )
    lines.append("")


def _manual(lines, conn, companies, today, mark_manual) -> None:
    manual = [c for c in companies if c.check_method == "manual"]
    due = [
        c for c in manual if store.manual_due(conn, c.name, today, MANUAL_INTERVAL_DAYS)
    ]
    lines.append(f"## Check by hand ({len(due)} due of {len(manual)} manual)")
    lines.append("")
    if not due:
        lines.append("_None due this week._")
        lines.append("")
        return
    for c in due:
        tier = f"T{c.tier}" if c.tier is not None else "—"
        lines.append(f"- [{tier}] **{c.name}** ({c.ats}) — {c.notes or c.category}")
        if mark_manual:
            store.mark_manual_surfaced(conn, c.name, today)
    lines.append("")
