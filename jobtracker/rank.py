"""The nightly ranking pass: which three jobs to apply to tomorrow.

`resolve` decides whether a posting is *on-target*. This decides which of the ones that
are is *urgent*. Two bounded questions, two bounded model roles — and the model's part
here is deliberately the smaller one.

The division of labour is the whole design:

    model   reads one description against your profile and returns three labelled
            ordinals plus a sentence. It never sees another posting, never returns a
            score, and never returns an order.
    python  turns those labels into a number using weights from profile.yaml, combined
            with three things it already knows — company tier, location rank, and how
            long ago the posting went up.

That split buys two properties worth protecting. Reweighting is free, because the
model's answers are cached and only the arithmetic replays: change how much you value
growth against pay and the entire queue re-sorts with zero model calls. And the order
is explainable — every position decomposes into named terms you can print, which a
"the model said 74" design cannot do.

Scores are absolute, not relative, and that is what gives you the queue behaviour you
actually want: a new posting is judged once and lands in its slot without disturbing
anything above or below it. Nothing is renumbered, no comparison state exists to go
stale, and one bad judgment can misplace exactly one posting.

Ordering is `ORDER BY score DESC`. What is *excluded* is as important: anything you
have applied to, skipped, or snoozed until a future date. That is what stops tomorrow
from showing you the same three jobs, and stops a job you meant to apply to from
silently falling off the bottom of the list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from .criteria import Criteria
from .match import location_rank
from .models import Company
from .profile import Profile

log = logging.getLogger("jobtracker.rank")

# Ordinal labels -> [0, 1]. The gaps are uneven on purpose: "strong" should clearly
# beat "moderate", while "weak" and "none" are both varieties of no, so squeezing them
# together stops a long tail of near-misses from crowding out a genuine hit.
ORDINAL_VALUE = {"strong": 1.0, "moderate": 0.6, "weak": 0.25, "none": 0.0}
RISK_VALUE = {"low": 0.0, "medium": 0.5, "high": 1.0}

# Location rank (0 NYC .. 3 non-US) -> [0, 1]. Mirrors match.location_rank()'s own
# reasoning: unknown scores above explicit non-US because a bare "Remote" is more
# likely to be US-eligible than a posting that names another country.
LOCATION_VALUE = {0: 1.0, 1: 0.7, 2: 0.4, 3: 0.15}

# Tier -> [0, 1], using the same three bands the dashboard colours by (T1-T2 anchor,
# T3-T5 applied, T6-T7 research). Three bands rather than seven steps because the tier
# numbers encode a strategy, not a ranking: tier 4 is not "twice as good" as tier 2.
TIER_VALUE = {"anchor": 1.0, "applied": 0.55, "research": 0.2, "none": 0.35}

# What an unknown posting date is worth. Deliberately mid-scale: an undated posting
# must not read as brand new (which would float stale reqs to the top) nor as ancient
# (which would bury a genuinely fresh one because its board is stingy with metadata).
UNKNOWN_RECENCY = 0.5


def tier_band(tier: Optional[int]) -> str:
    if not isinstance(tier, int):
        return "none"
    if tier <= 2:
        return "anchor"
    if tier <= 5:
        return "applied"
    return "research"


def days_since(day: Optional[str], today: str) -> Optional[int]:
    if not day:
        return None
    try:
        return (date.fromisoformat(today) - date.fromisoformat(day)).days
    except (ValueError, TypeError):
        return None


def recency_value(posted_on: Optional[str], today: str, half_life: float) -> float:
    """Exponential decay from the posted date, halving every `half_life` days.

    Decay rather than a cliff because urgency is continuous: a 15-day-old posting is
    not categorically worse than a 14-day-old one. Future dates clamp to 1.0 — a
    vendor clock skewed a few hours ahead should not produce a value above the scale.
    """
    days = days_since(posted_on, today)
    if days is None:
        return UNKNOWN_RECENCY
    if days <= 0:
        return 1.0
    return 0.5 ** (days / half_life)


@dataclass(frozen=True)
class ScoreBreakdown:
    """A score and the named terms that produced it.

    Kept as a structure rather than a bare float because "why is this #1" has to be
    answerable. DESIGN.md §3.5 requires every automated verdict to carry its reason;
    an unexplained ordering is the same failure in a different costume.
    """

    total: float
    terms: dict

    def explain(self) -> str:
        parts = [f"{k} {v:+.1f}" for k, v in sorted(
            self.terms.items(), key=lambda kv: -abs(kv[1])
        )]
        return " · ".join(parts)


def score_posting(
    row,
    profile: Profile,
    criteria: Criteria,
    tier: Optional[int],
    today: str,
) -> Optional[ScoreBreakdown]:
    """Compose one posting's score. None if the model has not judged it yet.

    Returning None rather than 0.0 is deliberate: an unjudged posting is unknown, not
    bad, and scoring it zero would bury it at the bottom of the list where nobody would
    ever notice the model had failed to read it.
    """
    fit = row["backend_fit"] if "backend_fit" in row.keys() else None
    if fit is None or fit not in ORDINAL_VALUE:
        return None
    growth = row["growth"]
    risk = row["entry_risk"]
    if growth not in ORDINAL_VALUE or risk not in RISK_VALUE:
        return None

    w = profile.weights
    posted_on = row["posted_on"] if "posted_on" in row.keys() else None
    terms = {
        "fit": w["fit"] * ORDINAL_VALUE[fit],
        "growth": w["growth"] * ORDINAL_VALUE[growth],
        "recency": w["recency"] * recency_value(
            posted_on, today, profile.recency_half_life_days
        ),
        "tier": w["tier"] * TIER_VALUE[tier_band(tier)],
        "location": w["location"] * LOCATION_VALUE[
            location_rank(row["location"] if "location" in row.keys() else "", criteria)
        ],
        # entry_risk's weight is negative in profile.yaml, so this subtracts.
        "entry_risk": w["entry_risk"] * RISK_VALUE[risk],
    }
    return ScoreBreakdown(total=sum(terms.values()), terms=terms)


def is_available(row, today: str) -> bool:
    """Is this posting still awaiting a decision from you?

    Excluded: applied to, skipped, or snoozed until a date that has not arrived. An
    expired snooze returns the posting to the queue on its own, which is the point of
    snoozing rather than skipping.
    """
    keys = row.keys()
    if "applied_status" in keys and row["applied_status"]:
        return False
    kind = row["deferral_kind"] if "deferral_kind" in keys else None
    if kind == "skipped":
        return False
    if kind == "snoozed":
        until = row["deferral_until"] if "deferral_until" in keys else None
        return not (until and until > today)
    return True


@dataclass
class RankStats:
    considered: int = 0
    judged: int = 0
    unreadable: int = 0
    scored: int = 0
    unranked: int = 0

    def summary(self) -> str:
        tail = f", {self.unranked} still unranked" if self.unranked else ""
        return (
            f"{self.judged} newly judged · {self.scored} scored"
            f" ({self.unreadable} the model could not read){tail}"
        )


def judge_matches(rows, client, profile: Profile) -> tuple[list[tuple], RankStats]:
    """Ask the model about each row. Returns [(company, ats_job_id, judgment), ...].

    Pure with respect to storage — the caller writes. Takes an already-built client so
    it can be tested against a stub with no network, the same shape `resolve_postings`
    uses.

    A row the model cannot read is simply absent from the result. It is never given a
    default judgment, because a fabricated "moderate" is worse than a visible gap.
    """
    stats = RankStats()
    out: list[tuple] = []
    for row in rows:
        stats.considered += 1
        judgment = client.judge_posting(row["title"], row["description"], profile.prose)
        if judgment is None:
            stats.unreadable += 1
            continue
        stats.judged += 1
        out.append((row["company"], row["ats_job_id"], judgment))
        log.debug(
            "judged %s — %s: fit=%s growth=%s risk=%s",
            row["company"], row["title"][:48],
            judgment.backend_fit, judgment.growth, judgment.entry_risk,
        )
    return out, stats


def top_n(rows, n: int, today: str) -> list:
    """The n highest-scoring postings still awaiting your decision."""
    available = [r for r in rows if is_available(r, today) and r["score"] is not None]
    return available[:n]


def tier_lookup(companies: list[Company]) -> dict:
    return {c.name: c.tier for c in companies}
