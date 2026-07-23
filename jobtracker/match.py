"""Deterministic matching: Posting -> Verdict. Pure, no I/O, no model.

The naive design is a boolean predicate; that forces genuinely-ambiguous titles into
a wrong answer. Instead this is a three-way verdict (DESIGN.md §6). The gating axis is
LEVEL — a title with an explicit new-grad/entry signal and no disqualifier MATCHes; a
title with a disqualifier or off-target role family REJECTs; a title with no level
signal is UNCERTAIN, which is exactly the residual the deferred LLM pass would read.

Matching reads the TITLE only. Geography is not a gate — see the note in match().

Every verdict carries the rule that fired, so silent filtering is impossible to
mistake for a bug (DESIGN.md §3.5).
"""

from __future__ import annotations

from .criteria import Criteria
from .models import Decision, Posting, Verdict


def match(posting: Posting, criteria: Criteria) -> Verdict:
    title = posting.title.lower()

    def verdict(decision: Decision, reason: str) -> Verdict:
        return Verdict(posting.company, posting.ats_job_id, decision, reason, "rules")

    # 1. Hard title disqualifiers (senior, staff, intern, II/III, ...).
    hit = criteria.first_hit("exclude_titles", title)
    if hit:
        return verdict(Decision.REJECT, f"excluded_title:{hit}")

    # 2. Off-target role families (frontend, mobile, ML-first, non-eng).
    hit = criteria.first_hit("role_type_exclude", title)
    if hit:
        return verdict(Decision.REJECT, f"excluded_role:{hit}")

    # NOTE: geography is deliberately NOT a gate (2026-07-22, at the user's direction).
    # It used to sit here and rejected 390 postings — London, Dublin, Bengaluru, Toronto
    # and so on — *before* they ever reached the level gate, so their titles were never
    # evaluated at all. Location now RANKS instead: see location_rank() below. Nothing is
    # disqualified for where it is; results are merely ordered NYC first.

    # 3. Level gate. An explicit entry-level signal in the title is necessary but not
    #    sufficient: a MATCH also needs an engineering signal, otherwise "Finance
    #    Associate" (level word, no engineering) would match a backend tracker.
    level = criteria.first_hit("level_include", title)
    if level:
        role = criteria.first_hit("role_type_include", title)  # backend-specific
        eng = role or criteria.first_hit("engineering_terms", title)  # any engineering
        if eng:
            detail = f"level:{level}+" + (f"role:{role}" if role else "eng:generic")
            return verdict(Decision.MATCH, detail)
        # Entry-level, but not an engineering role -> off target.
        return verdict(Decision.REJECT, f"non_engineering_role:level={level}")

    # 4. No level signal in the title. The eligibility, if any, is in the JD body,
    #    which the deterministic layer does not read. Hand it to the UNCERTAIN bucket.
    return verdict(Decision.UNCERTAIN, "no_level_token_in_title")


# Rank names, lowest (most preferred) first. Index == the rank integer.
LOCATION_RANKS = ("nyc", "us", "unknown", "non-us")

NYC, US, UNKNOWN, NON_US = range(4)


def location_rank(location: str | None, criteria: Criteria) -> int:
    """Order a posting by where it is. Preference only — this never rejects anything.

    0 NYC · 1 elsewhere in the US · 2 unknown · 3 outside the US.

    Unknown outranks explicit non-US on purpose: a bare "Remote" is more likely to be
    US-eligible than a posting that names Bengaluru, so vague data is not punished as
    hard as data that plainly says "not here".

    Check order is NYC -> non-US -> US, and it is load-bearing. The US list contains
    two-letter state codes, several of which are ambiguous country codes ("CA" is both
    California and Canada), so an explicit country name must win before codes are
    consulted. "New York" is never non-US, so NYC can safely go first.
    """
    text = (location or "").strip().lower()
    if not text:
        return UNKNOWN
    if criteria.first_hit("locations_nyc", text):
        return NYC
    if criteria.first_hit("locations_non_us", text):
        return NON_US
    if criteria.first_hit("locations_us", text):
        return US
    return UNKNOWN


def location_label(rank: int) -> str:
    """Human-readable rank name, for reports and the dashboard."""
    return LOCATION_RANKS[rank]
