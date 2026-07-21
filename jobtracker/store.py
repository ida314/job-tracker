"""SQLite persistence. Schema is DESIGN.md §5.2 plus a `runs` log and a manual-check
rate-limit table.

The load-bearing idea (DESIGN.md §5.2): postings are keyed on (company, ats_job_id), so
diffing is a set difference in SQL rather than a model comparing prose. `first_seen` is
*our* observation and is set exactly once; `closed_at` is inferred only from a HEALTHY
fetch, because absence during a failure or drift is not evidence a posting closed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from .models import BoardHealth, HealthStatus, Posting, Verdict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    company     TEXT NOT NULL,
    ats_job_id  TEXT NOT NULL,
    title       TEXT NOT NULL,
    location    TEXT,
    url         TEXT NOT NULL,
    posted_at   TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    closed_at   TEXT,
    PRIMARY KEY (company, ats_job_id)
);

CREATE TABLE IF NOT EXISTS verdicts (
    company     TEXT NOT NULL,
    ats_job_id  TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    decided_by  TEXT NOT NULL,
    decided_at  TEXT NOT NULL,
    PRIMARY KEY (company, ats_job_id)
);

CREATE TABLE IF NOT EXISTS board_health (
    company                TEXT PRIMARY KEY,
    last_status            TEXT NOT NULL,
    consecutive_empty_runs INTEGER NOT NULL DEFAULT 0,
    observed_board_name    TEXT,
    last_ok_at             TEXT,
    detail                 TEXT,
    alerting               INTEGER NOT NULL DEFAULT 0,
    updated_at             TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    companies    INTEGER,
    ok           INTEGER,
    failed       INTEGER,
    new_postings INTEGER,
    matches      INTEGER
);

CREATE TABLE IF NOT EXISTS manual_checks (
    company        TEXT PRIMARY KEY,
    last_surfaced  TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


# -- board health ------------------------------------------------------------------
def get_health(conn: sqlite3.Connection, company: str) -> Optional[BoardHealth]:
    row = conn.execute(
        "SELECT * FROM board_health WHERE company=?", (company,)
    ).fetchone()
    if row is None:
        return None
    return BoardHealth(
        company=row["company"],
        status=HealthStatus(row["last_status"]),
        consecutive_empty_runs=row["consecutive_empty_runs"],
        observed_board_name=row["observed_board_name"],
        last_ok_at=row["last_ok_at"],
        detail=row["detail"] or "",
        alerting=bool(row["alerting"]),
    )


def upsert_health(conn: sqlite3.Connection, health: BoardHealth, now: str) -> None:
    conn.execute(
        """
        INSERT INTO board_health
            (company, last_status, consecutive_empty_runs, observed_board_name,
             last_ok_at, detail, alerting, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company) DO UPDATE SET
            last_status=excluded.last_status,
            consecutive_empty_runs=excluded.consecutive_empty_runs,
            observed_board_name=excluded.observed_board_name,
            last_ok_at=excluded.last_ok_at,
            detail=excluded.detail,
            alerting=excluded.alerting,
            updated_at=excluded.updated_at
        """,
        (
            health.company,
            health.status.value,
            health.consecutive_empty_runs,
            health.observed_board_name,
            health.last_ok_at,
            health.detail,
            int(health.alerting),
            now,
        ),
    )


def ever_nonempty(conn: sqlite3.Connection, company: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM postings WHERE company=? LIMIT 1", (company,)
    ).fetchone()
    return row is not None


# -- postings ----------------------------------------------------------------------
def sync_postings(
    conn: sqlite3.Connection, company: str, postings: list[Posting], now: str
) -> tuple[list[Posting], list[str]]:
    """Reconcile a HEALTHY fetch against stored state.

    Returns (new_postings, closed_ids). New postings get first_seen=now. Previously
    stored postings get last_seen bumped and are reopened if they had been closed.
    Postings absent from this fetch are marked closed. Call ONLY for OK boards.
    """
    existing = {
        row["ats_job_id"]: row
        for row in conn.execute(
            "SELECT ats_job_id, closed_at FROM postings WHERE company=?", (company,)
        )
    }
    current_ids: set[str] = set()
    new_postings: list[Posting] = []

    for p in postings:
        current_ids.add(p.ats_job_id)
        if p.ats_job_id in existing:
            conn.execute(
                "UPDATE postings SET last_seen=?, closed_at=NULL, title=?, "
                "location=?, url=?, posted_at=? WHERE company=? AND ats_job_id=?",
                (now, p.title, p.location, p.url, p.posted_at, company, p.ats_job_id),
            )
        else:
            conn.execute(
                "INSERT INTO postings (company, ats_job_id, title, location, url, "
                "posted_at, first_seen, last_seen, closed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (company, p.ats_job_id, p.title, p.location, p.url, p.posted_at, now, now),
            )
            new_postings.append(p)

    closed_ids = [
        jid
        for jid, row in existing.items()
        if jid not in current_ids and row["closed_at"] is None
    ]
    for jid in closed_ids:
        conn.execute(
            "UPDATE postings SET closed_at=? WHERE company=? AND ats_job_id=?",
            (now, company, jid),
        )
    return new_postings, closed_ids


def record_verdict(conn: sqlite3.Connection, verdict: Verdict, now: str) -> None:
    conn.execute(
        """
        INSERT INTO verdicts (company, ats_job_id, verdict, reason, decided_by, decided_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(company, ats_job_id) DO UPDATE SET
            verdict=excluded.verdict, reason=excluded.reason,
            decided_by=excluded.decided_by, decided_at=excluded.decided_at
        """,
        (
            verdict.company,
            verdict.ats_job_id,
            verdict.decision.value,
            verdict.reason,
            verdict.decided_by,
            now,
        ),
    )


# -- runs --------------------------------------------------------------------------
def record_run(conn: sqlite3.Connection, started: str, finished: str, stats: dict) -> None:
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, companies, ok, failed, "
        "new_postings, matches) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            started,
            finished,
            stats.get("companies", 0),
            stats.get("ok", 0),
            stats.get("failed", 0),
            stats.get("new_postings", 0),
            stats.get("matches", 0),
        ),
    )


# -- manual-check rate limiting ----------------------------------------------------
def manual_due(conn: sqlite3.Connection, company: str, today: str, interval_days: int) -> bool:
    row = conn.execute(
        "SELECT last_surfaced FROM manual_checks WHERE company=?", (company,)
    ).fetchone()
    if row is None or row["last_surfaced"] is None:
        return True
    return _days_between(row["last_surfaced"], today) >= interval_days


def mark_manual_surfaced(conn: sqlite3.Connection, company: str, today: str) -> None:
    conn.execute(
        "INSERT INTO manual_checks (company, last_surfaced) VALUES (?, ?) "
        "ON CONFLICT(company) DO UPDATE SET last_surfaced=excluded.last_surfaced",
        (company, today),
    )


# -- report queries ----------------------------------------------------------------
def postings_with_decision(
    conn: sqlite3.Connection, decision: str, since: str
) -> list[sqlite3.Row]:
    """Open postings first seen on/after `since` with the given verdict."""
    return list(
        conn.execute(
            """
            SELECT p.company, p.ats_job_id, p.title, p.location, p.url,
                   p.first_seen, v.reason
            FROM postings p JOIN verdicts v
              ON p.company=v.company AND p.ats_job_id=v.ats_job_id
            WHERE v.verdict=? AND p.first_seen>=? AND p.closed_at IS NULL
            ORDER BY p.company, p.title
            """,
            (decision, since),
        )
    )


def unhealthy_boards(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT company, last_status, detail, alerting FROM board_health "
            "WHERE last_status != 'ok' ORDER BY alerting DESC, company"
        )
    )


def counts_by_verdict(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row["verdict"]: row["n"]
        for row in conn.execute(
            "SELECT verdict, COUNT(*) n FROM verdicts GROUP BY verdict"
        )
    }


def _days_between(a: str, b: str) -> int:
    from datetime import date

    return abs((date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days)
