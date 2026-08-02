"""Community new-grad list repositories (SimplifyJobs-style).

These are the single highest-yield source for new-grad roles specifically, because
they aggregate across every company — including ones not on our target list (DESIGN.md
§9). One repo maintains one big HTML `<table>` in its README; a row is one role.

This adapter is the odd one out among sources in three ways, all consequences of the
data being a rendered README rather than a JSON board:

  * It parses **HTML text**, not JSON, so the fetch path hands it `raw` as a `str`
    (Fetcher._request_text / fetch_aggregator), not a parsed object.
  * One feed lists **many employers**. We keep the aggregator's own name as the
    Posting.company (so `sync_postings` has one stable diff namespace per feed) and put
    the real employer in the title: `"NVIDIA — Software Engineer, New Grad"`. The
    title-only matcher (match.py) reads that fine, and the employer stays visible in the
    dashboard's company/title columns without a schema change.
  * There is no ATS id. We use the stable Simplify posting UUID when present, else a
    hash of employer+role — enough for the diff to recognize the same row next run.

The list is mostly *closed* roles (a lock, 🔒, in the Application cell); at last check
1,745 of 2,072 rows. Closed rows are skipped — a filled req is not an opening.

Robustness is load-bearing: these repos rename by cycle year and occasionally restyle
the table, so a parse that hits an unexpected shape must return `[]` (an empty feed is
SUSPECT_EMPTY, a visible flag) rather than raise. Same contract as every other adapter.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import date, timedelta
from typing import Optional

from ..models import Posting
from .base import Source, register

_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_HREF = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_SIMPLIFY_ID = re.compile(r"simplify\.jobs/p/([0-9a-f-]+)", re.IGNORECASE)
_BR = re.compile(r"</?br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
# Decorative markers the lists append to a role/company cell; not part of the text.
_MARKERS = ("🔒", "🎓", "🛂", "🔥", "🆕", "⭐")

# The continuation glyph: a row whose Company cell is just "↳" repeats the employer above.
_CONTINUATION = "↳"

# The Age column: "2d", "3mo", "1yr", sometimes with a trailing "+" on a bucketed value.
_AGE = re.compile(r"^(\d+)\s*(h|d|w|mo|y)")


def _text(fragment: str) -> str:
    """Visible text of an HTML cell: drop tags, unescape entities, collapse whitespace."""
    text = _BR.sub("; ", fragment)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    for marker in _MARKERS:
        text = text.replace(marker, " ")
    return re.sub(r"\s+", " ", text).strip().strip(";").strip()


class Aggregator(Source):
    ats = "aggregator"

    def parse_jobs(self, company: str, raw: object) -> list[Posting]:
        if not isinstance(raw, str):
            return []
        postings: list[Posting] = []
        seen_ids: set[str] = set()
        employer = ""
        for row in _ROW.findall(raw):
            cells = _CELL.findall(row)
            if len(cells) < 4:  # header rows use <th>; malformed rows lack columns
                continue
            company_cell, role_cell, location_cell, application_cell = cells[:4]
            # A 5th column holds the posting's age ("2d", "1mo"). It is optional —
            # these repos restyle their tables every cycle — so a row without it is
            # still a valid row, just one with no date.
            age_cell = _text(cells[4]) if len(cells) > 4 else ""

            name = _text(company_cell)
            if name and name != _CONTINUATION:
                employer = name  # a fresh employer; later ↳ rows inherit it
            if not employer:
                continue

            if "🔒" in application_cell:  # a closed req is not an opening
                continue
            href = _HREF.search(application_cell)
            role = _text(role_cell)
            if not href or not role:
                continue
            apply_url = html.unescape(href.group(1))

            uuid = _SIMPLIFY_ID.search(application_cell)
            ats_job_id = (
                uuid.group(1)
                if uuid
                else hashlib.sha1(f"{employer}|{role}".encode()).hexdigest()[:16]
            )
            if ats_job_id in seen_ids:  # a repo can list the same role twice
                continue
            seen_ids.add(ats_job_id)

            postings.append(
                Posting(
                    company=company,
                    ats_job_id=ats_job_id,
                    title=f"{employer} — {role}",
                    url=apply_url,
                    location=_text(location_cell),
                    posted_at=age_cell or None,
                )
            )
        return postings

    def normalize_posted_at(self, raw: object, today: str) -> Optional[str]:
        """A relative age ("2d", "3mo") counted back from the run date.

        This is the one source that dates relatively, which is why `today` is threaded
        through the interface at all. The value is only as fresh as the run that read
        it, so it is resolved once at parse time and stored absolutely — re-reading a
        stored "2d" a month later would otherwise silently mean something new.
        """
        if not raw:
            return None
        m = _AGE.match(str(raw).strip().lower())
        if not m:
            return None
        try:
            anchor = date.fromisoformat(today)
        except ValueError:
            return None
        n = int(m.group(1))
        unit = m.group(2)
        days = n * {"h": 0, "d": 1, "w": 7, "mo": 30, "y": 365}[unit]
        if unit == "h":
            days = 0  # anything under a day is today
        return (anchor - timedelta(days=days)).isoformat()


register(Aggregator())
