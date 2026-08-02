"""Lever postings API.

  jobs  GET https://api.lever.co/v0/postings/{slug}?mode=json  -> [ {...}, ... ]

Lever returns a bare array with no board-name field; identity is derived from the
hostedUrl path, which begins with the org slug.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from ..models import Posting
from .base import Source, iso_day, register

BASE = "https://api.lever.co/v0/postings"


class Lever(Source):
    ats = "lever"

    def jobs_url(self, slug: str) -> str:
        return f"{BASE}/{slug}?mode=json"

    def parse_jobs(self, company: str, raw: object) -> list[Posting]:
        if not isinstance(raw, list):
            return []
        postings: list[Posting] = []
        for job in raw:
            if not isinstance(job, dict) or not job.get("id"):
                continue
            location = ""
            cats = job.get("categories")
            if isinstance(cats, dict):
                location = str(cats.get("location") or "")
            postings.append(
                Posting(
                    company=company,
                    ats_job_id=str(job["id"]),
                    title=str(job.get("text") or ""),
                    url=str(job.get("hostedUrl") or job.get("applyUrl") or ""),
                    location=location,
                    posted_at=str(job.get("createdAt")) if job.get("createdAt") else None,
                    # Free in the bulk payload, like Ashby. `descriptionPlain` is the
                    # opening blurb; `lists` holds the requirements — which is where
                    # the level signal ("0-2 years", "recent graduate") usually lives,
                    # so the two are concatenated rather than taking the first.
                    description=_description(job),
                )
            )
        return postings

    def normalize_posted_at(self, raw: object, today: str) -> Optional[str]:
        """Lever dates in epoch milliseconds, delivered as a string.

        This is the value that makes a single raw column unsortable: `1785533737281`
        collates before every ISO timestamp, so a mixed `ORDER BY posted_at` silently
        buries every Lever posting at one end.
        """
        if raw is None:
            return None
        text = str(raw).strip()
        if not text.isdigit():
            return iso_day(text)  # a board that switched formats should not vanish
        try:
            return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return None

    def identity_from_jobs(self, raw: object) -> Optional[str]:
        if isinstance(raw, list):
            for job in raw:
                if isinstance(job, dict) and job.get("hostedUrl"):
                    path = urlparse(str(job["hostedUrl"])).path.strip("/")
                    if path:
                        return path.split("/")[0]
        return None


def _description(job: dict) -> str:
    """Opening blurb plus the requirement lists, flattened to plain text."""
    parts = [str(job.get("descriptionPlain") or "")]
    for section in job.get("lists") or []:
        if not isinstance(section, dict):
            continue
        text = str(section.get("text") or "")
        # `content` is an HTML <li> blob; strip tags rather than pull in a parser.
        content = re.sub(r"<[^>]+>", " ", str(section.get("content") or ""))
        parts.append(f"{text}: {content}" if text else content)
    return "\n".join(p.strip() for p in parts if p.strip())


register(Lever())
