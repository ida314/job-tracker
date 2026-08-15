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

-- Your judgments. This is the regression corpus, not a cache: `jobtracker eval`
-- replays the criteria over these to answer "what would this rule change break?"
--
-- `title` is denormalized on purpose. A posting closes and eventually gets pruned,
-- but the judgment "a title shaped like this is not what I want" stays true forever.
-- Joining to postings would silently shrink the corpus every time a req closed, which
-- is exactly when you least want to lose the evidence.
CREATE TABLE IF NOT EXISTS decisions (
    company     TEXT NOT NULL,
    ats_job_id  TEXT NOT NULL,
    title       TEXT NOT NULL,
    location    TEXT,
    decision    TEXT NOT NULL,        -- 'match' | 'reject'
    note        TEXT,
    decided_at  TEXT NOT NULL,
    PRIMARY KEY (company, ats_job_id)
);

-- Per-posting escape hatch. Survives rematch: once you have ruled on something
-- specific, no rule change should quietly re-open it.
--
-- Also the pin for the LLM pass. `check` re-records a rules verdict for *every*
-- posting it fetches, so anything decided by reading a description — which the rules
-- cannot see — is erased the next night unless it is pinned here. `decided_by` keeps
-- the two apart so a model pass can never displace a human judgment.
CREATE TABLE IF NOT EXISTS overrides (
    company     TEXT NOT NULL,
    ats_job_id  TEXT NOT NULL,
    decision    TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (company, ats_job_id)
);

-- The outer loop: which surfaced roles you actually applied to, and what came of it.
-- `title` is denormalized for the same reason as `decisions` — a req closes and gets
-- pruned, but "I applied here and got an interview" stays true and worth keeping.
-- `applied_at` is set once; `status`/`updated_at` move as the application progresses.
CREATE TABLE IF NOT EXISTS applications (
    company     TEXT NOT NULL,
    ats_job_id  TEXT NOT NULL,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL,   -- applied | interviewing | offer | rejected | withdrawn
    note        TEXT,
    applied_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (company, ats_job_id)
);

-- What the local model made of one posting, read against profile.yaml. NOT a score:
-- three labelled ordinals and a sentence. `score` next to them is computed in Python
-- from profile weights, and is recomputed on every rank run without a model call.
--
-- `prose_hash` is the identity of the question that was asked. Weights are excluded
-- from it on purpose, which is what makes retuning free — change a weight and these
-- rows still stand, so re-sorting the queue costs nothing. Change the prose and the
-- hash moves, invalidating them, because they were answers to a different question.
--
-- Deliberately NOT stored in `verdicts`: that table is PK'd one row per posting and
-- is rewritten by `check` every night, which is what erased the LLM's work before.
CREATE TABLE IF NOT EXISTS rankings (
    company      TEXT NOT NULL,
    ats_job_id   TEXT NOT NULL,
    backend_fit  TEXT NOT NULL,   -- none | weak | moderate | strong
    growth       TEXT NOT NULL,   -- none | weak | moderate | strong
    entry_risk   TEXT NOT NULL,   -- low | medium | high
    why          TEXT NOT NULL DEFAULT '',
    prose_hash   TEXT NOT NULL,
    judged_at    TEXT NOT NULL,
    score        REAL,
    scored_at    TEXT,
    PRIMARY KEY (company, ats_job_id)
);

-- "Not this one" and "not yet". A job holds its slot in the daily top 3 until you say
-- what happened to it, so that nothing you meant to apply to falls off unseen.
--
-- 'applied' is deliberately NOT a kind here — it goes to `applications`, which already
-- tracks the outcome past the click. This table is only for the two dispositions that
-- have no outcome to track.
CREATE TABLE IF NOT EXISTS deferrals (
    company     TEXT NOT NULL,
    ats_job_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,   -- skipped | snoozed
    note        TEXT,
    until       TEXT,            -- ISO date; NULL for 'skipped', which does not expire
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (company, ats_job_id)
);

-- A failure ledger for the task queue. NOT a queue: the queue is derived every run by
-- each task's own SQL over the tables above, so there is nothing here to drift out of
-- sync with what is actually pending. This exists for one purpose — to stop retrying a
-- unit that has failed the same way three nights running, which would otherwise consume
-- the whole budget forever while the rest of the queue starves.
--
-- `unit_key` identifies the *question*, not the posting: for `judge` it carries the
-- profile prose hash, for `prefill` the answers hash. Change the question and the unit
-- is new, so it is retried with a clean slate — which is correct, because a failure
-- against the old question says nothing about the new one.
CREATE TABLE IF NOT EXISTS task_attempts (
    task        TEXT NOT NULL,
    company     TEXT NOT NULL,
    ats_job_id  TEXT NOT NULL,
    unit_key    TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_status TEXT NOT NULL,   -- ok | no_answer | error
    last_error  TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (task, company, ats_job_id, unit_key)
);

-- What we know about one company's application form, learned either from an ATS that
-- publishes its questions (Greenhouse) or from the DOM on the first browser visit
-- (everything else). Keyed per company because forms are per company and stable, so one
-- visit teaches the system that employer's form permanently.
--
-- `question_key` NULL is the whole point: it means "this field was seen and we have no
-- answer for it", which is what `prefill_gaps` is generated from.
CREATE TABLE IF NOT EXISTS form_fields (
    company      TEXT NOT NULL,
    form_key     TEXT NOT NULL,   -- ATS field name, or a slug of the label for DOM finds
    label        TEXT NOT NULL,
    type         TEXT NOT NULL,
    required     INTEGER NOT NULL DEFAULT 0,
    options      TEXT,            -- JSON array of {label, value}, for selects
    question_key TEXT,            -- resolved answers.yaml key; NULL means gap
    source       TEXT NOT NULL,   -- greenhouse-api | dom
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (company, form_key)
);

-- One row per question we cannot answer, deduplicated across every company that asks
-- it. This is what the Settings page renders, and what the commented stubs appended to
-- answers.yaml mirror. `resolved_at` is set rather than the row deleted, so the history
-- of what the system had to ask you stays readable.
CREATE TABLE IF NOT EXISTS prefill_gaps (
    question_key TEXT PRIMARY KEY,
    ask          TEXT NOT NULL,   -- the question text, verbatim from the form
    type         TEXT NOT NULL,
    options      TEXT,
    seen_on      TEXT NOT NULL,   -- comma-separated company names
    first_seen   TEXT NOT NULL,
    resolved_at  TEXT
);

-- A built prefill for one posting: which value goes in which field, and how many fields
-- had no answer. `answers_hash` plays exactly the role `rankings.prose_hash` plays for
-- judging — it records which version of the question this was an answer to, so adding an
-- answer invalidates the plans that needed it and nothing else.
CREATE TABLE IF NOT EXISTS prefill_plans (
    company      TEXT NOT NULL,
    ats_job_id   TEXT NOT NULL,
    plan         TEXT NOT NULL,   -- JSON [{form_key, label, type, value, source}]
    fields       INTEGER NOT NULL DEFAULT 0,
    gaps         INTEGER NOT NULL DEFAULT 0,
    answers_hash TEXT NOT NULL,
    built_at     TEXT NOT NULL,
    PRIMARY KEY (company, ats_job_id)
);
"""

# Columns added after the initial schema shipped. CREATE TABLE IF NOT EXISTS cannot
# express these, and SQLite has no ADD COLUMN IF NOT EXISTS, so they are applied by
# inspecting the table and swallowing the duplicate-column error.
_ADDED_COLUMNS = [
    # Job descriptions, fetched lazily by the LLM pass for UNCERTAIN postings only.
    # NULL means "never fetched", which is distinct from "fetched and empty".
    ("postings", "description", "TEXT"),
    # Who set an override. Defaults to 'human' because every row that existed before
    # this column did was a human decision.
    ("overrides", "decided_by", "TEXT NOT NULL DEFAULT 'human'"),
    # The vendor's posted date, normalized to a plain ISO day. `posted_at` next door
    # holds the raw value and is three mutually incomparable formats across sources,
    # so this is the only one anything may sort on. NULL means "not normalized yet",
    # and must never be read as "posted today".
    ("postings", "posted_on", "TEXT"),
]


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _apply_column_migrations(conn)
    conn.commit()
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
            # COALESCE on posted_on: a normalizer that returns None must not erase a
            # date we already have. Greenhouse in particular only yields its true
            # `first_published` from the detail payload, so the bulk pass would
            # otherwise blank it every night.
            conn.execute(
                "UPDATE postings SET last_seen=?, closed_at=NULL, title=?, "
                "location=?, url=?, posted_at=?, posted_on=COALESCE(?, posted_on) "
                "WHERE company=? AND ats_job_id=?",
                (now, p.title, p.location, p.url, p.posted_at, p.posted_on,
                 company, p.ats_job_id),
            )
        else:
            conn.execute(
                "INSERT INTO postings (company, ats_job_id, title, location, url, "
                "posted_at, posted_on, first_seen, last_seen, closed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (company, p.ats_job_id, p.title, p.location, p.url, p.posted_at,
                 p.posted_on, now, now),
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


def backfill_posted_on(conn: sqlite3.Connection, sources: dict, today: str) -> int:
    """Normalize `posted_at` into `posted_on` for rows that predate the column.

    `sources` maps company name -> Source. Rows whose company is unknown (a company
    dropped from companies.yaml) are left alone rather than guessed at.

    No refetch is involved: the raw vendor values are already stored and already
    correct, they were merely unsortable. Idempotent and self-draining — it only ever
    looks at rows where posted_on IS NULL, so a second run is a no-op.
    """
    rows = conn.execute(
        "SELECT company, ats_job_id, posted_at FROM postings "
        "WHERE posted_on IS NULL AND posted_at IS NOT NULL"
    ).fetchall()
    filled = 0
    for row in rows:
        source = sources.get(row["company"])
        if source is None:
            continue
        day = source.normalize_posted_at(row["posted_at"], today)
        if not day:
            continue
        conn.execute(
            "UPDATE postings SET posted_on=? WHERE company=? AND ats_job_id=?",
            (day, row["company"], row["ats_job_id"]),
        )
        filled += 1
    return filled


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


def open_postings_by_verdict(conn: sqlite3.Connection, decision: str) -> list[sqlite3.Row]:
    """Every OPEN posting with this verdict, regardless of when it was first seen.

    postings_with_decision() answers the daily report's question — "what is new since
    <date>". The dashboard asks a different one: "what is the standing backlog right
    now", which includes a match first seen three weeks ago and still open. Same join,
    no date floor, newest first because recency is how you triage a backlog.
    """
    return list(
        conn.execute(
            """
            SELECT p.company, p.ats_job_id, p.title, p.location, p.url,
                   p.first_seen, v.reason
            FROM postings p JOIN verdicts v
              ON p.company=v.company AND p.ats_job_id=v.ats_job_id
            WHERE v.verdict=? AND p.closed_at IS NULL
            ORDER BY p.first_seen DESC, p.company, p.title
            """,
            (decision,),
        )
    )


def last_run(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """The most recent run row, or None if the pipeline has never run."""
    return conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()


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


# -- decisions: your judgments, kept as a regression corpus -------------------------
def record_decision(
    conn: sqlite3.Connection,
    company: str,
    ats_job_id: str,
    title: str,
    decision: str,
    now: str,
    location: str = "",
    note: str = "",
) -> None:
    """Record how *you* judged a posting. Re-judging the same posting overwrites."""
    if decision not in ("match", "reject"):
        raise ValueError(f"decision must be 'match' or 'reject', got {decision!r}")
    conn.execute(
        """
        INSERT INTO decisions
            (company, ats_job_id, title, location, decision, note, decided_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company, ats_job_id) DO UPDATE SET
            title=excluded.title, location=excluded.location,
            decision=excluded.decision, note=excluded.note,
            decided_at=excluded.decided_at
        """,
        (company, ats_job_id, title, location, decision, note, now),
    )


def all_decisions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every judgment ever recorded, newest first. The input to `eval`."""
    return list(
        conn.execute(
            "SELECT company, ats_job_id, title, location, decision, note, decided_at "
            "FROM decisions ORDER BY decided_at DESC, company, title"
        )
    )


def decision_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]


# -- applications: the outer loop (which roles you applied to, and the outcome) --------
# The lifecycle of one application. Ordered, but not enforced as a state machine — you
# can jump straight to `rejected`, and `withdrawn` is a terminal you reach from anywhere.
APPLICATION_STATUSES = ("applied", "interviewing", "offer", "rejected", "withdrawn")


def record_application(
    conn: sqlite3.Connection,
    company: str,
    ats_job_id: str,
    title: str,
    status: str,
    now: str,
    note: str = "",
) -> None:
    """Record (or advance) an application. First write sets applied_at; later writes
    move status/note/updated_at and leave applied_at untouched."""
    if status not in APPLICATION_STATUSES:
        raise ValueError(
            f"status must be one of {APPLICATION_STATUSES}, got {status!r}"
        )
    conn.execute(
        """
        INSERT INTO applications
            (company, ats_job_id, title, status, note, applied_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company, ats_job_id) DO UPDATE SET
            title=excluded.title, status=excluded.status,
            note=excluded.note, updated_at=excluded.updated_at
        """,
        (company, ats_job_id, title, status, note, now, now),
    )


def all_applications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every application, most-recently-updated first."""
    return list(
        conn.execute(
            "SELECT company, ats_job_id, title, status, note, applied_at, updated_at "
            "FROM applications ORDER BY updated_at DESC, company, title"
        )
    )


def application_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) n FROM applications").fetchone()["n"]


# -- rankings and dispositions ------------------------------------------------------
DEFERRAL_KINDS = ("skipped", "snoozed")


def record_judgment(
    conn: sqlite3.Connection,
    company: str,
    ats_job_id: str,
    judgment,
    prose_hash: str,
    now: str,
) -> None:
    """Store what the model made of one posting. Score is filled in separately."""
    conn.execute(
        """
        INSERT INTO rankings (company, ats_job_id, backend_fit, growth, entry_risk,
                              why, prose_hash, judged_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company, ats_job_id) DO UPDATE SET
            backend_fit=excluded.backend_fit, growth=excluded.growth,
            entry_risk=excluded.entry_risk, why=excluded.why,
            prose_hash=excluded.prose_hash, judged_at=excluded.judged_at
        """,
        (company, ats_job_id, judgment.backend_fit, judgment.growth,
         judgment.entry_risk, judgment.why, prose_hash, now),
    )


def set_score(
    conn: sqlite3.Connection, company: str, ats_job_id: str, score: float, now: str
) -> None:
    conn.execute(
        "UPDATE rankings SET score=?, scored_at=? WHERE company=? AND ats_job_id=?",
        (score, now, company, ats_job_id),
    )


def matches_needing_judgment(
    conn: sqlite3.Connection, prose_hash: str, limit: Optional[int] = None
) -> list[sqlite3.Row]:
    """Open matches with no judgment at the *current* prose_hash.

    A row judged under older prose is treated as unjudged rather than deleted: if the
    model is unreachable tonight, the stale judgment still orders the queue, which
    beats an empty list. Newest first, so a --limit run spends its budget on the
    postings most likely to still be open tomorrow.
    """
    sql = """
        SELECT p.company, p.ats_job_id, p.title, p.description
        FROM postings p
        JOIN verdicts v ON p.company=v.company AND p.ats_job_id=v.ats_job_id
        LEFT JOIN rankings r ON p.company=r.company AND p.ats_job_id=r.ats_job_id
        WHERE v.verdict='match' AND p.closed_at IS NULL
          AND p.description IS NOT NULL AND p.description != ''
          AND (r.prose_hash IS NULL OR r.prose_hash != ?)
        ORDER BY COALESCE(p.posted_on, p.first_seen) DESC, p.company
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql, (prose_hash,)))


def ranked_matches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every open match with its judgment and score, best first.

    Includes unjudged ones (score NULL, sorted last) on purpose. A match the model
    could not read must stay visible as a countable gap, not vanish from the totals —
    silently dropping it is indistinguishable from never having found it.
    """
    return list(conn.execute(
        """
        SELECT p.company, p.ats_job_id, p.title, p.location, p.url,
               p.posted_on, p.first_seen,
               r.backend_fit, r.growth, r.entry_risk, r.why, r.score, r.prose_hash,
               a.status AS applied_status,
               d.kind AS deferral_kind, d.until AS deferral_until
        FROM postings p
        JOIN verdicts v ON p.company=v.company AND p.ats_job_id=v.ats_job_id
        LEFT JOIN rankings r ON p.company=r.company AND p.ats_job_id=r.ats_job_id
        LEFT JOIN applications a ON p.company=a.company AND p.ats_job_id=a.ats_job_id
        LEFT JOIN deferrals d ON p.company=d.company AND p.ats_job_id=d.ats_job_id
        WHERE v.verdict='match' AND p.closed_at IS NULL
        ORDER BY r.score IS NULL, r.score DESC, p.company, p.title
        """
    ))


def set_deferral(
    conn: sqlite3.Connection,
    company: str,
    ats_job_id: str,
    kind: str,
    now: str,
    until: Optional[str] = None,
    note: str = "",
) -> None:
    if kind not in DEFERRAL_KINDS:
        raise ValueError(f"kind must be one of {DEFERRAL_KINDS}, got {kind!r}")
    if kind == "snoozed" and not until:
        raise ValueError("a snooze needs an 'until' date, or it is a skip")
    conn.execute(
        """
        INSERT INTO deferrals (company, ats_job_id, kind, note, until, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(company, ats_job_id) DO UPDATE SET
            kind=excluded.kind, note=excluded.note,
            until=excluded.until, updated_at=excluded.updated_at
        """,
        (company, ats_job_id, kind, note, until, now),
    )


def clear_deferral(conn: sqlite3.Connection, company: str, ats_job_id: str) -> None:
    conn.execute(
        "DELETE FROM deferrals WHERE company=? AND ats_job_id=?", (company, ats_job_id)
    )


# -- overrides: per-posting, survives rematch ---------------------------------------
OVERRIDE_AUTHORS = ("human", "llm")


def set_override(
    conn: sqlite3.Connection,
    company: str,
    ats_job_id: str,
    decision: str,
    now: str,
    reason: str = "",
    decided_by: str = "human",
) -> bool:
    """Pin a verdict for one posting. Returns False if an existing pin was kept.

    A machine pass must never displace a human judgment, so a non-human write over an
    existing `human` row is refused rather than merged. The reverse is allowed: you
    overruling the model is the whole point of the escape hatch.
    """
    if decision not in ("match", "reject", "uncertain"):
        raise ValueError(f"bad override decision {decision!r}")
    if decided_by not in OVERRIDE_AUTHORS:
        raise ValueError(f"bad override author {decided_by!r}")

    if decided_by != "human":
        row = conn.execute(
            "SELECT decided_by FROM overrides WHERE company=? AND ats_job_id=?",
            (company, ats_job_id),
        ).fetchone()
        if row is not None and row["decided_by"] == "human":
            return False

    conn.execute(
        """
        INSERT INTO overrides (company, ats_job_id, decision, reason, created_at, decided_by)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(company, ats_job_id) DO UPDATE SET
            decision=excluded.decision, reason=excluded.reason,
            created_at=excluded.created_at, decided_by=excluded.decided_by
        """,
        (company, ats_job_id, decision, reason, now, decided_by),
    )
    return True


def clear_override(conn: sqlite3.Connection, company: str, ats_job_id: str) -> None:
    conn.execute(
        "DELETE FROM overrides WHERE company=? AND ats_job_id=?", (company, ats_job_id)
    )


def load_overrides(conn: sqlite3.Connection) -> dict[tuple[str, str], sqlite3.Row]:
    """All overrides keyed by (company, ats_job_id).

    Loaded once per run rather than queried per posting: there are ~9k postings and
    a handful of overrides, so this is one small dict instead of 9k point lookups.
    """
    return {
        (r["company"], r["ats_job_id"]): r
        for r in conn.execute(
            "SELECT company, ats_job_id, decision, reason, decided_by FROM overrides"
        )
    }


# -- descriptions: fetched lazily, only for the UNCERTAIN residual ------------------
def postings_needing_description(
    conn: sqlite3.Connection, limit: Optional[int] = None
) -> list[sqlite3.Row]:
    """Open UNCERTAIN postings whose description has never been fetched.

    NULL means never fetched; '' means fetched and genuinely empty. Only the former
    is retried, so a description-less posting is not re-requested every night.
    """
    sql = """
        SELECT p.company, p.ats_job_id, p.title, p.url, p.location
        FROM postings p JOIN verdicts v
          ON p.company=v.company AND p.ats_job_id=v.ats_job_id
        WHERE v.verdict='uncertain' AND p.closed_at IS NULL AND p.description IS NULL
        ORDER BY p.first_seen DESC
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql))


def uncertain_for_resolution(
    conn: sqlite3.Connection, limit: Optional[int] = None
) -> list[sqlite3.Row]:
    """Open UNCERTAIN postings, with whatever description we already hold.

    Rows whose description is NULL sort **last**, and that ordering is load-bearing for
    `--limit`. `check` caches descriptions on a per-run budget, so on any night there
    are uncertain postings it has not reached yet — and since this pass no longer
    fetches, such a row is a guaranteed no-op. Ordering newest-first alone would hand a
    limited run a budget's worth of postings it cannot read: observed on live data,
    `--limit 40` considering 40 and resolving 0.

    Within that, newest first, so the budget goes to the freshest readable postings.
    """
    sql = """
        SELECT p.company, p.ats_job_id, p.title, p.location, p.description
        FROM postings p JOIN verdicts v
          ON p.company=v.company AND p.ats_job_id=v.ats_job_id
        WHERE v.verdict='uncertain' AND p.closed_at IS NULL
        ORDER BY (p.description IS NULL OR p.description = ''),
                 p.first_seen DESC, p.company
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql))


def set_description(
    conn: sqlite3.Connection, company: str, ats_job_id: str, description: str
) -> None:
    conn.execute(
        "UPDATE postings SET description=? WHERE company=? AND ats_job_id=?",
        (description, company, ats_job_id),
    )


def set_posted_on(
    conn: sqlite3.Connection, company: str, ats_job_id: str, day: str
) -> None:
    """Record a better posted date than the bulk payload could supply.

    Only Greenhouse needs this: `first_published` lives on the detail payload, which
    is fetched for the description, so the date arrives later than the posting does.
    """
    conn.execute(
        "UPDATE postings SET posted_on=? WHERE company=? AND ats_job_id=?",
        (day, company, ats_job_id),
    )


def get_description(
    conn: sqlite3.Connection, company: str, ats_job_id: str
) -> Optional[str]:
    row = conn.execute(
        "SELECT description FROM postings WHERE company=? AND ats_job_id=?",
        (company, ats_job_id),
    ).fetchone()
    return row["description"] if row else None


# -- the task ledger ---------------------------------------------------------------
TASK_STATUSES = ("ok", "no_answer", "error")


def record_attempt(
    conn: sqlite3.Connection,
    task: str,
    company: str,
    ats_job_id: str,
    unit_key: str,
    status: str,
    now: str,
    error: Optional[str] = None,
) -> None:
    """Note that a unit was tried, and how it went.

    `attempts` counts consecutive failures, so a success resets it to zero. A unit that
    works one night and fails the next has not used up two of its three lives.
    """
    if status not in TASK_STATUSES:
        raise ValueError(f"unknown task status {status!r}; expected {TASK_STATUSES}")
    attempts_expr = "0" if status == "ok" else "task_attempts.attempts + 1"
    conn.execute(
        f"""
        INSERT INTO task_attempts (task, company, ats_job_id, unit_key,
                                   attempts, last_status, last_error, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task, company, ats_job_id, unit_key) DO UPDATE SET
            attempts={attempts_expr},
            last_status=excluded.last_status,
            last_error=excluded.last_error,
            updated_at=excluded.updated_at
        """,
        (task, company, ats_job_id, unit_key,
         0 if status == "ok" else 1, status, error, now),
    )


def blocked_units(
    conn: sqlite3.Connection, task: str, max_attempts: int
) -> set[tuple[str, str, str]]:
    """Units that have failed `max_attempts` times running and are out of retries.

    Deliberately small — only failures are in this table at all — so the caller loads
    the whole set once per run and filters in Python rather than joining it into every
    task's own query.
    """
    rows = conn.execute(
        """
        SELECT company, ats_job_id, unit_key FROM task_attempts
        WHERE task=? AND attempts >= ? AND last_status != 'ok'
        """,
        (task, int(max_attempts)),
    )
    return {(r["company"], r["ats_job_id"], r["unit_key"]) for r in rows}


def task_ledger(conn: sqlite3.Connection, task: Optional[str] = None) -> list[sqlite3.Row]:
    """Every unit that has failed at least once, worst first. For reporting."""
    sql = """
        SELECT task, company, ats_job_id, unit_key, attempts,
               last_status, last_error, updated_at
        FROM task_attempts WHERE last_status != 'ok'
    """
    params: tuple = ()
    if task:
        sql += " AND task=?"
        params = (task,)
    sql += " ORDER BY attempts DESC, updated_at DESC"
    return list(conn.execute(sql, params))


# -- application forms and prefill --------------------------------------------------
def upsert_form_field(
    conn: sqlite3.Connection,
    company: str,
    form_key: str,
    label: str,
    field_type: str,
    now: str,
    required: bool = False,
    options: Optional[str] = None,
    question_key: Optional[str] = None,
    source: str = "greenhouse-api",
) -> None:
    """Record one field of one company's application form.

    `question_key` is written even when NULL — an unresolved field must be able to
    *become* unresolved again if an answer is deleted, and a COALESCE here would pin
    the first resolution forever.
    """
    conn.execute(
        """
        INSERT INTO form_fields (company, form_key, label, type, required, options,
                                 question_key, source, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company, form_key) DO UPDATE SET
            label=excluded.label, type=excluded.type, required=excluded.required,
            options=excluded.options, question_key=excluded.question_key,
            source=excluded.source, last_seen=excluded.last_seen
        """,
        (company, form_key, label, field_type, 1 if required else 0, options,
         question_key, source, now, now),
    )


def form_fields_for(conn: sqlite3.Connection, company: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM form_fields WHERE company=? ORDER BY rowid", (company,)
    ))


def known_question_keys(conn: sqlite3.Connection) -> dict[str, str]:
    """Every label we have already resolved, normalized -> question_key.

    This is the alias pass: a question one company asked, answered once, is recognized
    at every other company that phrases it the same way, with no model call.
    """
    rows = conn.execute(
        "SELECT label, question_key FROM form_fields WHERE question_key IS NOT NULL"
    )
    return {normalize_label(r["label"]): r["question_key"] for r in rows}


def normalize_label(label: str) -> str:
    """Fold a form label to a comparison key.

    Lowercase, strip anything that is not alphanumeric, collapse whitespace. Deliberately
    aggressive: "Who is your current or previous employer?" and "Who is your current or
    previous employer" must be the same question, because a trailing asterisk marking a
    field required is not a different question.
    """
    kept = [c.lower() if c.isalnum() else " " for c in label]
    return " ".join("".join(kept).split())


def record_gap(
    conn: sqlite3.Connection,
    question_key: str,
    ask: str,
    field_type: str,
    company: str,
    now: str,
    options: Optional[str] = None,
) -> bool:
    """Note a question we have no answer for. True if this is the first sighting.

    Re-seeing a gap appends the company to `seen_on` rather than creating a second row:
    the same question asked by six employers is one thing for you to answer, not six.
    """
    row = conn.execute(
        "SELECT seen_on, resolved_at FROM prefill_gaps WHERE question_key=?",
        (question_key,),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO prefill_gaps (question_key, ask, type, options, seen_on,
                                      first_seen, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (question_key, ask, field_type, options, company, now),
        )
        return True
    seen = [s for s in row["seen_on"].split(",") if s]
    if company not in seen:
        seen.append(company)
        conn.execute(
            "UPDATE prefill_gaps SET seen_on=? WHERE question_key=?",
            (",".join(seen), question_key),
        )
    return False


def open_gaps(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM prefill_gaps WHERE resolved_at IS NULL ORDER BY first_seen, question_key"
    ))


def resolve_gap(conn: sqlite3.Connection, question_key: str, now: str) -> None:
    conn.execute(
        "UPDATE prefill_gaps SET resolved_at=? WHERE question_key=?", (now, question_key)
    )


def record_plan(
    conn: sqlite3.Connection,
    company: str,
    ats_job_id: str,
    plan_json: str,
    fields: int,
    gaps: int,
    answers_hash: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO prefill_plans (company, ats_job_id, plan, fields, gaps,
                                   answers_hash, built_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company, ats_job_id) DO UPDATE SET
            plan=excluded.plan, fields=excluded.fields, gaps=excluded.gaps,
            answers_hash=excluded.answers_hash, built_at=excluded.built_at
        """,
        (company, ats_job_id, plan_json, fields, gaps, answers_hash, now),
    )


def get_plan(
    conn: sqlite3.Connection, company: str, ats_job_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM prefill_plans WHERE company=? AND ats_job_id=?",
        (company, ats_job_id),
    ).fetchone()


def plans_by_posting(conn: sqlite3.Connection) -> dict[tuple[str, str], sqlite3.Row]:
    """Every plan, keyed for a cheap lookup while rendering."""
    return {
        (r["company"], r["ats_job_id"]): r
        for r in conn.execute("SELECT * FROM prefill_plans")
    }


def matches_needing_prefill(
    conn: sqlite3.Connection,
    answers_hash: str,
    today: str,
    limit: Optional[int] = None,
) -> list[sqlite3.Row]:
    """Open, scored, undeferred matches with no current prefill plan — best first.

    Ordered by score DESC, which is the "highest-matched jobs first" the queue is meant
    to work down. Unscored matches are excluded rather than sorted last: a posting the
    ranker has not read yet has no claim to be at the front of an application queue, and
    `judge` runs ahead of this task precisely so that stays a temporary state.

    Already-applied and skipped postings are excluded here rather than filtered later —
    building a prefill for a job you have already applied to is pure waste.
    """
    sql = """
        SELECT p.company, p.ats_job_id, p.title, p.url, r.score
        FROM postings p
        JOIN verdicts v ON p.company=v.company AND p.ats_job_id=v.ats_job_id
        JOIN rankings r ON p.company=r.company AND p.ats_job_id=r.ats_job_id
        LEFT JOIN applications a ON p.company=a.company AND p.ats_job_id=a.ats_job_id
        LEFT JOIN deferrals d ON p.company=d.company AND p.ats_job_id=d.ats_job_id
        LEFT JOIN prefill_plans pp
               ON p.company=pp.company AND p.ats_job_id=pp.ats_job_id
        WHERE v.verdict='match' AND p.closed_at IS NULL
          AND r.score IS NOT NULL
          AND a.company IS NULL
          AND (d.company IS NULL
               OR (d.kind='snoozed' AND d.until IS NOT NULL AND d.until <= ?))
          AND (pp.answers_hash IS NULL OR pp.answers_hash != ?)
        ORDER BY r.score DESC, p.company
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql, (today, answers_hash)))
