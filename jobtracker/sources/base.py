"""Source adapter interface + registry.

Each ATS gets one adapter, and it is the *only* place that vendor's JSON shape is
known. Everything downstream speaks in normalized Posting objects. Adapters are pure:
they build URLs and parse payloads; they never touch the network (that is fetch.py).
"""

from __future__ import annotations

from typing import Optional

from ..models import Posting


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


_REGISTRY: dict[str, Source] = {}


def register(source: Source) -> Source:
    _REGISTRY[source.ats] = source
    return source


def get_source(ats: str) -> Optional[Source]:
    return _REGISTRY.get(ats)


def api_sources() -> set[str]:
    return set(_REGISTRY)
