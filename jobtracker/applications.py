"""The outer loop: what you applied to, what stage it reached, and what it needs next.

Pure functions over `applications` rows. No I/O, no SQL, no HTML — the same split as
`match.py` and `rank.py`, and for the same reason: two surfaces render this data (the
read-only tab in the static dashboard and the editable page under `serve`), and the
grouping must not be able to disagree between them.

Everything here is *derived*. Nothing in this module is stored, and three things are
derived on purpose rather than being columns:

* **Staleness.** "No reply in 30 days" is arithmetic over `updated_at`, not a `ghosted`
  status. A status only you can set is one you will not remember to set, and an unset
  one is indistinguishable from an application that is going fine.
* **Round counts.** `interview ×3` is `len([e for e in events if e.status == 'interview'])`.
  See the note above `store.APPLICATION_STATUSES` for why rounds are repeated events
  rather than numbered statuses.
* **Response rate.** Counted from the event log, so it survives a status moving on.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from .store import ACTIVE_STATUSES, CLOSED_STATUSES, STALE_AFTER_DAYS

# A reply is any stage past `applied` that the employer caused. `withdrawn` is excluded
# because you caused it — counting it as a response would let giving up look like
# traction. `rejected` is included: a rejection is a reply.
RESPONSE_STATUSES = ("oa", "screen", "interview", "offer", "rejected")

# How many days ahead of a next-action date it starts asking for attention. Three days
# is a working week's worth of warning without the section filling up with things that
# are not yet actionable.
DUE_SOON_DAYS = 3


def day_of(stamp: Optional[str]) -> Optional[str]:
    """The ISO day part of a stored timestamp.

    `applied_at`/`updated_at` are full timestamps ('2026-08-16T13:00:00') while
    `next_action` is a plain day. `date.fromisoformat` rejects the former outright, so
    every date comparison here goes through this first. Skipping it does not raise — it
    silently returns None and the row quietly reports no age at all, which is the
    failure-is-absence trap in miniature.
    """
    if not stamp:
        return None
    return stamp.split("T", 1)[0]


def days_since(stamp: Optional[str], today: str) -> Optional[int]:
    """Whole days from `stamp` to `today`, or None if either is unparseable.

    None means "unknown", never 0. A row whose date cannot be read must not render as
    having been touched today.
    """
    day = day_of(stamp)
    if not day:
        return None
    try:
        return (date.fromisoformat(today) - date.fromisoformat(day)).days
    except (ValueError, TypeError):
        return None


def days_until(stamp: Optional[str], today: str) -> Optional[int]:
    """Whole days from `today` to `stamp`. Negative means overdue."""
    since = days_since(stamp, today)
    return None if since is None else -since


def is_active(app) -> bool:
    return app["status"] in ACTIVE_STATUSES


def is_closed(app) -> bool:
    return app["status"] in CLOSED_STATUSES


def is_stale(app, today: str) -> bool:
    """An active application nobody has touched in STALE_AFTER_DAYS.

    Closed applications are never stale — there is nothing left to chase.
    """
    if not is_active(app):
        return False
    since = days_since(app["updated_at"], today)
    return since is not None and since >= STALE_AFTER_DAYS


def action_state(app, today: str) -> Optional[str]:
    """'overdue' | 'today' | 'soon' | 'later', or None when nothing is scheduled.

    None is the normal state for most rows and never means overdue — an application
    with no next action is not behind, it just has nothing booked.
    """
    if not is_active(app):
        return None
    until = days_until(app["next_action"], today)
    if until is None:
        return None
    if until < 0:
        return "overdue"
    if until == 0:
        return "today"
    return "soon" if until <= DUE_SOON_DAYS else "later"


def needs_action(app, today: str) -> bool:
    """Does this want attention now — either a due action or a stalled application?"""
    return action_state(app, today) in ("overdue", "today", "soon") or is_stale(app, today)


def round_counts(events) -> dict[str, int]:
    """How many times each status was reached. Drives the `interview ×3` badge."""
    counts: dict[str, int] = {}
    for event in events or ():
        counts[event["status"]] = counts.get(event["status"], 0) + 1
    return counts


def has_responded(app, events) -> bool:
    """Did the employer ever come back?

    Read from the event log rather than the current status so a rejection after three
    interview rounds still counts as a response — by the time it is `rejected` the
    current status alone no longer says one ever happened.
    """
    if any(e["status"] in RESPONSE_STATUSES for e in events or ()):
        return True
    # An application entered by hand at a later stage may have no event history that
    # predates it. Its current status is then the only evidence there is.
    return app["status"] in RESPONSE_STATUSES


def group(apps, events_by, today: str) -> dict[str, list]:
    """Split applications into the three sections the page renders, each sorted.

    Ordering is by urgency, not recency, because the point of the page is to answer
    "what do I owe someone today" before "what did I do lately":

    * **needs action** — soonest deadline first, then the most stalled.
    * **active** — most stalled first, so the ones going quiet surface without a filter.
    * **closed** — most recent first; it is a record, not a queue.
    """
    order = {"overdue": 0, "today": 1, "soon": 2, "later": 3}

    def action_key(app):
        state = action_state(app, today)
        until = days_until(app["next_action"], today)
        return (
            order.get(state, 4),
            until if until is not None else 10**6,
            -(days_since(app["updated_at"], today) or 0),
        )

    def stale_key(app):
        # Longest untouched first; an unreadable date sorts last rather than first, so a
        # bad timestamp cannot masquerade as the most urgent thing on the page.
        since = days_since(app["updated_at"], today)
        return (-(since if since is not None else -1), app["company"], app["title"])

    active, closed, urgent = [], [], []
    for app in apps:
        if is_closed(app):
            closed.append(app)
        elif needs_action(app, today):
            urgent.append(app)
        else:
            active.append(app)

    urgent.sort(key=action_key)
    active.sort(key=stale_key)
    closed.sort(key=lambda a: (a["updated_at"] or "", a["company"]), reverse=True)
    return {"needs_action": urgent, "active": active, "closed": closed}


def summary(apps, events_by) -> dict[str, int | float]:
    """The tile row: totals, where things stand, and the conversion rate.

    `response_rate` is the number `jobtracker apply`'s docstring is reaching for — the
    evidence that would eventually let tiers be re-ranked by what actually converts
    rather than by prior. It is a percentage of *all* applications, so it falls when you
    apply and does not move until someone replies.
    """
    total = len(apps)
    responded = sum(
        1 for a in apps
        if has_responded(a, events_by.get((a["company"], a["ats_job_id"]), []))
    )
    return {
        "total": total,
        "active": sum(1 for a in apps if is_active(a)),
        "interviewing": sum(1 for a in apps if a["status"] == "interview"),
        "offers": sum(1 for a in apps if a["status"] == "offer"),
        "responded": responded,
        # Guarded: an empty tracker reports 0%, not a ZeroDivisionError on a page whose
        # whole job is to be openable before you have applied to anything.
        "response_rate": round(100 * responded / total) if total else 0,
    }


def parse_day(value: str) -> Optional[str]:
    """Validate a user-supplied ISO day, returning it or None.

    None means "not a date", and every caller must treat it as a refusal rather than
    storing the raw text. A `next_action` that does not parse would sort as a string
    against real dates and quietly never come due.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except (ValueError, TypeError):
        return None


def default_next_action(today: str, days: int = 7) -> str:
    """A week out — the follow-up date offered when you record an application."""
    return (date.fromisoformat(today) + timedelta(days=days)).isoformat()
