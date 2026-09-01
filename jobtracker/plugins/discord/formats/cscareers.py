"""The cscareers.dev jobs-bot template.

A real message, and the one this was written against:

    ## [Software Developer Associate @ Artera](<https://jobs.lever.co/artera-2/eae88c70-...>)
    ### Locations:  Seattle, WA
    ### Sponsorship: `Unknown`
    Posted on: July 31, 2026

Structured enough to read role, employer, apply URL, location, sponsorship and a real
posted date with two regexes and no guessing. The angle brackets around the URL are
Discord's embed suppression, and stripping them is not cosmetic — they would otherwise
end up in `postings.url` and in every link on the dashboard.

That field set is one bot's house style, not a Discord convention, which is why this is a
named format with `generic` underneath rather than the parser.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from .base import Format, FormatContext, ParsedJob, register

# A markdown heading whose whole content is one link. `#{1,6}` rather than `##` because a
# restyle to `###` should not silently empty the feed.
_HEADLINE = re.compile(
    r"^\s{0,3}#{1,6}\s*\[(?P<label>[^\]]+)\]\(\s*<?(?P<url>https?://[^\s>)]+)>?\s*\)",
    re.MULTILINE,
)

# One regex for all three labelled lines, with the heading prefix optional. That is what
# keeps this working when the bot restyles `### Locations:` to `**Locations:**` next
# cycle — a change that would otherwise turn every message unparseable at once.
_FIELD = re.compile(
    r"^\s{0,3}[#*_\s]*(?P<key>Locations?|Sponsorship|Posted\s+on)\s*[:\-]\s*(?P<value>.*)$",
    re.MULTILINE | re.IGNORECASE,
)

_DECORATION = re.compile(r"[`*_]+")
_SEPARATOR = " @ "


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", _DECORATION.sub("", value)).strip()


def _parse_day(raw: str, today: str) -> Optional[str]:
    """`July 31, 2026` -> `2026-07-31`, or None.

    None on failure, **never `today`**. A missing date that reads as "posted now" would
    float the stalest reqs to the top of the very ranking the date exists to inform —
    the rule `sync_postings` and every adapter already follow.
    """
    text = _clean(raw)
    if not text:
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


class CsCareers(Format):
    name = "cscareers"

    def matches(self, message: dict, text: str) -> bool:
        m = _HEADLINE.search(text)
        return bool(m) and _SEPARATOR in m.group("label")

    def parse(self, message: dict, ctx: FormatContext) -> Optional[ParsedJob]:
        m = _HEADLINE.search(ctx.text)
        if not m:
            return None
        label = _clean(m.group("label"))
        if _SEPARATOR not in label:
            # Not this bot's shape after all. Falling through to `generic` is the right
            # answer; inventing an employer from half a string is not.
            return None
        role, employer = label.rsplit(_SEPARATOR, 1)
        role, employer = role.strip(), employer.strip()
        if not role:
            return None

        fields = {}
        for f in _FIELD.finditer(ctx.text):
            key = re.sub(r"\s+", " ", f.group("key")).strip().lower().rstrip("s")
            fields.setdefault(key, _clean(f.group("value")))

        posted_raw = fields.get("posted on", "")
        return ParsedJob(
            role=role,
            employer=employer,
            url=m.group("url").strip(),
            location=fields.get("location", ""),
            sponsorship=fields.get("sponsorship", ""),
            # The bot's own wording is kept verbatim as provenance and never compared;
            # only the ISO day below is ever sorted on.
            posted_at=posted_raw or None,
            posted_on=_parse_day(posted_raw, ctx.today),
            format=self.name,
        )


register(CsCareers())
