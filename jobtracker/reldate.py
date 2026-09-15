"""Relative ages, resolved to a day.

Job boards date their postings relatively far more often than ATSes do, and each one
phrases it differently: Simplify's README said `2d`, YC's listing says `about 1 year` and
`7 days`, Workday says `Posted 30+ Days Ago`. All of them mean "this many units before
the moment you read it", which is why `today` is a **parameter** everywhere this is used
and never a clock read — a parse must give the same answer twice.

Resolving at parse time and storing the result absolutely is the whole point. A stored
`2d` re-read a month later silently means something new; a stored `2026-09-13` does not.

**Unparseable input is None, never today.** A missing date reading as "posted now" would
invert the ranking it exists to inform, which is the rule `postings.posted_on` is built
on. Nothing here guesses.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

# Days per unit. An hour is not a fraction of a day here: anything under a day is today,
# which is the honest reading of "3h ago" for a field whose resolution is one day.
_UNITS = {
    "h": 0, "hr": 0, "hrs": 0, "hour": 0, "hours": 0,
    "d": 1, "day": 1, "days": 1,
    "w": 7, "wk": 7, "wks": 7, "week": 7, "weeks": 7,
    "mo": 30, "mos": 30, "month": 30, "months": 30,
    "y": 365, "yr": 365, "yrs": 365, "year": 365, "years": 365,
}

# Hedges a human-phrased age carries. They are dropped rather than interpreted: "about a
# year" and "a year" land on the same day, and the alternative is pretending to a
# precision the source does not have.
_HEDGES = ("about", "almost", "over", "nearly", "around", "approximately", "~", "posted")

# "1", "a" and "an" all mean one. `_NUMBER` keeps that in one place so "a month ago" and
# "1 month ago" cannot drift apart.
_NUMBER = re.compile(r"^(?:(\d+)\+?|an?)\s*")
_UNIT = re.compile(r"^([a-z]+)")


def relative_day(raw: object, today: str) -> Optional[str]:
    """`raw` as an ISO day counted back from `today`, or None if it is not an age.

    Accepts the compact form (`2d`, `3mo`, `30+`) and the prose form (`about 1 year`,
    `7 days ago`, `a month ago`). A trailing "ago" is ignored; so is a leading hedge.

    `30+ days` is floored at exactly 30 — the value is a bound, not a date, and treating
    it as one is the most recent day it could possibly mean.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None

    for hedge in _HEDGES:
        if text.startswith(hedge):
            text = text[len(hedge):].strip()

    number = _NUMBER.match(text)
    if not number:
        return None
    count = int(number.group(1)) if number.group(1) else 1

    unit_match = _UNIT.match(text[number.end():].strip())
    if not unit_match:
        return None
    unit = _UNITS.get(unit_match.group(1))
    if unit is None:
        return None

    try:
        anchor = date.fromisoformat(today)
    except (ValueError, TypeError):
        return None
    return (anchor - timedelta(days=count * unit)).isoformat()
