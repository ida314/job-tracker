"""Task: read a description for the experience level its title omitted.

The first bounded model role (DESIGN.md §8), unchanged in scope by the move into the
task queue. It answers *only* "what experience level does this description require".
Whether a role is backend, whether the company is interesting, whether you want it: all
of that stays in criteria.yaml where you can read it, diff it, and test it.

    UNCERTAIN + engineering-looking title + a cached description
        -> entry     : re-apply the *rules'* engineering gate -> MATCH or REJECT
           not_entry : REJECT
           unclear   : leave UNCERTAIN

One thing did change, and it is an improvement worth naming. The old pass counted
postings with no description as "considered, no description" — work it had queued and
could not do. Here they are simply not pending: since `resolve` stopped fetching, a
posting with no cached description is a guaranteed no-op, and calling it queued work
overstated the backlog every night.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from .. import store
from ..criteria import Criteria
from ..llm import MAX_DESCRIPTION_CHARS
from ..models import Decision, Verdict
from .base import Task, TaskContext, TaskUnit, register

log = logging.getLogger("jobtracker.tasks.level")

LEVEL_SCHEMA = {
    "type": "object",
    "properties": {
        "level": {"type": "string", "enum": ["entry", "not_entry", "unclear"]},
        "evidence": {"type": "string"},
    },
    "required": ["level", "evidence"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You read a software job description and report ONLY the experience level it requires.

Answer with one of:
  entry      — explicitly open to new/recent graduates, or asks for roughly 0-2 years,
               or is labelled new grad / university grad / early career / campus.
  not_entry  — requires more experience than a new graduate has: a minimum number of
               years above 2, or seniority (senior, staff, principal, lead, manager).
  unclear    — the description does not state a level either way.

Rules:
- Judge ONLY experience level. Do not judge whether the role is backend, interesting,
  or a good fit. Something else decides that.
- "unclear" is a correct and useful answer. Prefer it over guessing. A wrong "entry"
  wastes an application; a wrong "not_entry" hides a real job.
- An internship is not entry-level for this purpose; answer not_entry.
- Quote the exact phrase you used as evidence. If there is none, use "unclear" and
  leave evidence empty.
"""


@dataclass(frozen=True)
class LevelVerdict:
    level: str  # 'entry' | 'not_entry' | 'unclear'
    evidence: str = ""

    @property
    def usable(self) -> bool:
        return self.level in ("entry", "not_entry")


def parse_verdict(text: Optional[str]) -> Optional[LevelVerdict]:
    """Validate the model's JSON. Anything unexpected is treated as no answer.

    Constrained decoding should make this total, but a server that silently ignores
    `response_format` would otherwise let free-form prose through as a verdict — the
    exact regression that made this pass a no-op for a month. See `llm/wire.py`.
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    level = data.get("level")
    if level not in ("entry", "not_entry", "unclear"):
        return None
    return LevelVerdict(level=level, evidence=str(data.get("evidence") or "")[:300])


def looks_engineering(title: str, criteria: Criteria) -> bool:
    """Does the title carry any engineering signal at all?

    Used to scope which UNCERTAIN postings are worth reading. On the live data this
    cuts the queue from 1,537 to 674: the remainder are titles like "Field Marketer"
    and "Talent Strategist", where reading a description learns something the title
    already said.

    The cost is a genuinely-engineering title with no matching token never being read.
    That posting stays UNCERTAIN and visible in the queue rather than being silently
    rejected, which is the tradeoff this project makes everywhere else too: surface the
    blind spot, do not hide it.
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


class LevelTask(Task):
    name = "level"
    priority = 10
    summary = "read descriptions to settle UNCERTAIN postings"

    def unavailable_reason(self, ctx: TaskContext) -> Optional[str]:
        return None if ctx.criteria is not None else "criteria.yaml did not load"

    def pending(self, conn, ctx, limit=None):
        # The SQL sorts description-less rows last; we drop them outright, because with
        # no fetching in this pass they are work that cannot be done rather than work
        # waiting its turn.
        rows = store.uncertain_for_resolution(conn, limit=None)
        units: list[TaskUnit] = []
        for row in rows:
            description = row["description"] if "description" in row.keys() else None
            if not description:
                continue
            if not looks_engineering(row["title"], ctx.criteria):
                continue
            units.append(TaskUnit(
                task=self.name,
                company=row["company"],
                ats_job_id=row["ats_job_id"],
                # The level question does not change when anything else does, so the
                # unit key is a constant. A re-ask is a retry, not a new question.
                unit_key="level",
                title=row["title"],
                payload={"description": description},
            ))
            if limit is not None and len(units) >= limit:
                break
        return units

    async def run(self, unit, client, ctx):
        text = await client.complete(
            system=SYSTEM_PROMPT,
            user=(
                f"Job title: {unit.title}\n\n"
                f"Job description:\n{unit.payload['description'][:MAX_DESCRIPTION_CHARS]}"
            ),
            schema=LEVEL_SCHEMA,
            schema_name="level",
            idempotency_key=unit.idempotency_key(),
        )
        reading = parse_verdict(text)
        if reading is None or not reading.usable:
            # 'unclear' is an answer, but not one that changes anything — same outcome
            # as no answer at all, which is what None means to the runner.
            return None
        return verdict_from_level(
            unit.company, unit.ats_job_id, unit.title,
            reading.level, reading.evidence, ctx.criteria,
        )

    def apply(self, conn, unit, result, ctx):
        verdict: Verdict = result
        # Pin before recording. `check` re-derives a rules verdict from the title for
        # every posting it fetches, and the title is exactly what the rules already
        # found insufficient — so an unpinned llm verdict lasts one night. A posting you
        # have already ruled on keeps your ruling; set_override says so by returning
        # False, and then the verdict is not ours to write either.
        if not store.set_override(
            conn, verdict.company, verdict.ats_job_id, verdict.decision.value,
            ctx.today, reason=verdict.reason, decided_by="llm",
        ):
            return "yours"
        store.record_verdict(conn, verdict, ctx.today)
        if verdict.decision is Decision.MATCH:
            log.info("resolved MATCH  %s — %s", unit.company, unit.title[:60])
        return verdict.decision.value


register(LevelTask())
