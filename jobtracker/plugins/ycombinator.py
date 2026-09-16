"""The Y Combinator jobs board.

**Read this before changing anything here.** CLAUDE.md's standing rule is that a portal
with no public JSON board is never scraped, and YC has no such board: no `/jobs.json`, no
Algolia key for jobs (the page ships `{"indices_not_set": true}`), no pagination. What it
does have is a server-rendered page carrying its entire job list as JSON in one
`data-page` attribute — Inertia's payload, the same bytes the page renders itself from.

Reading that is a **deliberate exception**, taken by the user on 2026-09-15, and it is
scoped so it stays one:

  * A small fixed set of listing paths, declared in settings and capped at six. This is
    never a crawl, and it never follows a link off the page.
  * The payload is parsed as data. Nothing here touches the DOM, so a restyle is not a
    breakage and a breakage is not a silent empty board — `page_error` says which.
  * `robots.txt` allows `/jobs` (checked 2026-09-15; it disallows `/companies?*`, which
    is not fetched).

**The trap, and the reason `url` is not `applyUrl`.** Every job's `applyUrl` is the same
`account.ycombinator.com/authenticate?continue=...` login URL. Stored as the posting's
URL, every YC row would normalize to one dedupe key, and the ranking would collapse the
whole board to a single pick while the page claimed they were all the same req. The job's
own detail path is unique, so that is what is stored — and the login is where applying
sends you anyway, which is a fact about YC worth seeing on the row rather than hiding.

A single module, not a package: see the note in `simplify.py`.
"""

from __future__ import annotations

import html as html_mod
import json
import re
from typing import Optional
from urllib.parse import urljoin

from ..models import Company, Posting
from ..reldate import relative_day
from .base import Plugin, register

BASE = "https://www.ycombinator.com"

# The listing paths read by default. Narrow on purpose: this tracker wants backend
# new-grad roles, and a page per location is how YC lets you say so without a crawl.
DEFAULT_PATHS = (
    "/jobs/role/software-engineer",
    "/jobs/role/software-engineer/new-york",
    "/jobs/role/software-engineer/remote",
)

# Settings are typed by their defaults, and the type here is `str` — a list would have no
# declared type to validate against. Comma-separated, which is also how you would type it.
MAX_PATHS = 6

_DATA_PAGE = re.compile(r'data-page="([^"]*)"')


def _payload(raw: object) -> Optional[dict]:
    """The page's Inertia payload as a dict, or None if this is not that page.

    Never raises: a truncated response, a login wall and a redirect to marketing all
    arrive here as ordinary strings, and each has to become a reported failure rather
    than an exception on the one hand or an empty board on the other.
    """
    if not isinstance(raw, str):
        return None
    found = _DATA_PAGE.search(raw)
    if not found:
        return None
    try:
        parsed = json.loads(html_mod.unescape(found.group(1)))
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _jobs(raw: object) -> Optional[list]:
    """The job list, or None when the page did not carry one."""
    payload = _payload(raw)
    if payload is None:
        return None
    postings = (payload.get("props") or {}).get("jobPostings")
    return postings if isinstance(postings, list) else None


def _paths(settings: dict) -> list:
    raw = str(settings.get("paths") or "")
    paths = [p.strip() for p in raw.split(",") if p.strip()]
    return paths or list(DEFAULT_PATHS)


def _location(job: dict) -> str:
    """YC separates places with " / "; this tracker separates them with "; "."""
    text = str(job.get("location") or "").strip()
    return "; ".join(p.strip() for p in text.split("/") if p.strip()) if text else ""


def _description(job: dict) -> str:
    """What the listing says beyond its title, as text for the model passes to read.

    `minExperience` is the field that matters most here — YC writes "Any (new grads ok)"
    on the roles this tracker is looking for, and "6+ years" on the ones it is not. It is
    left as prose rather than turned into a rule: deciding a level from a listing field is
    the `level` task's job, and a gate here would apply before any title was read.
    """
    bits = []
    for label, value in (
        ("Role", job.get("roleSpecificType")),
        ("Experience", job.get("minExperience")),
        ("School year", job.get("minSchoolYear")),
        ("Employment", job.get("type")),
        ("Visa", job.get("visa")),
        ("Batch", job.get("companyBatchName")),
        ("Locations", _location(job)),
    ):
        text = str(value or "").strip()
        if text:
            bits.append(f"{label}: {text}")
    skills = [str(s).strip() for s in (job.get("skills") or []) if str(s).strip()]
    if skills:
        bits.append("Skills: " + ", ".join(skills))
    one_liner = str(job.get("companyOneLiner") or "").strip()
    if one_liner:
        bits.append(one_liner)
    return "\n".join(bits)


class YCombinator(Plugin):
    name = "ycombinator"
    tag = "yc"
    summary = "import the Y Combinator jobs board"
    page_format = "text"

    def defaults(self) -> dict:
        return {
            "enabled": False,
            # Listing paths, comma-separated. Change these to follow a different role or
            # city; `validate` caps the count, because this is a fixed read and not a
            # crawl and the cap is what keeps that true.
            "paths": ",".join(DEFAULT_PATHS),
            # YC listings sit for a year, and the board has no way to say a req closed —
            # `lastActive` is about the company, not the role. Shorter than the 90-day
            # default because a year-old YC listing is not an opening.
            "expire_after_days": 60,
            "base_url": BASE,
        }

    def validate(self, settings: dict) -> None:
        from .settings import InvalidSettings

        if settings.get("expire_after_days", 0) < 0:
            raise InvalidSettings("ycombinator: `expire_after_days` cannot be negative")
        paths = _paths(settings)
        if len(paths) > MAX_PATHS:
            raise InvalidSettings(
                f"ycombinator: at most {MAX_PATHS} listing paths — this is a fixed read "
                "of a few pages, and a longer list makes it a crawl"
            )
        for path in paths:
            if not path.startswith("/"):
                raise InvalidSettings(
                    f"ycombinator: `paths` holds site paths like /jobs/role/"
                    f"software-engineer, not {path!r}"
                )
        if not str(settings.get("base_url") or "").startswith("https://"):
            raise InvalidSettings("ycombinator: `base_url` must be an https URL")

    def company(self, settings: dict) -> Company:
        return Company(
            name="Y Combinator jobs",
            ats="plugin",
            slug="",
            tier=None,
            category="startup-jobs",
            check_method="plugin",
            expected_board_name=None,
            notes="Imported by the ycombinator plugin. Not curation — see docs/plugins.md.",
        )

    # -- a fixed set of pages, each a complete statement of its own listing ------------
    def page_urls(self, settings: dict, today: str) -> list:
        base = str(settings.get("base_url") or BASE)
        return [urljoin(base, path) for path in _paths(settings)]

    def page_error(self, raw: object) -> Optional[str]:
        """Why this page is not a listing, or None.

        Everything this catches returns HTTP 200 and would otherwise parse to zero jobs:
        a login wall, a redirect to marketing, a truncated body, and the day YC stops
        rendering its list into the page. Zero *jobs* on a page that does carry the list
        is allowed through — that is health's question, and `evaluate_plugin` asks it of
        every snapshot read.
        """
        if not isinstance(raw, str):
            return "the response was not a page"
        if _payload(raw) is None:
            return "no data-page payload — the page did not render its job list"
        if _jobs(raw) is None:
            return "the payload carries no jobPostings — its shape has changed"
        return None

    def page_ids(self, raw: object) -> list:
        return [str(j.get("id")) for j in (_jobs(raw) or []) if j.get("id")]

    def parse_page(self, group: str, raw: object, settings: dict, today: str):
        base = str(settings.get("base_url") or BASE)
        postings: list[Posting] = []
        unparsed = 0
        for job in _jobs(raw) or []:
            if not isinstance(job, dict):
                unparsed += 1
                continue
            job_id = str(job.get("id") or "").strip()
            title = str(job.get("title") or "").strip()
            path = str(job.get("url") or "").strip()
            if not (job_id and title and path):
                unparsed += 1
                continue

            employer = str(job.get("companyName") or "").strip()
            created = str(job.get("createdAt") or "").strip()
            postings.append(
                Posting(
                    company=group,
                    ats_job_id=job_id,
                    title=f"{employer} — {title}" if employer else title,
                    # The job's own page, never `applyUrl` — see the module docstring.
                    url=urljoin(base, path),
                    location=_location(job),
                    employer=employer or None,
                    posted_at=created or None,
                    posted_on=relative_day(created, today),
                    description=_description(job),
                )
            )
        return postings, unparsed, 0

    def describe_cursor(self, cursor: str) -> str:
        return "reads a fixed set of listing pages every run"


register(YCombinator())
