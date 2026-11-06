"""The review queue's lanes: the one classification of an UNCERTAIN row.

An uncertain posting is one the rules could not decide. Most of the queue is not
engineering at all — on the laptop corpus, most uncertain titles carry no field word —
and the user chose to *fold* that part rather than reject it: it stays a short list of
possible misses, sorted after everything more promising. This module says which part of
the queue a row is in, and nothing else.

Several surfaces have to agree on that — `/review`, the dashboard tables, the Today count
and the folds — so the lane is **one pure function**. Two copies are how two pages come
to disagree about what is on a list.

- **Derived when a page renders, never stored.** A lane reads the title, the criteria,
  the curation and the model's reading as they are now; a stored lane would be a
  snapshot of all four that nothing invalidates.
- **Folding is display, never a verdict.** No lane moves a posting out of `uncertain`,
  and nothing here writes. A folded row is still on the list.
- **`board_scope` is read here and nowhere else.** It is curation saying a board serves
  many roles outside the field; it decides which fold a no-signal row lands in, and never
  `match()`, health or a fetch.

Pure: no SQLite, no files, no clock. The caller hands in everything — the row (a
`store.uncertain_with_context` row or any mapping with the same keys), the criteria, the
curated companies by name, each job board's `coverage()` by origin, and the reading.
`targets` is the set of career bands the user is after; it is a parameter, never read
from config here, so preferences can feed it without this module knowing they exist.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Mapping, Optional

from .criteria import Criteria
from .match import location_rank
from .tasks.level import looks_engineering


class Lane(str, Enum):
    """Declared in queue order: most promising first. `sort_key` reads this order."""

    FEED = "feed"            # feed declares a band we target, and the title has a field signal
    READ_UNCLEAR = "read"    # level read real prose; it does not state a level
    SIGNAL = "signal"        # field signal, not read yet
    NO_SIGNAL = "no_signal"  # folded
    BOARD_NOISE = "noise"    # NO_SIGNAL on a board_scope: mixed company; folded inside the fold


# Lanes a page folds by default. BOARD_NOISE is a fold inside this one.
FOLDED = frozenset({Lane.NO_SIGNAL, Lane.BOARD_NOISE})

_ORDER = {ln: i for i, ln in enumerate(Lane)}

# How a career band reads in a sentence. The one table that turns a band name into words,
# so the wording of a band is written exactly once; a band missing from it reads as its own name.
BAND_WORDS = {
    "entry": "new-grad",
}

_LABELS = {
    Lane.READ_UNCLEAR: "read — the description does not say",
    Lane.SIGNAL: "not read yet",
    Lane.NO_SIGNAL: "no field signal in the title",
    Lane.BOARD_NOISE: "board lists many non-field roles",
}


def _field(row: Any, key: str) -> Any:
    """`row[key]`, or None when the row has no such key. A database row has no `.get`."""
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key] if key in row.keys() else None
    except (AttributeError, TypeError):
        return getattr(row, key, None)


def _band(reading: Any) -> Optional[str]:
    """The band a reading recorded, or None for no reading.

    Accepts a `store.level_reading` row (`band`), an object with `.band`, or a
    `store.uncertain_with_context` row (`reading_band`) — so a caller holding the context
    row can pass it as its own reading.
    """
    if reading is None:
        return None
    band = getattr(reading, "band", None)
    if band is None:
        band = _field(reading, "band")
    if band is None:
        band = _field(reading, "reading_band")
    return band or None


def feed_band(
    row: Any, coverage_by_origin: Mapping, targets: tuple = ("entry",)
) -> Optional[str]:
    """The first band the row's job board is curated to that is also one we target.

    None for a curated company board (no origin), an origin with no coverage — a plugin
    since removed, or never registered — and a board curated to nothing we want.
    """
    origin = _field(row, "origin")
    if not origin:
        return None
    coverage = coverage_by_origin.get(origin) or {}
    for band in coverage.get("bands") or ():
        if band in targets:
            return band
    return None


def lane(
    row: Any,
    criteria: Criteria,
    companies_by_name: Mapping,
    coverage_by_origin: Mapping,
    reading: Any,
    targets: tuple = ("entry",),
) -> Lane:
    """Which part of the review queue an uncertain row is in.

    The order of checks is the definition. The field signal comes first and is
    `looks_engineering` and nothing else — the same test that scopes `level`'s queue, so
    "not read yet" never names a row `level` would not read. Without a signal, the row is
    folded; it folds deeper when its company's board is curated `board_scope: mixed`.
    With one, a job board that declares a targeted band wins over everything the model
    said, then an `unclear` reading, then the plain signal.

    `companies_by_name` is read with `.get`: a job board's group is never curation and is
    never in it, and that absence must read as an ordinary board, not a crash.
    """
    title = _field(row, "title") or ""
    if not looks_engineering(title, criteria):
        company = companies_by_name.get(_field(row, "company"))
        if getattr(company, "board_scope", None) == "mixed":
            return Lane.BOARD_NOISE
        return Lane.NO_SIGNAL
    if feed_band(row, coverage_by_origin, targets) is not None:
        return Lane.FEED
    if _band(reading) == "unclear":
        return Lane.READ_UNCLEAR
    return Lane.SIGNAL


def label(lane: Lane, coverage_band: Optional[str] = None) -> str:
    """The words a page shows for a lane.

    For `FEED`, the wording comes from the band (`feed_band`) through `BAND_WORDS`; with
    no band given it says what the lane means without naming one.
    """
    lane = Lane(lane)
    if lane is Lane.FEED:
        if not coverage_band:
            return "listed on a feed curated to a level you target"
        return f"listed on a {BAND_WORDS.get(coverage_band, coverage_band)} feed"
    return _LABELS[lane]


def _day_ordinal(first_seen: Any) -> Optional[int]:
    try:
        return date.fromisoformat(str(first_seen)[:10]).toordinal()
    except (TypeError, ValueError):
        return None


def sort_key(row: Any, lane: Lane, criteria: Criteria) -> tuple:
    """`(lane order, location_rank, -first_seen)`: lane first, then where, then newest.

    Location ranks and never gates (`match.location_rank`). An unreadable `first_seen`
    sorts last within its lane and location, never as "seen today".
    """
    day = _day_ordinal(_field(row, "first_seen"))
    newest = (0, -day) if day is not None else (1, 0)
    return (
        _ORDER[Lane(lane)],
        location_rank(_field(row, "location"), criteria),
        newest,
    )
