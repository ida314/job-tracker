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

from ..models import FormField, Posting
from .base import Source, register

BASE = "https://boards-api.greenhouse.io/v1/boards"

# Greenhouse's own field types, mapped to the vocabulary in models.FormField. Anything
# not listed is treated as free text, which is the safe default: a field we fill with a
# typed answer is recoverable, a field we skip because we did not recognize its type is
# a blank on a submitted application.
_GH_TYPES = {
    "input_text": "text",
    "textarea": "textarea",
    "input_file": "file",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "multiselect",
    "boolean": "select",
}


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

    def parse_job_detail_posted_at(self, raw: object) -> Optional[str]:
        """`first_published` — the only true posted date Greenhouse exposes.

        The bulk call gives `updated_at`, which moves whenever anyone touches the req:
        a fix to a salary band re-dates a six-month-old posting as fresh. That field is
        a freshness signal at best. `first_published` only exists on the detail payload,
        which we already fetch for the description, so this costs no extra request.
        """
        if not isinstance(raw, dict):
            return None
        value = raw.get("first_published") or raw.get("updated_at")
        return str(value) if value else None

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

    # -- application form ------------------------------------------------------------
    def application_form_url(self, slug: str, ats_job_id: str) -> Optional[str]:
        """The single-job payload, asked for its questions.

        `?questions=true` is keyless and complete: field names, types, required flags,
        and the full option list of every select. That is the whole reason prefill can
        detect a question it has no answer for without ever opening a browser — and it
        is the only ATS here that offers it.
        """
        return f"{BASE}/{slug}/jobs/{ats_job_id}?questions=true"

    def parse_application_form(self, raw: object) -> list[FormField]:
        """Flatten `questions[].fields[]` into one field per input.

        Greenhouse nests fields under a question because one question can render as
        several inputs — "Resume/CV" is a file input *and* a textarea, either of which
        satisfies it. They are kept as separate fields, sharing the question's label, so
        that filling the file input and leaving the textarea empty is representable.
        """
        if not isinstance(raw, dict):
            return []
        out: list[FormField] = []
        for question in raw.get("questions") or []:
            if not isinstance(question, dict):
                continue
            label = str(question.get("label") or "").strip()
            required = bool(question.get("required"))
            for field in question.get("fields") or []:
                if not isinstance(field, dict) or not field.get("name"):
                    continue
                options = tuple(
                    str(v["label"])
                    for v in (field.get("values") or [])
                    if isinstance(v, dict) and v.get("label") is not None
                )
                out.append(FormField(
                    key=str(field["name"]),
                    label=label or str(field["name"]),
                    type=_GH_TYPES.get(str(field.get("type")), "text"),
                    required=required,
                    options=options,
                ))
        return out


register(Greenhouse())
