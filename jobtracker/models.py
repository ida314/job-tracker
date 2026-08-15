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
    """A curated target. Mirrors one entry in companies.yaml.

    That file is only ever written by a foreground command someone typed — never by a
    scheduled run — which is what keeps curation and run state apart (DESIGN.md §2.3).
    """

    name: str
    ats: str
    slug: str
    tier: Optional[int] = None
    category: str = ""
    check_method: str = "manual"  # api | manual | aggregator
    expected_board_name: Optional[str] = None
    careers_page: str = ""  # human-facing fallback + where slug-repair reads
    board_url: str = ""  # aggregator feeds fetch this directly (a raw README URL), since
    #                      they have no slug to template — see sources/aggregator.py.
    notes: str = ""

    @property
    def key(self) -> str:
        return self.name


@dataclass(frozen=True)
class Posting:
    """A normalized job posting. Every source adapter emits exactly this shape.

    ats_job_id is the stable identifier from the vendor; (company, ats_job_id) is the
    primary key everywhere downstream. Our own first_seen lives in the DB, not here.

    Two date fields, and the distinction matters. `posted_at` is whatever the vendor
    sent, verbatim and in the vendor's own format — kept as provenance, never compared.
    `posted_on` is that value normalized to a plain ISO date, and is the only one
    anything is allowed to sort or do arithmetic on.

    Keeping both is not redundancy. The raw values are mutually incomparable across
    sources — Greenhouse sends `2026-08-01T01:46:42-04:00`, Ashby
    `2026-08-01T01:57:58.337+00:00`, Lever the epoch-millis string `1785533737281`, and
    the aggregator a relative age like `2d`. In one TEXT column an epoch string sorts
    before every ISO string, so `ORDER BY posted_at` is silently, plausibly wrong.
    """

    company: str
    ats_job_id: str
    title: str
    url: str
    location: str = ""
    posted_at: Optional[str] = None
    description: str = ""
    posted_on: Optional[str] = None


@dataclass(frozen=True)
class Verdict:
    """A matching decision plus its reason. Persisted so filtering is auditable."""

    company: str
    ats_job_id: str
    decision: Decision
    reason: str
    # 'rules'  — the deterministic matcher (match.py)
    # 'llm'    — the ambiguity pass, resolving an UNCERTAIN title from its description
    # 'human'  — a per-posting override you set; outranks both
    decided_by: str = "rules"


@dataclass(frozen=True)
class FormField:
    """One question on one application form.

    The shared vocabulary between the two ways a form becomes known — an ATS that
    publishes its questions, and a browser reading the rendered DOM — so the resolution
    and gap machinery downstream never has to know which one it is looking at.

    `key` is the ATS's own field name where there is one (`first_name`, `question_681…`)
    and a slug of the label where there is not. `options` is empty for anything that is
    not a select; a select with no options is a form we failed to read properly, not a
    select with nothing to choose.
    """

    key: str
    label: str
    type: str  # text | textarea | select | multiselect | file | checkbox
    required: bool = False
    options: tuple[str, ...] = ()

    @property
    def is_file(self) -> bool:
        return self.type == "file"


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
    # Consecutive runs this board has failed to *fetch*. Counts a different thing from
    # consecutive_empty_runs next door, and conflating the two loses the distinction the
    # whole health module exists to preserve: empty is a fact about the company (dbt Labs
    # has no reqs), failing is a fact about the board (it moved, or it is down). This is
    # the counter that makes "persistent FETCH_FAILED" expressible — see
    # health.needs_repair.
    consecutive_failures: int = 0
