"""Task: propose resume edits for one scored match.

The fifth bounded model role, and the first one that composes prose. That is worth
stating plainly rather than letting it pass, because DESIGN.md §8's summary of the other
four is "the model is allowed to *read*, never to *decide*" — level reads a description
for a fact the title omitted, judge reads it for a fit judgment, inbox reads a reply and
proposes what it meant. Each returns an enum, an ordinal, or a quote. This one returns a
sentence somebody might send to an employer under their own name.

So the bound cannot be the shape of the answer, and it is not. It is three things:

**Both anchors are verbatim quotes.** `evidence` must occur in the job description and
`current_line` must occur in the resume — the `inbox` grounding rule applied at both ends.
An edit that cannot say which real requirement it is answering, or which real line it is
changing, is dropped. That is what keeps an invented requirement and an invented resume
line off the page, and it is checked in `parse_edits`, not asked for in the prompt.

**The suggestion is sanitized before it can reach a document.** A resume is compiled, so
text composed by a model is text we are about to run through a TeX engine. See
`resume/latex.py`.

**Nothing it writes is read back by anything deterministic.** `resume_suggestions` has one
reader: the page that shows it to you. This is the §8.1 lesson — the role removed there
was *more* tightly bounded than this one (an enum of keys, no free text at all) and was
still wrong, because `apply` cached its answers into `form_fields` and the rules replayed
them at every later company. A model that writes into a cache the rules read back is on
the main loop, one night later. Ask where an answer is stored, not just where it is made.

It proposes and stops. Applying an edit and assembling the PDF is `jobtracker tailor
build`, which needs no model; attaching the result to an application is a button you
press. No code path here writes bytes to a resume.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from .. import store
from ..llm import MAX_DESCRIPTION_CHARS
from ..resume import Edit
from .base import Task, TaskContext, TaskUnit, register

log = logging.getLogger("jobtracker.tasks")

# Where in a resume an edit may land. An enum rather than free text so a section cannot
# be invented, and coarse on purpose: these are the parts of a resume a job description
# can actually speak to. "Education" is deliberately absent — a description cannot tell
# you to change your degree, and a model asked for one would find something to say.
SECTIONS = ["experience", "projects", "skills", "summary"]

# How much of the resume the model is shown. A resume is one or two pages; this is
# generous and bounded, and the truncation is the task's job rather than the client's.
MAX_RESUME_CHARS = 12000

# At most this many edits per posting. Not a quality bound — a bound on how much of your
# resume one job description is allowed to rewrite. A model asked for "the changes" with
# no cap will find twenty, and a resume that moves twenty lines per posting is not a
# tailored resume, it is a different one each time.
MAX_EDITS = 6

TAILOR_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": SECTIONS},
                    "current_line": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["section", "current_line", "suggestion", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["edits"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You suggest small, factual edits to ONE candidate's resume for ONE job posting.

You are given the resume as LaTeX source and the job description. Return a list of edits.

Rules:
- `current_line` must be copied VERBATIM from the resume, character for character. It is
  the line you want changed. If you cannot copy a line exactly, do not propose the edit.
- `evidence` must be a phrase copied VERBATIM from the job description — the requirement
  this edit is answering. If the description does not say it, there is no edit to make.
- `suggestion` is the replacement for that line. Keep the candidate's own facts: you may
  re-word, re-order, and surface what is already there. You may NOT add an employer, a
  technology, a metric, a date or an achievement the resume does not already contain.
- Keep the LaTeX structure of the line you are replacing. If the line begins with \\item,
  so does yours. Do not introduce any LaTeX command that was not already in the line.
- Prefer few, strong edits. An empty list is a good answer when the resume already
  addresses the posting; say nothing rather than inventing something to say.
"""

_WS = re.compile(r"\s+")


def _flat(text: str) -> str:
    """Whitespace-normalized, for containment checks. Same helper `inbox` uses."""
    return _WS.sub(" ", (text or "")).strip()


@dataclass(frozen=True)
class Suggestions:
    """What the model proposed for one posting, after every refusal has been applied."""

    edits: list

    def as_json(self) -> str:
        return json.dumps([e.as_dict() for e in self.edits])


def parse_edits(
    text: Optional[str], resume_text: str, description: str, fmt
) -> Optional[Suggestions]:
    """The proposed edits that survive grounding, or None if none do.

    Six refusals, applied per edit, and the two that matter most are the quotes:

    1. not JSON, or not an object, or `edits` is not a list  -> no answer
    2. a section outside the enum                            -> drop the edit
    3. `evidence` absent from the job description, verbatim  -> drop the edit
    4. `current_line` absent from the resume, verbatim       -> drop the edit
    5. a suggestion the format refuses to compile            -> drop the edit
    6. a suggestion identical to the line it replaces        -> drop the edit

    Returning None for "nothing survived" rather than an empty list is deliberate, and it
    is the opposite of the call `inbox` makes. There, "this is not application news" is a
    real finding about a message that nothing else will ever resolve, so it is written.
    Here, zero edits means only that this model, this time, found nothing to say about a
    posting that is still open and still worth asking about — so it stays in the queue.
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("edits")
    if not isinstance(raw, list):
        return None

    flat_resume = _flat(resume_text)
    flat_description = _flat(description)

    kept: list = []
    for item in raw[:MAX_EDITS]:
        if not isinstance(item, dict):
            continue
        section = item.get("section")
        if section not in SECTIONS:
            continue

        # Grounded at both ends. `repair` requires a proposed slug to appear on the page
        # it was read from; this is that rule applied to a requirement and to a line.
        evidence = _flat(str(item.get("evidence") or ""))
        if not evidence or evidence not in flat_description:
            continue

        current = str(item.get("current_line") or "")
        if not current.strip() or _flat(current) not in flat_resume:
            continue
        # Verbatim in the whitespace-normalized sense above, but `apply_edits` replaces
        # exactly — so the line has to be findable in the real text too, or the edit
        # would be shown on the page and then quietly do nothing when applied.
        if current not in resume_text:
            continue

        suggestion = fmt.sanitize(str(item.get("suggestion") or ""))
        if suggestion is None or suggestion.strip() == current.strip():
            continue

        kept.append(Edit(section=section, current_line=current,
                         suggestion=suggestion, evidence=evidence))

    return Suggestions(edits=kept) if kept else None


class TailorTask(Task):
    name = "tailor"
    priority = 50
    summary = "propose resume edits for a scored match, grounded in its description"

    def unavailable_reason(self, ctx: TaskContext) -> Optional[str]:
        """Missing configuration only. A missing TeX toolchain is not one.

        Suggestions are text and need no engine; only assembling a PDF does. Reporting
        this task unavailable on a box with no LaTeX installed would withhold the whole
        feature for want of the last step in it.
        """
        if getattr(ctx, "resume_text", None) is None:
            return (
                f"no resume source — write one to {_resume_path()} "
                f"or set $JOBTRACKER_RESUME_TEX"
            )
        if not ctx.resume_text.strip():
            return "the resume source is empty"
        if getattr(ctx, "resume_format", None) is None:
            return "no resume format handles that file"
        if not ctx.resume_format.is_editable(ctx.resume_text):
            return f"that does not look like a {ctx.resume_format.name} document"
        return None

    def pending(
        self, conn, ctx: TaskContext, limit: Optional[int] = None
    ) -> list[TaskUnit]:
        rows = store.matches_needing_tailoring(conn, ctx.resume_hash, limit=limit)
        return [
            TaskUnit(
                task=self.name,
                company=row["company"],
                ats_job_id=row["ats_job_id"],
                # The resume IS the question: change it and every posting is a new unit
                # with a clean retry count, exactly as editing profile.yaml's prose
                # re-asks `judge`. The posting is already in `TaskUnit.ident`, so folding
                # it in here too would only duplicate it in the attempts key.
                unit_key=ctx.resume_hash,
                title=row["title"],
                payload={"description": row["description"]},
            )
            for row in rows
        ]

    async def run(self, unit: TaskUnit, client, ctx: TaskContext):
        text = await client.complete(
            system=SYSTEM_PROMPT,
            user=(
                f"RESUME ({ctx.resume_format.name} source)\n"
                f"{ctx.resume_text[:MAX_RESUME_CHARS]}\n\n"
                f"---\n\nJob title: {unit.title}\n"
                f"Company: {unit.company}\n\n"
                f"Job description:\n"
                f"{unit.payload['description'][:MAX_DESCRIPTION_CHARS]}"
            ),
            schema=TAILOR_SCHEMA,
            schema_name="resume_edits",
            idempotency_key=unit.idempotency_key(),
            max_tokens=1024,
        )
        return parse_edits(
            text, ctx.resume_text, unit.payload["description"], ctx.resume_format
        )

    def apply(self, conn, unit: TaskUnit, result: Suggestions, ctx: TaskContext) -> str:
        store.record_suggestions(
            conn, unit.company, unit.ats_job_id, result.as_json(),
            ctx.resume_hash, ctx.today,
        )
        log.info(
            "tailor proposes %d edit(s) at %s — %s",
            len(result.edits), unit.company, unit.title[:60],
        )
        # Bounded label: 1..MAX_EDITS, so the outcome counter stays small.
        return f"{len(result.edits)} edit(s)"


def _resume_path():
    from .. import config

    return config.RESUME_TEX


register(TailorTask())
