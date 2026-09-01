"""Workday's `cxs` job board API.

  jobs    POST https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
          {"appliedFacets":{},"limit":20,"offset":N,"searchText":""}
  detail  GET  https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}

Keyless, unauthenticated, and identical in shape across every tenant — which is why the
nine Workday entries on the target list spent months as `check_method: manual` on the
belief that no public JSON board existed. It does; nobody had looked for it. Verified
2026-08-31 against redhat, nvidia, capitalone, workday and target.

`slug` is a tenant *triple*, `tenant/dc/site`, because no single string identifies a
Workday board: the data centre (`wd1`, `wd5`, `wd12`, …) is part of the hostname and
carries no relationship to the tenant name. Whitespace around the separators is tolerated
because the two entries that predate this adapter were written by hand as
`redhat / wd5 / jobs`.

Four things about this API are traps, and each one presents as an empty board — the
failure DESIGN.md §3.4 exists to prevent. See `jobs_page_error`, `jobs_body`,
`normalize_posted_at` and `identity_from_jobs` below; each has a test named after it.
"""

from __future__ import annotations

import html
import re
from datetime import date, timedelta
from typing import Optional

from ..models import Posting
from .base import Source, iso_day, register

# Workday's hard page cap. Asking for more does not clamp and does not error — see
# `jobs_page_error`. Measured against redhat (total 122): limit=20 returns 20 rows,
# limit=50 and limit=100 return a payload with no `jobPostings` key at all.
PAGE_SIZE = 20

_RELATIVE = re.compile(r"posted\s+(\d+)\+?\s+days?\s+ago", re.I)


class Workday(Source):
    ats = "workday"
    jobs_method = "POST"
    page_size = PAGE_SIZE

    # -- addressing --------------------------------------------------------------------
    @staticmethod
    def parse_slug(slug: str) -> Optional[tuple[str, str, str]]:
        """`tenant/dc/site` -> its three parts, or None if it is not a triple."""
        parts = [p.strip() for p in (slug or "").split("/")]
        if len(parts) != 3 or not all(parts):
            return None
        return parts[0], parts[1], parts[2]

    def _host(self, slug: str) -> Optional[str]:
        parsed = self.parse_slug(slug)
        if parsed is None:
            return None
        tenant, dc, _site = parsed
        return f"https://{tenant}.{dc}.myworkdayjobs.com"

    def _cxs(self, slug: str) -> Optional[str]:
        parsed = self.parse_slug(slug)
        if parsed is None:
            return None
        tenant, _dc, site = parsed
        return f"{self._host(slug)}/wday/cxs/{tenant}/{site}"

    def jobs_url(self, slug: str) -> str:
        # A malformed triple still has to produce a string; the request then 404s and the
        # board reports FETCH_FAILED, which is the honest outcome for a bad slug. Raising
        # here would take down the whole run for one curated typo.
        return f"{self._cxs(slug) or 'https://invalid.workday.slug'}/jobs"

    def jobs_body(self, slug: str, offset: int) -> Optional[dict]:
        """One page of results.

        `appliedFacets` stays empty on purpose. Workday will filter by location or
        category server-side, and doing so would reintroduce the geography gate deleted
        on 2026-07-22 — the one that discarded 390 postings before their titles were ever
        read. Everything is fetched; `match.py` decides what is on target.
        """
        return {
            "appliedFacets": {},
            "limit": PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        }

    def jobs_page_error(self, raw: object) -> Optional[str]:
        """Reject a payload that is not a page of results.

        Workday answers a `limit` above its cap with HTTP 200, valid JSON, and no
        `jobPostings` key — not an error, not a clamp, just a different shape. Parsed
        with `.get("jobPostings", [])` that is zero rows, and zero rows on page one is a
        board with nothing open: `sync_postings` would close every posting the company
        has. The distinction between "no rows" and "not an answer" has to be made here,
        because by the time a row count is returned it is gone.
        """
        if not isinstance(raw, dict):
            return f"expected a JSON object, got {type(raw).__name__}"
        if "jobPostings" not in raw:
            return "no `jobPostings` key — the request shape was rejected"
        if not isinstance(raw["jobPostings"], list):
            return "`jobPostings` is not a list"
        return None

    # -- parsing -----------------------------------------------------------------------
    def parse_jobs(self, company: str, raw: object) -> list[Posting]:
        if not isinstance(raw, dict):
            return []
        postings: list[Posting] = []
        for job in raw.get("jobPostings") or []:
            if not isinstance(job, dict):
                continue
            path = str(job.get("externalPath") or "").strip()
            if not path:
                continue
            postings.append(
                Posting(
                    company=company,
                    # The path is the id: stable across runs, unique within a board, and
                    # the exact key the detail endpoint takes. `bulletFields[0]` carries
                    # the human req number (R-058865) but is not always present.
                    ats_job_id=path,
                    title=str(job.get("title") or ""),
                    url="",  # filled by `posting_url`, which needs the slug
                    location=str(job.get("locationsText") or ""),
                    posted_at=job.get("postedOn"),
                )
            )
        return postings

    def posting_url(self, slug: str, ats_job_id: str) -> str:
        parsed = self.parse_slug(slug)
        if parsed is None:
            return ""
        _tenant, _dc, site = parsed
        return f"{self._host(slug)}/{site}{ats_job_id}"

    def job_detail_url(self, slug: str, ats_job_id: str) -> Optional[str]:
        cxs = self._cxs(slug)
        return f"{cxs}{ats_job_id}" if cxs else None

    def parse_job_detail(self, raw: object) -> Optional[str]:
        if not isinstance(raw, dict):
            return None
        info = raw.get("jobPostingInfo")
        if not isinstance(info, dict):
            return None
        content = info.get("jobDescription")
        if not content:
            return None
        text = html.unescape(str(content))
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        return re.sub(r"[ \t\xa0]+", " ", text).strip()

    def parse_job_detail_posted_at(self, raw: object) -> Optional[str]:
        """`startDate` — the exact day the relative prose is counted from.

        Verified 2026-08-31: a posting reading "Posted 2 Days Ago" carries
        `startDate: 2026-08-29`, exactly two days before that date. So the detail payload
        turns an approximation into the real thing, at no extra request, because the
        description fetch already goes here. Greenhouse's `first_published` again.
        """
        if not isinstance(raw, dict):
            return None
        info = raw.get("jobPostingInfo")
        if not isinstance(info, dict):
            return None
        value = info.get("startDate")
        return str(value) if value else None

    def normalize_posted_at(self, raw: object, today: str) -> Optional[str]:
        """Workday dates a board relatively and a posting absolutely.

        The bulk row says "Posted Today" / "Posted 2 Days Ago" / "Posted 30+ Days Ago";
        the detail payload says `2026-08-29`. Both arrive here, so both are handled, ISO
        first. Anything else is None — never today, because a missing date reading as
        "posted now" would invert the ranking it exists to inform.

        "30+ Days Ago" is floored at exactly 30. It is a bound rather than a date, and 30
        is the honest edge of it: it can only make a posting look newer than it is, never
        older, and the detail fetch replaces it with `startDate` the moment anything reads
        the description.
        """
        exact = iso_day(raw)
        if exact:
            return exact
        text = str(raw or "").strip().lower()
        if not text:
            return None
        try:
            base = date.fromisoformat(today)
        except (TypeError, ValueError):
            return None
        if text == "posted today":
            return base.isoformat()
        if text == "posted yesterday":
            return (base - timedelta(days=1)).isoformat()
        match = _RELATIVE.search(text)
        if match:
            return (base - timedelta(days=int(match.group(1)))).isoformat()
        return None

    def identity_from_jobs(self, raw: object) -> Optional[str]:
        """No identity, deliberately.

        Nothing in either payload names the employer. The only company-ish string
        available is the tenant inside `externalUrl`, which restates the slug we asked
        for — the Ashby/Lever tautology `ashby/cedar` sails straight through. Returning
        it would let a wrong tenant verify itself, so a Workday board can only ever be
        evidenced as reachable, never as identity-confirmed.
        """
        return None


register(Workday())
