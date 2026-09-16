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


# The views the page can be sorted into, in the order the bar offers them. The first is
# the default: an unknown or missing key falls back to it rather than erroring, because
# the key arrives in a URL anyone can mistype.
SORTS = (
    ("urgency", "Needs action"),
    ("applied", "Newest applied"),
    ("applied_asc", "Oldest applied"),
    ("stage", "Stage"),
    ("company", "Company"),
    ("tier", "Tier"),
)
SORT_KEYS = tuple(k for k, _ in SORTS)

# Furthest along first, then the two ways an application ends without an offer.
STAGE_ORDER = ("offer", "interview", "screen", "oa", "applied", "rejected", "withdrawn")


def sort_key(value) -> str:
    """The requested view, or the default when the value is not one we offer."""
    return value if value in SORT_KEYS else SORT_KEYS[0]


def employer(app) -> str:
    """Who the job is at. A job-board application's `company` is the board's name, so
    the posting's `employer` wins where `all_applications` found one."""
    keys = app.keys()
    return ("employer" in keys and app["employer"]) or app["company"]


def _by_applied(apps, *, newest: bool) -> list:
    """Chronological order of applying. An unreadable `applied_at` sorts last in either
    direction — it is unknown, not the oldest or the newest thing on the page."""
    dated = [a for a in apps if _valid_day(a["applied_at"])]
    undated = [a for a in apps if not _valid_day(a["applied_at"])]
    dated.sort(key=lambda a: (employer(a).casefold(), a["title"]))
    # Stable, so equal stamps keep the name order above in both directions.
    dated.sort(key=lambda a: a["applied_at"], reverse=newest)
    undated.sort(key=lambda a: (employer(a).casefold(), a["title"]))
    return dated + undated


def _valid_day(stamp) -> bool:
    try:
        date.fromisoformat(day_of(stamp))
        return True
    except (ValueError, TypeError):
        return False


def arrange(apps, events_by, today: str, sort: str, tier_of=None) -> list[tuple]:
    """The sections a view renders, as `(key, heading, blurb, rows)`.

    One derivation for both surfaces, like `group`, which is the default view here.
    `tier_of(company) -> int | None` is passed in because tiers are curation, and this
    module reads no files. Within a grouped view the newest application comes first.
    """
    sort = sort_key(sort)
    if sort == "urgency":
        groups = group(apps, events_by, today)
        return [
            (k, heading, blurb, groups[k])
            for k, heading, blurb in (
                ("needs_action", "Needs action",
                 "a date has come due, or nobody has moved in "
                 f"{STALE_AFTER_DAYS} days"),
                ("active", "Active", "applied, waiting"),
                ("closed", "Closed", "offer, rejection, or withdrawn"),
            )
            if groups[k]
        ]
    if sort in ("applied", "applied_asc"):
        newest = sort == "applied"
        return [("all", "By date applied",
                 "newest first" if newest else "oldest first",
                 _by_applied(apps, newest=newest))]
    if sort == "company":
        rows = _by_applied(apps, newest=True)
        rows.sort(key=lambda a: employer(a).casefold())
        return [("all", "By company", "A–Z, newest application first", rows)]

    newest = _by_applied(apps, newest=True)
    if sort == "stage":
        rank = {s: i for i, s in enumerate(STAGE_ORDER)}
        sections = []
        for status in sorted({a["status"] for a in apps},
                             key=lambda s: (rank.get(s, len(rank)), s)):
            rows = [a for a in newest if a["status"] == status]
            sections.append((status, status.capitalize(), "", rows))
        return sections

    # tier
    tiers: dict = {}
    for app in newest:
        # The board's own entry first; a job-board row falls through to its employer,
        # which is tiered only when that employer is also curated.
        tier = tier_of(app["company"]) if tier_of else None
        if not isinstance(tier, int) and tier_of:
            tier = tier_of(employer(app))
        tiers.setdefault(tier if isinstance(tier, int) else None, []).append(app)
    return [
        (f"tier-{t}" if t is not None else "tier-none",
         f"Tier {t}" if t is not None else "No tier",
         "" if t is not None else "an employer not in companies.yaml",
         tiers[t])
        for t in sorted(tiers, key=lambda t: (t is None, t or 0))
    ]


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
