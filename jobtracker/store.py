"""SQLite persistence. Schema is DESIGN.md §5.2 plus a `runs` log and a manual-check
rate-limit table.

The load-bearing idea (DESIGN.md §5.2): postings are keyed on (company, ats_job_id), so
diffing is a set difference in SQL rather than a model comparing prose. `first_seen` is
*our* observation and is set exactly once; `closed_at` is inferred only from a HEALTHY
fetch, because absence during a failure or drift is not evidence a posting closed.
"""

from __future__ import annotations

import hashlib
import json
import re
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
--
-- `url` and `location` are denormalized for a second reason on top of that one: a
-- manually-entered application has no `postings` row at all, so there is nothing to
-- join to. See `manual_job_id` for how those rows are keyed. `url`, `location`,
-- `source`, `next_action` and `next_action_note` arrive via _ADDED_COLUMNS below.
CREATE TABLE IF NOT EXISTS applications (
    company     TEXT NOT NULL,
    ats_job_id  TEXT NOT NULL,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL,   -- see APPLICATION_STATUSES
    note        TEXT,
    applied_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (company, ats_job_id)
);

-- Every stage an application has passed through, in order. `applications.status` is
-- the current-state cache; this is how it got there.
--
-- Deliberately no primary key. It is a log, and (company, ats_job_id, status) really
-- does repeat — that is the whole `interview ×3` case, one row per round. The implicit
-- rowid is the tiebreaker for two events stamped on the same day.
--
-- Also deliberately no index: this table has no indexes anywhere in the schema to be
-- consistent with, it is read once per page render, and a job search is a few hundred
-- rows. Do not add one without a measurement.
CREATE TABLE IF NOT EXISTS application_events (
    company     TEXT NOT NULL,
    ats_job_id  TEXT NOT NULL,
    status      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL
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

-- A resume for ONE posting, overriding the answer bank's. OBSERVATION, not curation: it
-- is a fact about one application, so it lives here and never in answers.yaml, whose
-- `resume:` key is hand-written and is the default for everything.
--
-- `filename` is a name this repo minted (resumes.stored_name), never the client's string.
-- A filename is attacker-shaped input even when the attacker is you with a badly named
-- file, and this one names a file that gets attached to a real job application.
--
-- A row whose file has gone missing reads as "no override" and logs — never as an
-- exception raised with a browser sitting on an open form, which is the worst possible
-- moment to discover a missing resume (docs/prefill.md says the same about the bank's).
CREATE TABLE IF NOT EXISTS posting_resumes (
    company     TEXT NOT NULL,
    ats_job_id  TEXT NOT NULL,
    filename    TEXT NOT NULL,
    bytes       INTEGER NOT NULL DEFAULT 0,
    uploaded_at TEXT NOT NULL,
    PRIMARY KEY (company, ats_job_id)
);

-- Mail the deterministic narrower tied to a row in `applications`. Written only by
-- `jobtracker mail` — never by a task, never by the web server. Reading a mailbox is I/O,
-- and the rule that made `check` cache descriptions applies unchanged: the `inbox` task
-- must be a pure read of state.db, so an unmounted maildir cannot shrink a queue that was
-- already recorded, and a task never opens a file.
--
-- Keyed on `message_id`, the mail system's own stable identity. The maildir FILENAME is
-- not that: a client renames `1234.host` to `1234.host:2,S` the moment you read the
-- message, so keying on it would re-propose the whole inbox every time you opened your
-- mail. A message with no Message-ID gets 'synth:' + a digest of From+Date+Subject+size,
-- the same fallback `manual_job_id` uses for a title that slugs to nothing.
--
-- `application_events` has no unique constraint and no dedup by design — repeats are the
-- point there — so anything ingesting an external stream nightly has to carry its own
-- idempotency key. This primary key is that key, and it is the only thing standing
-- between a nightly scan and seven identical `rejected` events.
--
-- `sent_at` is the raw Date header and `sent_on` is it normalized to a plain ISO day.
-- Same split, and same reason, as `postings.posted_at` / `posted_on`: Date headers arrive
-- in a dozen timezone spellings and collate wrongly as text, so nothing may sort on the
-- raw one.
--
-- `body` is truncated to mail.MAX_MAIL_CHARS and is the model's entire input, cached for
-- the same reason `postings.description` is. Consequence worth stating out loud: state.db
-- now holds the text of personal mail. It is gitignored, it lives in ./data, nothing
-- ships it anywhere, and no log line or span attribute may carry a subject or a body.
--
-- `read_at` NULL IS the queue — "the model has not been asked what this is". Non-NULL
-- with no `mail_proposals` row means "asked, and it is not application news": exactly the
-- NULL-vs-'' distinction `postings.description` draws between never-fetched and
-- fetched-and-genuinely-empty. It is never cleared.
--
-- Messages the narrower REJECTED are deliberately not stored. Re-narrowing is free, local
-- and deterministic, and storing rejections would freeze them — not storing is what lets
-- a message that arrived BEFORE you recorded the application become a candidate on the
-- very next scan.
CREATE TABLE IF NOT EXISTS mail_candidates (
    message_id  TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    ats_job_id  TEXT NOT NULL DEFAULT '',  -- '' = company matched, no single job did
    choices     TEXT NOT NULL DEFAULT '',  -- JSON [ats_job_id, ...] the model may point at
    match_kind  TEXT NOT NULL,             -- job_url|job_id|title|sole_open|company_domain|company_name
    evidence    TEXT NOT NULL DEFAULT '',  -- the literal text that matched, for the reviewer
    from_addr   TEXT NOT NULL DEFAULT '',
    from_name   TEXT NOT NULL DEFAULT '',
    subject     TEXT NOT NULL DEFAULT '',
    sent_at     TEXT,
    sent_on     TEXT,
    body        TEXT NOT NULL DEFAULT '',
    snippet     TEXT NOT NULL DEFAULT '',
    scanned_at  TEXT NOT NULL,
    read_at     TEXT
);

-- What the model made of one message, and what you did about it. A PROPOSAL, never a
-- decision: nothing in this subsystem calls advance_application except the accept
-- endpoint and `jobtracker mail --accept`, and there is a test asserting a full task run
-- leaves `applications` and `application_events` untouched.
--
-- `resolution` is set, never deleted, and a dismissed row stays dismissed forever — the
-- scanner skips any message_id already in `mail_candidates`, so a rejection you have
-- already waved off cannot return next Tuesday. Deleting the row is what would let it.
--
-- `evidence` is the model's quote, and it is the ONLY free text the model produces here.
-- It is shown on the review card and never written into an application: the event note is
-- composed by Python. DESIGN.md §8.4's rule that no sentence the model composed reaches a
-- field, with your own application history as the field.
CREATE TABLE IF NOT EXISTS mail_proposals (
    message_id   TEXT PRIMARY KEY,
    company      TEXT NOT NULL,
    ats_job_id   TEXT NOT NULL DEFAULT '',  -- '' = you still have to say which job
    status       TEXT NOT NULL,             -- one of APPLICATION_STATUSES
    evidence     TEXT NOT NULL DEFAULT '',
    resolution   TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | dismissed
    proposed_at  TEXT NOT NULL,
    resolved_at  TEXT
);

-- Verified slug repairs awaiting a human (DESIGN.md §8.2). OBSERVATION, not curation:
-- a proposal is something the system noticed about a board, so it lives here and never
-- in companies.yaml, which no scheduled run may write (§2.3, principle 3).
--
-- Deliberately NOT a column on board_health. `check` rewrites every board_health row
-- nightly, which is the same mechanism that erased the LLM's verdicts before `rankings`
-- was given its own table. A proposal is a durable claim, not this run's status.
--
-- Keyed on company alone: one open proposal per board. A board broken for a week
-- re-derives the same conclusion every night and updates this row, so the report shows
-- one line rather than seven.
--
-- `verified_at` is the load-bearing column. A row exists here only because an actual
-- API fetch of the proposed slug returned postings AND passed the identity check for
-- its ATS. That is "slugs are verified, never guessed" (CLAUDE.md) in executable form,
-- and it is why `board_name`, `job_count` and `sample_titles` are stored rather than
-- recomputed on read: they are the evidence the human is reviewing, as of the moment
-- the claim was made.
--
-- `evidence_kind` records WHICH kind of evidence backed the claim, because the two are
-- not equally strong. 'identity' means the ATS has a dedicated identity endpoint
-- (Greenhouse `.name`) whose answer comes from a different endpoint than the slug, so
-- agreement is real. 'provenance' means identity was derived from a job URL (Ashby,
-- Lever), where for a candidate slug it merely restates that slug — see repair.py. A
-- 'provenance' row is a weaker claim and is flagged as such everywhere it is shown.
CREATE TABLE IF NOT EXISTS repair_proposals (
    company        TEXT PRIMARY KEY,
    from_ats       TEXT NOT NULL,
    from_slug      TEXT NOT NULL,
    to_ats         TEXT NOT NULL,
    to_slug        TEXT NOT NULL,
    board_name     TEXT,
    job_count      INTEGER NOT NULL DEFAULT 0,
    sample_titles  TEXT NOT NULL DEFAULT '',
    evidence_kind  TEXT NOT NULL,          -- identity | provenance
    found_by       TEXT NOT NULL,          -- regex | llm
    evidence       TEXT NOT NULL DEFAULT '',
    trigger_status TEXT NOT NULL,
    verified_at    TEXT NOT NULL,
    applied_at     TEXT                    -- NULL = still awaiting a human
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
    # Consecutive failed *fetches*, the counter that makes "persistent FETCH_FAILED"
    # expressible for the slug-repair detector. `consecutive_empty_runs` next door was
    # never a substitute — it counts a board that answers with nothing, not one that
    # does not answer — and `last_ok_at` is not either: it is NULL forever for a board
    # that has never been healthy, so "failed for three nights" and "failed since day
    # one" read identically. Reset by every non-failing outcome, so it is a streak.
    ("board_health", "consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
    # Where the application can be reopened, and where the job is. Denormalized off
    # `postings` rather than joined because a manual entry has no posting row, and
    # because a tracked one loses its row when the req closes — which is exactly when
    # "where did I apply?" is still worth answering.
    ("applications", "url", "TEXT"),
    ("applications", "location", "TEXT"),
    # 'tracked' (surfaced by the pipeline) | 'manual' (typed in by hand). Stored rather
    # than sniffed off the `manual:` id prefix: the prefix is how the row is *keyed*,
    # and reading a key as a flag couples two things that can drift.
    #
    # Nullable, and read through COALESCE(source, 'tracked') instead of carrying a NOT
    # NULL DEFAULT. A column default only fires when the INSERT omits the column, and
    # this one is always bound — so `NOT NULL DEFAULT 'tracked'` rejected every caller
    # that passed None rather than defaulting it. NULL reading as 'tracked' is also
    # exactly right for rows written before manual entry existed: they all came from
    # the pipeline.
    ("applications", "source", "TEXT"),
    # The next thing you owe this application, as an ISO day, plus what it is. Not an
    # apply-by deadline: every row in this table exists because you already applied, so
    # a closing date has nothing left to inform. NULL means nothing is scheduled, which
    # is the normal state and never means "overdue".
    ("applications", "next_action", "TEXT"),
    ("applications", "next_action_note", "TEXT"),
    # Which resume this plan was built with: a `posting_resumes.filename`, or NULL for
    # the answer bank's. It is a second column rather than a component of `answers_hash`
    # because they answer two different questions — the hash covers the bank, this covers
    # one posting. Folding the override into the hash would make every plan built with one
    # look permanently stale, and `matches_needing_prefill` would rebuild it every night
    # forever.
    ("prefill_plans", "resume_key", "TEXT"),
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
        consecutive_failures=row["consecutive_failures"] or 0,
    )


def upsert_health(conn: sqlite3.Connection, health: BoardHealth, now: str) -> None:
    conn.execute(
        """
        INSERT INTO board_health
            (company, last_status, consecutive_empty_runs, observed_board_name,
             last_ok_at, detail, alerting, updated_at, consecutive_failures)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company) DO UPDATE SET
            last_status=excluded.last_status,
            consecutive_empty_runs=excluded.consecutive_empty_runs,
            observed_board_name=excluded.observed_board_name,
            last_ok_at=excluded.last_ok_at,
            detail=excluded.detail,
            alerting=excluded.alerting,
            updated_at=excluded.updated_at,
            -- Must be in the UPDATE list too, not just the INSERT. Omitting it would
            -- leave a repaired board carrying its old failure streak forever, and the
            -- repair detector would keep firing on a board that is now healthy.
            consecutive_failures=excluded.consecutive_failures
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
            health.consecutive_failures,
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


def unhealthy_health(conn: sqlite3.Connection) -> list[BoardHealth]:
    """Every non-OK board as a typed BoardHealth. The repair detector's input.

    Separate from `unhealthy_boards` rather than a widening of it. That query is
    consumed by report.py and dashboard.py by column name, and the detector needs
    fields (`consecutive_failures`) those two do not — so it gets its own reader
    instead of a shared SELECT that grows a column every time someone needs one.
    """
    rows = conn.execute(
        "SELECT * FROM board_health WHERE last_status != 'ok' ORDER BY company"
    )
    return [
        BoardHealth(
            company=r["company"],
            status=HealthStatus(r["last_status"]),
            consecutive_empty_runs=r["consecutive_empty_runs"],
            observed_board_name=r["observed_board_name"],
            last_ok_at=r["last_ok_at"],
            detail=r["detail"] or "",
            alerting=bool(r["alerting"]),
            consecutive_failures=r["consecutive_failures"] or 0,
        )
        for r in rows
    ]


# -- slug repair proposals -----------------------------------------------------------
def record_proposal(conn: sqlite3.Connection, proposal, now: str) -> None:
    """Store a verified repair proposal, replacing any earlier one for that board.

    Re-proposing resets `applied_at` to NULL: a board that is broken again after a
    previous repair was applied has a new, unreviewed claim against it, and carrying
    the old timestamp forward would hide it from `open_proposals`.
    """
    conn.execute(
        """
        INSERT INTO repair_proposals
            (company, from_ats, from_slug, to_ats, to_slug, board_name, job_count,
             sample_titles, evidence_kind, found_by, evidence, trigger_status,
             verified_at, applied_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(company) DO UPDATE SET
            from_ats=excluded.from_ats,
            from_slug=excluded.from_slug,
            to_ats=excluded.to_ats,
            to_slug=excluded.to_slug,
            board_name=excluded.board_name,
            job_count=excluded.job_count,
            sample_titles=excluded.sample_titles,
            evidence_kind=excluded.evidence_kind,
            found_by=excluded.found_by,
            evidence=excluded.evidence,
            trigger_status=excluded.trigger_status,
            verified_at=excluded.verified_at,
            applied_at=NULL
        """,
        (
            proposal.company,
            proposal.from_ats,
            proposal.from_slug,
            proposal.to_ats,
            proposal.to_slug,
            proposal.board_name,
            proposal.job_count,
            " · ".join(proposal.sample_titles),
            proposal.evidence_kind,
            proposal.found_by,
            proposal.evidence,
            proposal.trigger,
            now,
        ),
    )


def open_proposals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Proposals still awaiting a human. Applied ones are history, not a queue."""
    return list(
        conn.execute(
            "SELECT * FROM repair_proposals WHERE applied_at IS NULL ORDER BY company"
        )
    )


def mark_proposal_applied(conn: sqlite3.Connection, company: str, now: str) -> None:
    conn.execute(
        "UPDATE repair_proposals SET applied_at=? WHERE company=?", (now, company)
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
# The lifecycle of one application, in funnel order. Not enforced as a state machine —
# you can jump straight to `rejected`, and `withdrawn` is a terminal you reach from
# anywhere.
#
# `interview` is deliberately ONE status and is repeatable rather than being split into
# round_1/round_2/onsite. A numbered enum is capped at however many rounds you guessed,
# and a fourth round has nowhere to go; a repeated event with a free-text note says
# "round 2 — system design" without a schema change. Counting the events is what
# renders as `interview ×3`.
#
# There is no `ghosted`. Silence is derived from `updated_at` (see `is_stale`) because
# a status only you can set is one you will not remember to set, and an unset status
# that means "no reply" is indistinguishable from an application going well.
APPLICATION_STATUSES = (
    "applied", "oa", "screen", "interview", "offer", "rejected", "withdrawn",
)
# Still in play — these are what "active" means on the page and in the counts.
ACTIVE_STATUSES = ("applied", "oa", "screen", "interview")
# Terminal. `offer` sits here because the application is over, not because it went badly.
CLOSED_STATUSES = ("offer", "rejected", "withdrawn")

# An active application nobody has touched in this many days is shown as stale. Not a
# status and not stored — see the note above APPLICATION_STATUSES.
STALE_AFTER_DAYS = 30

# Manual entries have no ATS and therefore no vendor id, so one is minted. The prefix
# guarantees a hand-typed row can never collide with a real (company, ats_job_id) even
# if that company is later added to companies.yaml and fetched for real.
MANUAL_PREFIX = "manual:"


def manual_job_id(title: str) -> str:
    """Mint a stable id for a hand-entered application.

    Deterministic on the title, so re-adding the same role at the same company updates
    the row you already have instead of silently creating a second one — the same
    stable-id rule the aggregator adapter follows for postings it has no vendor id for.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    # A title of pure punctuation would slug to nothing, which would make every such
    # entry at one company the same row. Fall back to a digest of the raw text.
    if not slug:
        slug = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
    return MANUAL_PREFIX + slug[:80]


def is_manual(ats_job_id: str) -> bool:
    return ats_job_id.startswith(MANUAL_PREFIX)


def record_application(
    conn: sqlite3.Connection,
    company: str,
    ats_job_id: str,
    title: str,
    status: str,
    now: str,
    note: str = "",
    *,
    url: Optional[str] = None,
    location: Optional[str] = None,
    source: Optional[str] = None,
    next_action: Optional[str] = None,
    next_action_note: Optional[str] = None,
) -> None:
    """Set an application's current state. First write sets applied_at; later writes
    move status/note/updated_at and leave applied_at untouched.

    Logs nothing. Callers that mean "something happened to this application" want
    `advance_application`, which also appends to the event log. The split is what makes
    a repeated `interview` legible: folding the append in here would either duplicate an
    event every time a note was edited, or suppress the second interview round — and
    from inside an upsert those two cases look identical.

    Every optional field updates through COALESCE, so passing None means "leave it
    alone" rather than "clear it". Without that, `jobtracker apply`, which passes none
    of them, would blank a URL set from the web page on the next status change. Same
    rule as `sync_postings` writing `posted_on`. To actually clear a field, pass "".
    """
    if status not in APPLICATION_STATUSES:
        raise ValueError(
            f"status must be one of {APPLICATION_STATUSES}, got {status!r}"
        )
    conn.execute(
        """
        INSERT INTO applications
            (company, ats_job_id, title, status, note, applied_at, updated_at,
             url, location, source, next_action, next_action_note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company, ats_job_id) DO UPDATE SET
            title=excluded.title, status=excluded.status,
            note=excluded.note, updated_at=excluded.updated_at,
            url=COALESCE(excluded.url, applications.url),
            location=COALESCE(excluded.location, applications.location),
            source=COALESCE(excluded.source, applications.source),
            next_action=COALESCE(excluded.next_action, applications.next_action),
            next_action_note=COALESCE(excluded.next_action_note,
                                      applications.next_action_note)
        """,
        (company, ats_job_id, title, status, note, now, now,
         url, location, source, next_action, next_action_note),
    )


def add_application_event(
    conn: sqlite3.Connection,
    company: str,
    ats_job_id: str,
    status: str,
    at: str,
    note: str = "",
) -> None:
    """Append one stage to an application's history. Never updates, never deduplicates —
    three `interview` events in a row is the point, not a bug."""
    if status not in APPLICATION_STATUSES:
        raise ValueError(
            f"status must be one of {APPLICATION_STATUSES}, got {status!r}"
        )
    conn.execute(
        "INSERT INTO application_events (company, ats_job_id, status, note, at) "
        "VALUES (?, ?, ?, ?, ?)",
        # Coerced rather than left to the caller: `note` reaches here from argparse
        # defaults and JSON payloads alike, and several of those are None. The column is
        # NOT NULL because "no note" is the empty string, not an unknown.
        (company, ats_job_id, status, note or "", at),
    )


def advance_application(
    conn: sqlite3.Connection,
    company: str,
    ats_job_id: str,
    title: str,
    status: str,
    now: str,
    note: str = "",
    **meta,
) -> None:
    """Record that something happened: set the current state *and* log the event.

    This is what every caller who means "I applied" / "they moved me to an OA" / "that
    was round 2" should use. Editing a reminder is not something happening — that calls
    `record_application` alone.
    """
    record_application(conn, company, ats_job_id, title, status, now, note, **meta)
    add_application_event(conn, company, ats_job_id, status, now, note)


_APPLICATION_COLUMNS = (
    "company, ats_job_id, title, status, note, applied_at, updated_at, "
    "url, location, COALESCE(source, 'tracked') AS source, "
    "next_action, next_action_note"
)


def all_applications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every application, most-recently-updated first.

    Reads `applications` directly rather than joining through `postings`, which is what
    lets a manual entry — a job with no board, no verdict and no posting row — appear at
    all. Ordering here is only a stable default; the page re-groups by urgency.
    """
    return list(
        conn.execute(
            f"SELECT {_APPLICATION_COLUMNS} "
            "FROM applications ORDER BY updated_at DESC, company, title"
        )
    )


def get_application(
    conn: sqlite3.Connection, company: str, ats_job_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        f"SELECT {_APPLICATION_COLUMNS} "
        "FROM applications WHERE company=? AND ats_job_id=?",
        (company, ats_job_id),
    ).fetchone()


def events_by_application(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], list[sqlite3.Row]]:
    """Every event, keyed by the application it belongs to, oldest first.

    One query returning a keyed dict — same idiom as `load_overrides` and
    `plans_by_posting` — so a page with N applications renders in two queries and not
    N+1. `rowid` breaks the tie when two events share a day.
    """
    out: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT company, ats_job_id, status, note, at FROM application_events "
        "ORDER BY at, rowid"
    ):
        out.setdefault((row["company"], row["ats_job_id"]), []).append(row)
    return out


def delete_application(
    conn: sqlite3.Connection, company: str, ats_job_id: str
) -> None:
    """Remove an application and its whole history — for a mistyped manual entry.

    Deletes the events too. Leaving them orphaned would make the row reappear with a
    history the moment the same title was entered again, since `manual_job_id` is
    deterministic and would mint the identical key.
    """
    conn.execute(
        "DELETE FROM applications WHERE company=? AND ats_job_id=?",
        (company, ats_job_id),
    )
    conn.execute(
        "DELETE FROM application_events WHERE company=? AND ats_job_id=?",
        (company, ats_job_id),
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

    `options` goes the other way, and the asymmetry is the point. A DOM reading cannot
    see a combobox's options at all — react-select renders its menu on demand — while
    Greenhouse's API publishes every one of them. Overwriting with NULL meant a browser
    visit erased what the API had taught, and the field went back to being a text box
    with no vocabulary for `match_option` to check an answer against. A pass that does
    not know a value must not erase one that does; same rule as `sync_postings` and
    `posted_on`.
    """
    conn.execute(
        """
        INSERT INTO form_fields (company, form_key, label, type, required, options,
                                 question_key, source, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company, form_key) DO UPDATE SET
            label=excluded.label, type=excluded.type, required=excluded.required,
            options=COALESCE(excluded.options, form_fields.options),
            question_key=excluded.question_key,
            source=excluded.source, last_seen=excluded.last_seen
        """,
        (company, form_key, label, field_type, 1 if required else 0, options,
         question_key, source, now, now),
    )


def form_fields_for(conn: sqlite3.Connection, company: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM form_fields WHERE company=? ORDER BY rowid", (company,)
    ))


def known_options(conn: sqlite3.Connection, company: str) -> dict[str, list]:
    """Every option list this company's form has ever published, by form key.

    Read back into a DOM reading by `browser._with_known_options`, because a combobox
    never carries its own. The keys agree across the two sources: Greenhouse's API names
    a question `question_65614029` and the rendered input carries that as its `id`.
    """
    out: dict[str, list] = {}
    for row in conn.execute(
        "SELECT form_key, options FROM form_fields "
        "WHERE company=? AND options IS NOT NULL",
        (company,),
    ):
        try:
            values = json.loads(row["options"])
        except (ValueError, TypeError):
            continue
        if isinstance(values, list) and values:
            out[row["form_key"]] = [str(v) for v in values]
    return out


def forget_learned(
    conn: sqlite3.Connection, derivable, write: bool = False
) -> list[dict]:
    """Un-learn every resolved label that no rule and no human alias can account for.

    `forget_question` one label at a time; this is the sweep, and it exists because of
    what was found when the model pass was removed on 2026-08-25. Deleting the code that
    made those matches removes nothing: `apply` wrote each one onto
    `form_fields.question_key`, and `known_question_keys` replays it as a deterministic,
    model-free alias at every company forever. 229 of 383 resolved rows in the live
    database were explicable only that way, and they included *"Protected Veteran
    Status"* pointing at `are_you_a_current_mongodb_employee` and every *"do you require
    sponsorship?"* pointing at a work-authorization answer, whose stored value means the
    opposite. Removing the model while keeping those would have frozen the worst reading
    in place and taken away the only thing that could have changed its mind.

    `derivable(label, form_key) -> bool` decides what to keep, and it is supplied by the
    caller rather than written here: it is a question about `prefill`'s rules and the
    user's own aliases, and `store` may not import either. Kept means "some rule or some
    alias a person wrote produces this key" — everything else goes.

    Also the tool for undoing a bad *human* alias in bulk, which is the only kind left.
    Dry by default; the same three tables move together as in `forget_question`.
    """
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT company, form_key, label, question_key FROM form_fields "
            "WHERE question_key IS NOT NULL"
        )
        if not derivable(r["label"], r["form_key"])
    ]
    if not rows or not write:
        return rows

    for row in rows:
        conn.execute(
            "UPDATE form_fields SET question_key=NULL WHERE company=? AND form_key=?",
            (row["company"], row["form_key"]),
        )
    for company in {r["company"] for r in rows}:
        conn.execute(
            "UPDATE prefill_plans SET answers_hash='' WHERE company=?", (company,)
        )
    # Reopened by the question, never by the key it was pointed at — `forget_question`'s
    # rule, and for its reason: a gap is filed under a slug of the question text, so
    # clearing by key would reopen whatever legitimately resolved to that key and leave
    # the bad question closed.
    wanted = {normalize_label(r["label"]) for r in rows}
    for gap in conn.execute("SELECT question_key, ask FROM prefill_gaps").fetchall():
        if normalize_label(gap["ask"]) in wanted:
            conn.execute(
                "UPDATE prefill_gaps SET resolved_at=NULL WHERE question_key=?",
                (gap["question_key"],),
            )
    conn.commit()
    return rows


def forget_question(
    conn: sqlite3.Connection, target: str, write: bool = False
) -> list[dict]:
    """Un-learn the answer key a label was resolved to. Returns what it did, or would do.

    `known_question_keys` replays every resolved label as a *deterministic* alias at
    every company, so one bad match becomes permanent and model-free: "Country*" pointed
    at `location` on three boards, which typed a city into a phone country selector, and
    nothing would ever reconsider it. This is the way back out.

    Three things move together, and leaving any of them behind would only look fixed:
    the `form_fields.question_key` that teaches the alias, the `prefill_gaps` row that
    was closed when the match was believed, and the `prefill_plans` rows that already
    hold the value — a stored plan beats a fresh `resolve_field` in `browser._plan_index`,
    so a plan built with the bad answer keeps carrying it. Blanking `answers_hash` puts
    those postings back in `matches_needing_prefill`.

    Dry by default. The write is `repair`'s shape and for its reason: this rewrites
    something a run decided, and the diff is worth reading first.
    """
    wanted = normalize_label(target)
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT company, form_key, label, question_key FROM form_fields "
            "WHERE question_key IS NOT NULL"
        )
        if normalize_label(r["label"]) == wanted or r["question_key"] == target
    ]
    if not rows or not write:
        return rows

    for row in rows:
        conn.execute(
            "UPDATE form_fields SET question_key=NULL WHERE company=? AND form_key=?",
            (row["company"], row["form_key"]),
        )
    for company in {r["company"] for r in rows}:
        conn.execute(
            "UPDATE prefill_plans SET answers_hash='' WHERE company=?", (company,)
        )
    # Reopened by the *question*, not by the answer key it was pointed at. A gap is
    # recorded under a slug of the question text, so clearing `location`'s row would
    # reopen whatever legitimately resolved to `location` and leave "Country*" closed —
    # exactly backwards.
    for gap in conn.execute("SELECT question_key, ask FROM prefill_gaps").fetchall():
        if normalize_label(gap["ask"]) == wanted:
            conn.execute(
                "UPDATE prefill_gaps SET resolved_at=NULL WHERE question_key=?",
                (gap["question_key"],),
            )
    conn.commit()
    return rows


def known_question_keys(conn: sqlite3.Connection) -> dict[str, str]:
    """Every label we have already resolved, normalized -> question_key.

    This is the alias pass: a question one company asked, answered once, is recognized
    at every other company that phrases it the same way, with no model call.
    """
    rows = conn.execute(
        "SELECT label, question_key FROM form_fields WHERE question_key IS NOT NULL"
    )
    return {normalize_label(r["label"]): r["question_key"] for r in rows}


def learn_question_key(
    conn: sqlite3.Connection, label: str, question_key: str
) -> int:
    """Teach every form that asks `label` that the answer is `question_key`.

    The write behind "this question is my `email`" on `/apply`. For an answer in the
    `answers:` block the durable record is the alias list in answers.yaml — the user's
    own file, which `Answers.by_alias` reads — and this would be redundant. For an
    **identity** key it is the only record there can be: `by_alias` is built from the
    `answers:` block alone, so a wording attached to `first_name` has nowhere else to
    live, and without this the same question comes back at the next employer.

    Rows only. It never invents a `form_fields` row, because a label nothing has asked
    is not a question — and a row minted here would name a company that never asked it.
    """
    wanted = normalize_label(label)
    hit = [
        r["rowid"]
        for r in conn.execute("SELECT rowid, label FROM form_fields")
        if normalize_label(r["label"]) == wanted
    ]
    for rowid in hit:
        conn.execute(
            "UPDATE form_fields SET question_key=? WHERE rowid=?", (question_key, rowid)
        )
    # The plans holding this field were built before the attachment, so they still carry
    # it as a gap. Blanking the hash is `forget_question`'s move and puts them back in
    # `matches_needing_prefill`; without it a stored plan beats a fresh `resolve_field`
    # in `browser._plan_index` and the field stays empty on the page.
    if hit:
        conn.execute(
            "UPDATE prefill_plans SET answers_hash='' WHERE company IN "
            "(SELECT company FROM form_fields WHERE rowid IN (%s))"
            % ",".join("?" * len(hit)),
            hit,
        )
    return len(hit)


def normalize_label(label: str) -> str:
    """Fold a form label to a comparison key.

    Lowercase, strip anything that is not alphanumeric, collapse whitespace. Deliberately
    aggressive: "Who is your current or previous employer?" and "Who is your current or
    previous employer" must be the same question, because a trailing asterisk marking a
    field required is not a different question.
    """
    kept = [c.lower() if c.isalnum() else " " for c in label]
    return " ".join("".join(kept).split())


def gap_companies(row) -> list[str]:
    """The companies that have asked a gap's question.

    One definition of the delimiter, because `seen_on` is a comma-joined string and two
    readers of it would drift. Accepts either the row or the column, so the Settings page
    and `record_gap` share it.

    Known limitation, inherited from the column and not introduced here: a company name
    containing a comma splits into two. No such name is in companies.yaml today, and the
    fix belongs in the schema rather than in a reader that would silently paper over it.
    """
    text = row if (row is None or isinstance(row, str)) else row["seen_on"]
    return [s for s in (text or "").split(",") if s]


def record_gap(
    conn: sqlite3.Connection,
    question_key: str,
    ask: str,
    field_type: str,
    company: str,
    now: str,
    options: Optional[str] = None,
) -> bool:
    """Note a question we have no answer for. True if it is newly on your list.

    Re-seeing a gap appends the company to `seen_on` rather than creating a second row:
    the same question asked by six employers is one thing for you to answer, not six.

    **A resolved gap reopens, carrying the wording that is still unanswered** (added
    2026-08-25). `question_key` is `slugify(ask)`, which caps at eight words, so several
    genuinely different questions share one row — measured on the live database, these
    five did:

        Are you legally authorized to work in the United States?
        Are you legally authorized to work in the United States for LaunchDarkly?
        Are you legally authorized to work in the United States for our Company?
        Are you legally authorized to work in the country in which you are applying?
        Are you legally authorized to work in the country where this position is located?

    Matching is by the *full* normalized label, so an alias attached to the first fills
    Twilio's form and none of the other four. Before this, the row was marked resolved
    and never came back: the question vanished from Settings while four employers' forms
    kept a blank required field, with the page having said it saved. Failure-as-absence,
    inside the loop that exists to prevent it — and newly load-bearing, because the model
    pass used to match the other four without being asked.

    Only the caller knows a field is still unfilled — `record` calls this for
    `result.gaps` and nothing else — so re-sighting *is* the signal, and `ask` is
    refreshed to the wording that is still open rather than the one that was answered.
    That also keeps it out of `close_answered_gaps`' way: the stored wording is by
    construction one nothing can currently fill, so the two cannot fight over the row.

    Collapsing the five into one row stays right. They are one thing to think about, they
    share a count and a sort position, and each still needs its own alias because that is
    what exact-label matching means.
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
    seen = gap_companies(row["seen_on"])
    if company not in seen:
        seen.append(company)
        conn.execute(
            "UPDATE prefill_gaps SET seen_on=? WHERE question_key=?",
            (",".join(seen), question_key),
        )
    if row["resolved_at"] is not None:
        conn.execute(
            "UPDATE prefill_gaps SET resolved_at=NULL, ask=?, type=?, "
            "options=COALESCE(?, options) WHERE question_key=?",
            (ask, field_type, options, question_key),
        )
        return True
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
    resume_key: Optional[str] = None,
) -> None:
    """Record a built plan. `resume_key` names the posting's own resume, or None.

    A trailing default rather than a positional, so the three-year-old callers that
    predate per-posting resumes keep working and mean what they always meant: this plan
    was built with the answer bank's resume.
    """
    conn.execute(
        """
        INSERT INTO prefill_plans (company, ats_job_id, plan, fields, gaps,
                                   answers_hash, built_at, resume_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company, ats_job_id) DO UPDATE SET
            plan=excluded.plan, fields=excluded.fields, gaps=excluded.gaps,
            answers_hash=excluded.answers_hash, built_at=excluded.built_at,
            resume_key=excluded.resume_key
        """,
        (company, ats_job_id, plan_json, fields, gaps, answers_hash, now, resume_key),
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

    A posting whose own resume has changed is stale too, which is the `resume_key`
    disjunct. It carries no new bind parameter because it compares two stored columns:
    the resume the plan was built with against the one attached now. Attaching, replacing
    or clearing a posting's resume therefore re-plans exactly that posting and nothing
    else — the reason the override is not part of `answers_hash`.
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
        LEFT JOIN posting_resumes pr
               ON p.company=pr.company AND p.ats_job_id=pr.ats_job_id
        WHERE v.verdict='match' AND p.closed_at IS NULL
          AND r.score IS NOT NULL
          AND a.company IS NULL
          AND (d.company IS NULL
               OR (d.kind='snoozed' AND d.until IS NOT NULL AND d.until <= ?))
          AND (pp.answers_hash IS NULL OR pp.answers_hash != ?
               OR COALESCE(pp.resume_key, '') != COALESCE(pr.filename, ''))
        ORDER BY r.score DESC, p.company
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql, (today, answers_hash)))


# -- per-posting resumes -------------------------------------------------------------
def set_posting_resume(
    conn: sqlite3.Connection,
    company: str,
    ats_job_id: str,
    filename: str,
    size: int,
    now: str,
) -> None:
    """Attach a resume to one posting, replacing any earlier one."""
    conn.execute(
        """
        INSERT INTO posting_resumes (company, ats_job_id, filename, bytes, uploaded_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(company, ats_job_id) DO UPDATE SET
            filename=excluded.filename, bytes=excluded.bytes,
            uploaded_at=excluded.uploaded_at
        """,
        (company, ats_job_id, filename, size, now),
    )


def get_posting_resume(
    conn: sqlite3.Connection, company: str, ats_job_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM posting_resumes WHERE company=? AND ats_job_id=?",
        (company, ats_job_id),
    ).fetchone()


def clear_posting_resume(conn: sqlite3.Connection, company: str, ats_job_id: str) -> None:
    conn.execute(
        "DELETE FROM posting_resumes WHERE company=? AND ats_job_id=?",
        (company, ats_job_id),
    )


def posting_resumes(conn: sqlite3.Connection) -> dict[tuple[str, str], sqlite3.Row]:
    """Every override, keyed for a cheap lookup while rendering or while queueing."""
    return {
        (r["company"], r["ats_job_id"]): r
        for r in conn.execute("SELECT * FROM posting_resumes")
    }


# -- mail ----------------------------------------------------------------------------
MAIL_RESOLUTIONS = ("pending", "accepted", "dismissed")


def record_mail_candidate(conn: sqlite3.Connection, cand, now: str) -> bool:
    """Record what the narrower tied to an application. True if this message is new.

    INSERT OR IGNORE, because the primary key is the whole idempotency mechanism: a
    message already recorded — read or not, proposed or not, dismissed or not — must not
    be re-recorded, or a dismissal would be undone by the next scan.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO mail_candidates
            (message_id, company, ats_job_id, choices, match_kind, evidence,
             from_addr, from_name, subject, sent_at, sent_on, body, snippet,
             scanned_at, read_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            cand.message.message_id, cand.company, cand.ats_job_id,
            json.dumps(list(cand.choices)), cand.match_kind, cand.evidence,
            cand.message.from_addr, cand.message.from_name, cand.message.subject,
            cand.message.sent_at, cand.message.sent_on, cand.message.body,
            cand.snippet, now,
        ),
    )
    return cur.rowcount > 0


def known_message_ids(conn: sqlite3.Connection) -> set[str]:
    """Every message already recorded. One query, filtered in Python — the same shape
    `blocked_units` uses, and the reason a scan of 5,000 messages costs one round trip."""
    return {r["message_id"] for r in conn.execute("SELECT message_id FROM mail_candidates")}


def unread_mail_candidates(
    conn: sqlite3.Connection, limit: Optional[int] = None
) -> list[sqlite3.Row]:
    """Candidates the model has not been asked about, newest first.

    `read_at IS NULL` is the queue. Undated messages sort last rather than first: an
    unparseable Date header is not evidence of urgency.
    """
    sql = ("SELECT * FROM mail_candidates WHERE read_at IS NULL "
           "ORDER BY sent_on IS NULL, sent_on DESC, message_id")
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql))


def mark_mail_read(conn: sqlite3.Connection, message_id: str, now: str) -> None:
    """The model has been asked about this message. Set whether or not it proposed
    anything — "read, and it is not application news" is an answer, and re-asking it
    every night would spend the budget on newsletters."""
    conn.execute(
        "UPDATE mail_candidates SET read_at=? WHERE message_id=?", (now, message_id)
    )


def record_mail_proposal(
    conn: sqlite3.Connection,
    message_id: str,
    company: str,
    ats_job_id: str,
    status: str,
    evidence: str,
    now: str,
) -> None:
    if status not in APPLICATION_STATUSES:
        raise ValueError(f"unknown application status: {status!r}")
    conn.execute(
        """
        INSERT INTO mail_proposals (message_id, company, ats_job_id, status, evidence,
                                    resolution, proposed_at, resolved_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL)
        ON CONFLICT(message_id) DO UPDATE SET
            company=excluded.company, ats_job_id=excluded.ats_job_id,
            status=excluded.status, evidence=excluded.evidence,
            proposed_at=excluded.proposed_at
        """,
        (message_id, company, ats_job_id, status, evidence, now),
    )


def get_mail_proposal(
    conn: sqlite3.Connection, message_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM mail_proposals WHERE message_id=?", (message_id,)
    ).fetchone()


def pending_mail_proposals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Proposals awaiting your ruling, with the message they came from.

    Joined to the candidate rather than duplicating subject and snippet into the proposal:
    the message is the evidence, and one copy of it cannot disagree with itself.
    """
    return list(conn.execute(
        """
        SELECT p.*, c.subject, c.from_addr, c.from_name, c.sent_on, c.snippet,
               c.choices, c.match_kind
        FROM mail_proposals p
        JOIN mail_candidates c ON p.message_id = c.message_id
        WHERE p.resolution = 'pending'
        ORDER BY c.sent_on IS NULL, c.sent_on DESC, p.message_id
        """
    ))


def pending_mail_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM mail_proposals WHERE resolution='pending'"
    ).fetchone()["n"]


def resolve_mail_proposal(
    conn: sqlite3.Connection, message_id: str, resolution: str, now: str
) -> None:
    if resolution not in MAIL_RESOLUTIONS:
        raise ValueError(f"unknown resolution: {resolution!r}")
    conn.execute(
        "UPDATE mail_proposals SET resolution=?, resolved_at=? WHERE message_id=?",
        (resolution, now, message_id),
    )


def application_keys(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Every (company, ats_job_id) you have applied to. What the mail narrower is
    allowed to match against, and what `inbox.pending()` re-checks — an application
    deleted since the scan is work the task can no longer do."""
    return {
        (r["company"], r["ats_job_id"])
        for r in conn.execute("SELECT company, ats_job_id FROM applications")
    }
