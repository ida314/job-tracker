"""Source adapter interface + registry.

Each ATS gets one adapter, and it is the *only* place that vendor's JSON shape is
known. Everything downstream speaks in normalized Posting objects. Adapters are pure:
they build URLs and parse payloads; they never touch the network (that is fetch.py).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from ..models import Posting


def iso_day(raw: object) -> Optional[str]:
    """The date part of an ISO-8601 timestamp, or None if it is not one.

    Shared by Greenhouse and Ashby, whose formats differ only in offset spelling and
    fractional seconds — `2026-08-01T01:46:42-04:00` vs `2026-08-01T01:57:58.337+00:00`.
    Deliberately does *not* convert to local time: a posting is dated by the day the
    vendor says it was posted, and shifting that by a timezone would move ~4% of
    postings by a day for no gain.
    """
    if not raw:
        return None
    text = str(raw).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    # Some boards send a bare date; accept it, reject anything else.
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


class Source:
    ats: str = ""
    jobs_method: str = "GET"

    def jobs_url(self, slug: str) -> str:
        raise NotImplementedError

    def parse_jobs(self, company: str, raw: object) -> list[Posting]:
        raise NotImplementedError

    # Identity: how we confirm the board belongs to the right company (DESIGN.md §7.2).
    # Greenhouse has a dedicated endpoint; Ashby/Lever derive identity from the payload.
    def identity_url(self, slug: str) -> Optional[str]:
        return None

    def parse_identity(self, raw: object) -> Optional[str]:
        return None

    def identity_from_jobs(self, raw: object) -> Optional[str]:
        return None

    # Per-posting description. Only needed where the bulk jobs payload omits it:
    # Ashby and Lever both ship `descriptionPlain` in the list call, so they leave
    # this alone and cost zero extra requests. Greenhouse is the exception, because
    # we deliberately drop ?content=true from the bulk call (it blows the timeout on
    # large boards — CLAUDE.md), so its descriptions are fetched one at a time and
    # only for postings the LLM pass actually needs to read.
    def job_detail_url(self, slug: str, ats_job_id: str) -> Optional[str]:
        return None

    def parse_job_detail(self, raw: object) -> Optional[str]:
        """Plain-text description from a single-job payload."""
        return None

    def parse_job_detail_posted_at(self, raw: object) -> Optional[str]:
        """A better posted timestamp from the single-job payload, if there is one.

        Only Greenhouse has one: the bulk call exposes `updated_at`, which moves every
        time anyone edits the req, while the detail payload carries `first_published`.
        """
        return None

    # Vendor timestamp -> plain ISO date. `today` is a parameter rather than a clock
    # read because adapters are pure (DESIGN.md §3.1) and because one source dates its
    # rows relatively, so "now" has to come from the caller that already knows it.
    def normalize_posted_at(self, raw: object, today: str) -> Optional[str]:
        return iso_day(raw)


_REGISTRY: dict[str, Source] = {}


def register(source: Source) -> Source:
    _REGISTRY[source.ats] = source
    return source


def get_source(ats: str) -> Optional[Source]:
    return _REGISTRY.get(ats)


def api_sources() -> set[str]:
    return set(_REGISTRY)
