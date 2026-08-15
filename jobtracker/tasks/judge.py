"""Task: judge one posting against the candidate profile.

The second bounded model role, and the smaller one. Where `level` decides whether a
posting is *on-target*, this decides which of the on-target ones is *urgent* — and it
does so without ever being shown a second posting, returning a score, or returning an
order. Three labelled ordinals and a sentence; `rank.py` turns those into a number using
weights from profile.yaml, in Python, where the arithmetic can be diffed and tested.

Ordinals rather than 0-100 because absolute numeric scores from an LLM cluster in a
narrow band and shift whenever the prompt or model changes, which would silently re-rank
the whole queue on an unrelated edit.

Scoring itself is deliberately NOT a task. It needs no model, it must run on every
invocation whether or not one is reachable, and it is arithmetic over rows this task has
already produced — so it stays in `cmd_rank`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from .. import store
from ..llm import MAX_DESCRIPTION_CHARS
from .base import Task, TaskContext, TaskUnit, register

ORDINAL = ["none", "weak", "moderate", "strong"]
RISK = ["low", "medium", "high"]

RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "backend_fit": {"type": "string", "enum": ORDINAL},
        "growth": {"type": "string", "enum": ORDINAL},
        "entry_risk": {"type": "string", "enum": RISK},
        "why": {"type": "string"},
    },
    "required": ["backend_fit", "growth", "entry_risk", "why"],
    "additionalProperties": False,
}

# Note what this asks for and what it does not. Three labelled ordinals and a sentence
# — never a score, never a rank, never a comparison. The model judges one posting in
# isolation and has no idea another exists (DESIGN.md §3.2).
RANK_SYSTEM_PROMPT = """\
You judge how well ONE job posting fits ONE candidate's stated goals.

You are given the candidate's profile and a single job description. Answer four
questions about THIS posting only. You are not ranking, comparing, or choosing —
another system does that with your answers.

  backend_fit — how well the ACTUAL WORK described matches the candidate's target
                roles. Judge the day-to-day engineering described in the body, not
                the job title and not the company's reputation.
                  strong   — the core work is squarely what they are looking for
                  moderate — adjacent or partially on-target
                  weak     — mostly off-target with some overlap
                  none     — a different discipline entirely

  growth      — evidence in the description of learning and mentorship: named senior
                engineers or mentors, structured onboarding, a new-grad or early-career
                program, explicit teaching, breadth of systems they would touch.
                  strong   — the posting concretely describes these
                  moderate — implied but not stated
                  weak     — little sign either way
                  none     — the posting reads as "deliver alone, no support"

  entry_risk  — the risk that this is NOT actually open to a new graduate, despite
                its title. Look for required years of experience, "proven track
                record", ownership of a whole system, or on-call leadership.
                  low      — clearly open to new grads
                  medium   — ambiguous
                  high     — reads as needing real prior experience

  why         — ONE sentence, under 200 characters, citing something specific from
                the description. Not a summary of the role; the reason for your
                judgment. This is shown to the candidate, so make it useful.

Judge only what the description says. If it is vague, say so through the labels —
"moderate" and "medium" are honest answers and are preferred over guessing.
"""


@dataclass(frozen=True)
class RankJudgment:
    backend_fit: str
    growth: str
    entry_risk: str
    why: str = ""


def parse_judgment(text: Optional[str]) -> Optional[RankJudgment]:
    """Validate the model's JSON. Anything off-enum is treated as no answer.

    Same reasoning as `level.parse_verdict`: a server that silently ignored
    `response_format` would otherwise let prose through as a judgment. Rejecting here
    means such a server produces no ranking rather than a fabricated one.
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    fit, growth = data.get("backend_fit"), data.get("growth")
    risk = data.get("entry_risk")
    if fit not in ORDINAL or growth not in ORDINAL or risk not in RISK:
        return None
    return RankJudgment(
        backend_fit=fit,
        growth=growth,
        entry_risk=risk,
        why=str(data.get("why") or "")[:300],
    )


class JudgeTask(Task):
    name = "judge"
    priority = 20
    summary = "judge open matches against profile.yaml"

    def unavailable_reason(self, ctx: TaskContext) -> Optional[str]:
        if ctx.profile is None:
            return "profile.yaml did not load"
        return None

    def pending(self, conn, ctx, limit=None):
        rows = store.matches_needing_judgment(
            conn, ctx.profile.prose_hash, limit=limit
        )
        return [
            TaskUnit(
                task=self.name,
                company=row["company"],
                ats_job_id=row["ats_job_id"],
                # The prose hash IS the question. Edit the prose and every posting
                # becomes a new unit, with its retry count reset — correct, because a
                # failure answering the old question says nothing about the new one.
                unit_key=ctx.profile.prose_hash,
                title=row["title"],
                payload={"description": row["description"]},
            )
            for row in rows
        ]

    async def run(self, unit, client, ctx):
        text = await client.complete(
            system=RANK_SYSTEM_PROMPT,
            user=(
                f"CANDIDATE PROFILE\n{ctx.profile.prose}\n\n"
                f"---\n\nJob title: {unit.title}\n\n"
                f"Job description:\n{unit.payload['description'][:MAX_DESCRIPTION_CHARS]}"
            ),
            schema=RANK_SCHEMA,
            schema_name="ranking",
            idempotency_key=unit.idempotency_key(),
        )
        return parse_judgment(text)

    def apply(self, conn, unit, result, ctx):
        store.record_judgment(
            conn, unit.company, unit.ats_job_id, result,
            ctx.profile.prose_hash, ctx.today,
        )
        return result.backend_fit


register(JudgeTask())
