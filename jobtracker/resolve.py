"""The ambiguity pass: resolve UNCERTAIN postings by reading their descriptions.

DESIGN.md §8 kept the model in exactly two bounded roles, and this is the first one.
The scope is deliberately narrow — it answers *only* "what experience level does this
description require", and nothing else. Whether a role is backend, whether the company
is interesting, whether you want it: all of that stays in criteria.yaml where you can
read it, diff it, and test it.

That boundary is what makes the pass safe to enable and safe to lose. The model can be
down, slow, or wrong and the worst case is a posting stays UNCERTAIN — which is where
it already was.

Flow per posting:

    UNCERTAIN + engineering-looking title
        -> description (already cached by `check`; this pass opens no ATS connection)
        -> local model: entry | not_entry | unclear
        -> entry     : re-apply the *rules'* engineering gate -> MATCH or REJECT
           not_entry : REJECT
           unclear   : leave UNCERTAIN
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .criteria import Criteria
from .llm import LlmClient
from .models import Decision, Verdict

log = logging.getLogger("jobtracker.resolve")


@dataclass
class ResolveStats:
    considered: int = 0
    skipped_no_eng: int = 0
    no_description: int = 0
    matched: int = 0
    rejected: int = 0
    still_uncertain: int = 0

    def summary(self) -> str:
        return (
            f"{self.considered} considered · {self.matched} → match · "
            f"{self.rejected} → reject · {self.still_uncertain} still uncertain "
            f"({self.no_description} had no description)"
        )


def looks_engineering(title: str, criteria: Criteria) -> bool:
    """Does the title carry any engineering signal at all?

    Used to scope which UNCERTAIN postings are worth reading. On the live data this
    cuts the queue from 1,537 to 674: the remainder are titles like "Field Marketer"
    and "Talent Strategist", where fetching a description spends a rate-limited ATS
    request to learn something the title already said.

    The cost is a title like "Member of Technical Staff" — genuinely engineering, no
    matching token — never being read. That posting stays UNCERTAIN and visible in
    the queue rather than being silently rejected, which is the tradeoff this project
    makes everywhere else too: surface the blind spot, do not hide it.
    """
    lowered = title.lower()
    return bool(
        criteria.first_hit("engineering_terms", lowered)
        or criteria.first_hit("role_type_include", lowered)
    )


def verdict_from_level(
    company: str,
    ats_job_id: str,
    title: str,
    level: str,
    evidence: str,
    criteria: Criteria,
) -> Optional[Verdict]:
    """Turn a level reading into a verdict, or None to leave the posting alone.

    Note what this does NOT do: it never lets the model decide that a role is
    on-target. `entry` only earns a MATCH after the *rules'* engineering gate agrees,
    the same gate match.py applies to a title carrying an explicit level token. The
    model supplies one missing fact; the criteria still decide what to do with it.
    """
    if level == "not_entry":
        return Verdict(company, ats_job_id, Decision.REJECT,
                       f"llm:not_entry:{evidence[:80]}", "llm")

    if level == "entry":
        lowered = title.lower()
        role = criteria.first_hit("role_type_include", lowered)
        eng = role or criteria.first_hit("engineering_terms", lowered)
        if eng:
            detail = "role:" + role if role else "eng:generic"
            return Verdict(company, ats_job_id, Decision.MATCH,
                           f"llm:entry+{detail}", "llm")
        # Entry level but not an engineering role — the same conclusion match.py
        # reaches for "Finance Associate".
        return Verdict(company, ats_job_id, Decision.REJECT,
                       "llm:entry+non_engineering", "llm")

    return None  # 'unclear' — no new information, leave the verdict untouched


def resolve_postings(
    rows,
    criteria: Criteria,
    client: LlmClient,
) -> tuple[list[Verdict], ResolveStats]:
    """Read descriptions for `rows` and return the verdicts that changed.

    A pure read. `check` caches the description for every match/uncertain posting, so
    by the time this runs the text is already in `state.db` and the only socket opened
    is to the local model. That is what makes the pass offline with respect to the
    ATSes: a throttled board can no longer shrink the queue this pass considers, and
    the function needs neither a network nor a database to test.
    """
    stats = ResolveStats()
    verdicts: list[Verdict] = []

    for row in rows:
        title = row["title"]
        company_name = row["company"]

        if not looks_engineering(title, criteria):
            stats.skipped_no_eng += 1
            continue

        stats.considered += 1
        description = row["description"] if "description" in row.keys() else None

        if not description:
            stats.no_description += 1
            stats.still_uncertain += 1
            continue

        reading = client.classify_level(title, description)
        if reading is None or not reading.usable:
            stats.still_uncertain += 1
            continue

        verdict = verdict_from_level(
            company_name, row["ats_job_id"], title,
            reading.level, reading.evidence, criteria,
        )
        if verdict is None:
            stats.still_uncertain += 1
            continue

        verdicts.append(verdict)
        if verdict.decision is Decision.MATCH:
            stats.matched += 1
            log.info("resolved MATCH  %s — %s", company_name, title[:60])
        else:
            stats.rejected += 1

    return verdicts, stats
