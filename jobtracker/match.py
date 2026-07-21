"""Deterministic matching: Posting -> Verdict. Pure, no I/O, no model.

The naive design is a boolean predicate; that forces genuinely-ambiguous titles into
a wrong answer. Instead this is a three-way verdict (DESIGN.md §6). The gating axis is
LEVEL — a title with an explicit new-grad/entry signal and no disqualifier MATCHes; a
title with a disqualifier or off-target role family REJECTs; a title with no level
signal is UNCERTAIN, which is exactly the residual the deferred LLM pass would read.

Every verdict carries the rule that fired, so silent filtering is impossible to
mistake for a bug (DESIGN.md §3.5).
"""

from __future__ import annotations

from .criteria import Criteria
from .models import Decision, Posting, Verdict


def match(posting: Posting, criteria: Criteria) -> Verdict:
    title = posting.title.lower()
    location = (posting.location or "").lower()

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

    # 3. Clearly out-of-scope geography (only when the location is populated).
    if location:
        hit = criteria.first_hit("locations_exclude", location)
        if hit:
            return verdict(Decision.REJECT, f"location:{hit}")

    # 4. Level gate. An explicit entry-level signal in the title is necessary but not
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

    # 5. No level signal in the title. The eligibility, if any, is in the JD body,
    #    which the deterministic layer does not read. Hand it to the UNCERTAIN bucket.
    return verdict(Decision.UNCERTAIN, "no_level_token_in_title")
