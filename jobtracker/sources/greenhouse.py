"""Greenhouse board API.

  jobs      GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs   -> {"jobs": [...]}
  identity  GET https://boards-api.greenhouse.io/v1/boards/{slug}        -> {"name": ...}

We deliberately DROP ?content=true on the jobs call: title/location/absolute_url/id are
all present without it, and full-content payloads blow the timeout on large boards
(Databricks ~787 reqs — CLAUDE.md). Descriptions are only needed by the deferred LLM
pass, which will fetch them per-posting when it exists.
"""

from __future__ import annotations

import html
import re
from typing import Optional

from ..models import Posting
from .base import Source, register

BASE = "https://boards-api.greenhouse.io/v1/boards"


class Greenhouse(Source):
    ats = "greenhouse"

    def jobs_url(self, slug: str) -> str:
        return f"{BASE}/{slug}/jobs"

    def identity_url(self, slug: str) -> Optional[str]:
        return f"{BASE}/{slug}"

    def parse_identity(self, raw: object) -> Optional[str]:
        if isinstance(raw, dict):
            name = raw.get("name")
            return str(name) if name else None
        return None

    def job_detail_url(self, slug: str, ats_job_id: str) -> Optional[str]:
        return f"{BASE}/{slug}/jobs/{ats_job_id}"

    def parse_job_detail(self, raw: object) -> Optional[str]:
        """Plain text from the single-job `content` field.

        Greenhouse HTML-escapes the markup *inside* a JSON string, so `content`
        arrives as `&lt;h2&gt;Who we are&lt;/h2&gt;` rather than `<h2>`. Unescaping
        has to happen before tag-stripping — strip first and the entities survive
        verbatim, and the model is handed a wall of `&lt;p&gt;`.
        """
        if not isinstance(raw, dict):
            return None
        content = raw.get("content")
        if not content:
            return None
        text = html.unescape(str(content))
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)  # entities like &amp;nbsp; survive the first pass
        return re.sub(r"[ \t\xa0]+", " ", text).strip()

    def parse_jobs(self, company: str, raw: object) -> list[Posting]:
        if not isinstance(raw, dict):
            return []
        postings: list[Posting] = []
        for job in raw.get("jobs", []):
            if not isinstance(job, dict) or job.get("id") is None:
                continue
            location = ""
            loc = job.get("location")
            if isinstance(loc, dict):
                location = str(loc.get("name") or "")
            postings.append(
                Posting(
                    company=company,
                    ats_job_id=str(job["id"]),
                    title=str(job.get("title") or ""),
                    url=str(job.get("absolute_url") or ""),
                    location=location,
                    posted_at=job.get("updated_at"),
                )
            )
        return postings


register(Greenhouse())
