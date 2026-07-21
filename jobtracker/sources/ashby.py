"""Ashby posting API.

  jobs  GET https://api.ashbyhq.com/posting-api/job-board/{slug}  -> {"jobs": [...]}

Ashby exposes no board-name field, so identity is derived from the jobUrl host+path:
every jobUrl looks like https://jobs.ashbyhq.com/{org-slug}/{uuid}. We extract that
org-slug and compare it to the configured slug — enough to catch the ashby/cedar class
of collision where a reachable board belongs to an unrelated company (DESIGN.md §7.2).

Note: fetching jobs.ashbyhq.com/{slug} directly is useless for verification — that host
returns a 200 SPA shell for any string. Only this posting API gives a real answer.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from ..models import Posting
from .base import Source, register

BASE = "https://api.ashbyhq.com/posting-api/job-board"


class Ashby(Source):
    ats = "ashby"

    def jobs_url(self, slug: str) -> str:
        return f"{BASE}/{slug}"

    def parse_jobs(self, company: str, raw: object) -> list[Posting]:
        if not isinstance(raw, dict):
            return []
        postings: list[Posting] = []
        for job in raw.get("jobs", []):
            if not isinstance(job, dict) or not job.get("id"):
                continue
            location = job.get("location") or job.get("locationName") or ""
            if job.get("isRemote") and "remote" not in str(location).lower():
                location = f"Remote / {location}".strip(" /")
            postings.append(
                Posting(
                    company=company,
                    ats_job_id=str(job["id"]),
                    title=str(job.get("title") or ""),
                    url=str(job.get("jobUrl") or job.get("applyUrl") or ""),
                    location=str(location),
                    posted_at=job.get("publishedAt"),
                )
            )
        return postings

    def identity_from_jobs(self, raw: object) -> Optional[str]:
        """Extract the org slug from the first jobUrl path: /{org}/{uuid}."""
        if not isinstance(raw, dict):
            return None
        for job in raw.get("jobs", []):
            if isinstance(job, dict) and job.get("jobUrl"):
                path = urlparse(str(job["jobUrl"])).path.strip("/")
                if path:
                    return path.split("/")[0]
        return None


register(Ashby())
