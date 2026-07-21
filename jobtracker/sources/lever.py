"""Lever postings API.

  jobs  GET https://api.lever.co/v0/postings/{slug}?mode=json  -> [ {...}, ... ]

Lever returns a bare array with no board-name field; identity is derived from the
hostedUrl path, which begins with the org slug.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from ..models import Posting
from .base import Source, register

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
                )
            )
        return postings

    def identity_from_jobs(self, raw: object) -> Optional[str]:
        if isinstance(raw, list):
            for job in raw:
                if isinstance(job, dict) and job.get("hostedUrl"):
                    path = urlparse(str(job["hostedUrl"])).path.strip("/")
                    if path:
                        return path.split("/")[0]
        return None


register(Lever())
