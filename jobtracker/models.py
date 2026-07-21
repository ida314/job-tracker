"""Shared vocabulary: the dataclasses/enums every layer speaks in.

These types are deliberately dumb — no I/O, no behaviour beyond light derivation.
The point of Design A is that data shapes are explicit and can be validated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Decision(str, Enum):
    """Outcome of matching a single posting against the criteria."""

    MATCH = "match"
    REJECT = "reject"
    UNCERTAIN = "uncertain"


class HealthStatus(str, Enum):
    """Board-level health. Encodes the §2.2 failure catalogue as distinct states.

    OK              — reachable, identity confirmed (or unpinned), postings trusted.
    SUSPECT_EMPTY   — reachable but zero postings; NOT the same as "no openings".
    IDENTITY_DRIFT  — reachable but the board belongs to the wrong company.
    FETCH_FAILED    — non-200 / timeout / malformed JSON. Absence of data, not data.
    """

    OK = "ok"
    SUSPECT_EMPTY = "suspect_empty"
    IDENTITY_DRIFT = "identity_drift"
    FETCH_FAILED = "fetch_failed"


@dataclass(frozen=True)
class Company:
    """A curated target. Mirrors one entry in companies.yaml (never machine-written)."""

    name: str
    ats: str
    slug: str
    tier: Optional[int] = None
    category: str = ""
    check_method: str = "manual"  # api | manual | aggregator
    expected_board_name: Optional[str] = None
    careers_page: str = ""  # human-facing fallback + where slug-repair reads
    notes: str = ""

    @property
    def key(self) -> str:
        return self.name


@dataclass(frozen=True)
class Posting:
    """A normalized job posting. Every source adapter emits exactly this shape.

    ats_job_id is the stable identifier from the vendor; (company, ats_job_id) is the
    primary key everywhere downstream. posted_at is the vendor's timestamp (often the
    last-edited time, so untrustworthy) — our own first_seen lives in the DB, not here.
    """

    company: str
    ats_job_id: str
    title: str
    url: str
    location: str = ""
    posted_at: Optional[str] = None
    description: str = ""


@dataclass(frozen=True)
class Verdict:
    """A matching decision plus its reason. Persisted so filtering is auditable."""

    company: str
    ats_job_id: str
    decision: Decision
    reason: str
    decided_by: str = "rules"  # 'rules' | 'llm'


@dataclass
class FetchResult:
    """The outcome of fetching one company's board.

    Carries enough to let health.py distinguish empty / drift / failure without
    guessing. `postings` is only meaningful when `error` is None and status is 200.
    """

    company: str
    ats: str
    slug: str
    ok: bool = False
    status_code: Optional[int] = None
    observed_board_name: Optional[str] = None
    postings: list[Posting] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class BoardHealth:
    """Board health snapshot, persisted in board_health and consulted next run."""

    company: str
    status: HealthStatus
    consecutive_empty_runs: int = 0
    observed_board_name: Optional[str] = None
    last_ok_at: Optional[str] = None
    detail: str = ""
    alerting: bool = False  # SUSPECT_EMPTY that has crossed the escalation threshold
