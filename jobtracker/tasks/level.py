"""Task: read a description for the experience level its title omitted.

The first bounded model role (DESIGN.md §8), unchanged in scope by the move into the
task queue. It answers *only* "what experience level does this description require".
Whether a role is backend, whether the company is interesting, whether you want it: all
of that stays in criteria.yaml where you can read it, diff it, and test it.

    UNCERTAIN + engineering-looking title + a cached description
        -> entry     : re-apply the *rules'* engineering gate -> MATCH or REJECT
           not_entry : REJECT
           unclear   : leave UNCERTAIN

Every grounded answer, `unclear` included, is also written to `level_readings` with a
hash of the text the model was shown. `unclear` is an answer — the model read the page
and the page does not say — so it is recorded and the unit is done, the way `inbox`
records "nothing here". It used to read as no answer, which spent three model calls on
every such posting and then set it aside as blocked, beside the transport failures.

One thing did change, and it is an improvement worth naming. The old pass counted
postings with no description as "considered, no description" — work it had queued and
could not do. Here they are simply not pending: since `resolve` stopped fetching, a
posting with no cached description is a guaranteed no-op, and calling it queued work
overstated the backlog every night. A feed row whose description is assembled metadata
(`description_source = 'feed'`) is the same case: there is no prose to read a level off.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Optional

from .. import store
from ..criteria import Criteria
from ..letter import flat
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


@dataclass(frozen=True)
class LevelAnswer:
    """What `run` hands `apply`: the reading to record, and the verdict it implies.

    `verdict` is None for `unclear`, which is recorded and changes nothing. A separate
    type rather than a bare Verdict because the reading is written for every grounded
    answer and the verdict only for some — returning None for `unclear`, as this once
    did, told the runner "no answer" about a posting the model had in fact read.
    """

    reading: LevelVerdict
    source_hash: str
    verdict: Optional[Verdict] = None


def shown_text(title: str, description: str) -> str:
    """Exactly what the model is shown: the prompt's user turn and the grounding corpus.

    One function builds both, so the text a quote is checked against can never drift
    from the text the model was handed. Grounding against the full description would
    accept a quote from past the truncation point, one the model cannot have read.
    """
    return (
        f"Job title: {title}\n\n"
        f"Job description:\n{(description or '')[:MAX_DESCRIPTION_CHARS]}"
    )


def source_hash(shown: str) -> str:
    """`sha256[:16]` of the text the model was shown — `level_readings.source_hash`.

    Of `shown`, not of the description: the title is part of what was read, and so is
    the truncation, so either moving is a different question.
    """
    return hashlib.sha256(shown.encode("utf-8")).hexdigest()[:16]


def grounded(evidence: str, shown: str) -> bool:
    """Does `evidence` occur in `shown`, whitespace-normalized and case-insensitive?

    `letter.flat` is the normalizer `tailor`, `inbox` and `coverletter` already use; the
    casefold is the one thing added, because a model that quotes "New Grad" off a title
    reading "new grad" has still quoted it.
    """
    quote = flat(evidence).casefold()
    return bool(quote) and quote in flat(shown).casefold()


def parse_verdict(text: Optional[str], shown: str) -> Optional[LevelVerdict]:
    """Validate the model's JSON. Anything unexpected is treated as no answer.

    Constrained decoding should make this total, but a server that silently ignores
    `response_format` would otherwise let free-form prose through as a verdict — the
    exact regression that made this pass a no-op for a month. See `llm/wire.py`.

    `shown` is the text the model was given (`shown_text`). A usable reading — `entry`
    or `not_entry` — must quote evidence that occurs in it, or it is no answer: the
    quote is the only thing in the reply that can be checked, and an invented phrase
    would otherwise pin a MATCH or REJECT that nothing on the page supports. `unclear`
    with empty evidence is still a valid reading; it changes nothing.
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
    evidence = flat(str(data.get("evidence") or ""))
    if level != "unclear" and not grounded(evidence, shown):
        return None
    return LevelVerdict(level=level, evidence=evidence[:300])


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


def _entry_reason(detail: str, evidence: str) -> str:
    """`llm:entry+<detail>:<evidence[:80]>`, the shape `not_entry` already had.

    An `entry` pin used to store no quote at all, so it could not be audited later
    against the text it claimed to have read. Rows written before this keep the bare
    `llm:entry+<detail>` form; every reader displays the string and none parses it, so
    both render. Empty evidence (a direct call, never a grounded reading) keeps the
    bare form rather than ending in a dangling colon.
    """
    reason = f"llm:entry+{detail}"
    return f"{reason}:{evidence[:80]}" if evidence else reason


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
                           _entry_reason(detail, evidence), "llm")
        # Entry level but not an engineering role — the same conclusion match.py
        # reaches for "Finance Associate".
        return Verdict(company, ats_job_id, Decision.REJECT,
                       _entry_reason("non_engineering", evidence), "llm")

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
            description = row["description"]
            if not description:
                continue
            # A feed's assembled metadata — employer, location, a date — is not a
            # description. There is no level to read in it, so it is not work.
            if row["description_source"] == "feed":
                continue
            if not looks_engineering(row["title"], ctx.criteria):
                continue
            # Already read, off this exact text. Hashed here rather than in SQL because
            # `shown_text` is the one builder of what the model sees; a description
            # upgrade moves the hash and the posting is asked again.
            if row["reading_hash"] == source_hash(shown_text(row["title"], description)):
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
        # Built once: the prompt and the grounding check read the same string.
        shown = shown_text(unit.title, unit.payload["description"])
        text = await client.complete(
            system=SYSTEM_PROMPT,
            user=shown,
            schema=LEVEL_SCHEMA,
            schema_name="level",
            idempotency_key=unit.idempotency_key(),
        )
        reading = parse_verdict(text, shown)
        if reading is None:
            # Malformed, ungrounded, or nothing at all: no answer, and the runner counts
            # it as a failure. Only this is None — 'unclear' is an answer.
            return None
        verdict = None
        if reading.usable:
            verdict = verdict_from_level(
                unit.company, unit.ats_job_id, unit.title,
                reading.level, reading.evidence, ctx.criteria,
            )
        return LevelAnswer(reading=reading, source_hash=source_hash(shown),
                           verdict=verdict)

    def apply(self, conn, unit, result, ctx):
        answer: LevelAnswer = result
        # The reading first, and whatever follows: it records what the model read, which
        # stays true whether or not the verdict is ours to write.
        store.record_level_reading(
            conn, unit.company, unit.ats_job_id, answer.reading.level,
            answer.reading.evidence, answer.source_hash, ctx.today,
        )
        verdict = answer.verdict
        if verdict is None:
            return "unclear"
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
