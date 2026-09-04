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

import dataclasses
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from .. import keywords as kw_mod
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

# The most technologies one posting may ask you about. A description names a dozen and a
# model asked for "the gaps" will list all of them; a page carrying twelve new questions
# per job is one nobody answers. Small enough that a term appearing here is a claim the
# model made deliberately, which is what "strongly recommend" has to mean to be useful.
MAX_FLAGGED = 4

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
        "flagged": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "evidence": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["term", "evidence", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["edits", "flagged"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You suggest small, factual edits to ONE candidate's resume for ONE job posting.

You are given the resume as LaTeX source, the job description, and — when the candidate
has written one — the list of technologies they actually know. Return edits, and a
separate list of technologies you wanted to use and were not allowed to.

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

The technology list, when one is given, is the ONLY vocabulary of technology names you
may write. A name that is not in it and not already in the resume may not appear in a
suggestion, however clearly the posting asks for it — the candidate has to defend every
word of this in an interview, and you cannot know what they have used.

When the posting asks for something genuinely important that the list does not carry,
say so in `flagged` instead of writing it:

- `term` is the technology, exactly as the job description spells it.
- `evidence` is a phrase copied VERBATIM from the job description asking for it.
- `why` is one short sentence on what it would add to this application.

Flag only what you would strongly recommend. An empty `flagged` list is the normal
answer. Never flag something already in the technology list or already in the resume,
and never flag a term merely because the posting mentions it.
"""

_WS = re.compile(r"\s+")


def _flat(text: str) -> str:
    """Whitespace-normalized, for containment checks. Same helper `inbox` uses."""
    return _WS.sub(" ", (text or "")).strip()


@dataclass(frozen=True)
class Flag:
    """A technology the model wanted and your keyword list does not carry.

    Grounded exactly as an edit is: `term` and `evidence` both have to occur in the job
    description. `why` is the one piece of free text — it is shown beside a decision you
    are about to make and nothing reads it back, which is the §8.1 test.
    """

    term: str
    evidence: str
    why: str

    def as_dict(self) -> dict:
        return {"term": self.term, "evidence": self.evidence, "why": self.why}


@dataclass(frozen=True)
class Suggestions:
    """What the model proposed for one posting, after every refusal has been applied."""

    edits: list
    flagged: list = dataclasses.field(default_factory=list)

    def as_json(self) -> str:
        return json.dumps([e.as_dict() for e in self.edits])

    def flagged_json(self) -> str:
        return json.dumps([f.as_dict() for f in self.flagged])


def _parse_flags(raw, description: str, resume_text: str, keywords) -> list:
    """The flagged terms that survive grounding, capped at `MAX_FLAGGED`.

    Four refusals, and they exist because a question you are asked is a question you have
    to answer — noise here is not free the way a dropped edit is.

    1. `term` not a term (empty, or longer than a technology name)  -> drop
    2. `term` or `evidence` absent from the job description         -> drop
    3. the term is already in the resume, or already ruled on       -> drop
    4. a duplicate of a term already kept                           -> drop

    Rule 2 is the grounding rule at both ends again: an ungrounded flag is a technology
    nobody asked for, being proposed for a resume it would then be on. Rule 3 is what
    keeps a decision from being re-asked — `denied` is a ruling you made, and re-surfacing
    it every night would make excluding a term feel like it did nothing.
    """
    if not isinstance(raw, list):
        return []
    kept: list = []
    seen: set = set()
    for item in raw:
        if len(kept) >= MAX_FLAGGED:
            break
        if not isinstance(item, dict):
            continue
        try:
            term = kw_mod.validate_term(str(item.get("term") or ""))
        except kw_mod.RefusedTerm:
            continue
        fold = term.casefold()
        if fold in seen or keywords.known(term):
            continue
        if not kw_mod.occurs(term, description):
            continue
        # Already on the resume, so there is nothing to ask about. Checked against the
        # source text rather than the allowed list because the two are different claims:
        # the list says what you will vouch for, the resume says what is already printed.
        if kw_mod.occurs(term, resume_text):
            continue
        evidence = _flat(str(item.get("evidence") or ""))
        if not evidence or evidence not in _flat(description):
            continue
        seen.add(fold)
        kept.append(Flag(term=term, evidence=evidence,
                         why=_flat(str(item.get("why") or ""))[:300]))
    return kept


def parse_edits(
    text: Optional[str], resume_text: str, description: str, fmt, keywords=None
) -> Optional[Suggestions]:
    """The proposed edits that survive grounding, or None if none do.

    Six refusals, applied per edit, and the two that matter most are the quotes:

    1. not JSON, or not an object, or `edits` is not a list  -> no answer
    2. a section outside the enum                            -> drop the edit
    3. `evidence` absent from the job description, verbatim  -> drop the edit
    4. `current_line` absent from the resume, verbatim       -> drop the edit
    5. a suggestion the format refuses to compile            -> drop the edit
    6. a suggestion identical to the line it replaces        -> drop the edit
    7. a suggestion carrying a term you have DENIED          -> drop the edit

    Seven is the keyword guard, and it is deliberately the only one of the two halves
    that refuses. `allowed` steers the prompt, because a vocabulary is prose and a model
    reads prose; `denied` is applied here, in Python, because a ruling you made about a
    technology you do not know is not something a prompt gets to reconsider. A term you
    have not ruled on yet does *not* drop the edit — it marks it, and the edit is held out
    of the compile by `keywords.split_edits` until you decide. Dropping it there would
    throw away the work the question is about.

    Returning None for "nothing survived" rather than an empty list is deliberate, and it
    is the opposite of the call `inbox` makes. There, "this is not application news" is a
    real finding about a message that nothing else will ever resolve, so it is written.
    Here, zero edits means only that this model, this time, found nothing to say about a
    posting that is still open and still worth asking about — so it stays in the queue.

    **Zero edits and a flag is not nothing**, and that is the case the keyword lists
    create: the honest answer to "we need Kafka" from a model that may not write Kafka is
    an empty edit list and a question. Written, because a question nobody records is one
    that is asked again tomorrow and every night after, and because answering it moves
    `keywords_hash` — which re-asks this posting, now with the term in the vocabulary.

    **Zero edits because rule 7 fired is also not nothing**, and it is the reason
    `refused` exists rather than the emptiness of `kept` being read directly. An edit
    dropped for a denied term is a *settled* outcome: you ruled the technology out, the
    model proposed it anyway, and there is nothing about tomorrow that changes either
    fact. Returning None there would leave the posting pending, and the next run would
    re-ask the same question, get the same answer, and drop it again — forever, at one
    model call a night. A dropped-for-denied answer is written as an empty proposal
    instead, which drains the unit and leaves nothing on the page.
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

    keywords = keywords if keywords is not None else kw_mod.Keywords()
    flat_resume = _flat(resume_text)
    flat_description = _flat(description)

    kept: list = []
    refused = False
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

        # A term you ruled out, in a line about to be compiled. Dropped rather than held,
        # because there is no question left to ask: `denied` is what "no, I do not know
        # that" is recorded as, and it applies at every posting, forever.
        denied = kw_mod.terms_in(keywords.denied, suggestion)
        if denied:
            log.debug("dropped an edit carrying denied term(s) %s", denied)
            refused = True
            continue

        kept.append(Edit(section=section, current_line=current,
                         suggestion=suggestion, evidence=evidence))

    flagged = _parse_flags(data.get("flagged"), description, resume_text, keywords)
    if not kept and not flagged and not refused:
        return None
    return Suggestions(edits=kept, flagged=flagged)


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
        rows = store.matches_needing_tailoring(
            conn, ctx.resume_hash, _keywords(ctx).hash, limit=limit
        )
        return [
            TaskUnit(
                task=self.name,
                company=row["company"],
                ats_job_id=row["ats_job_id"],
                # The resume IS the question: change it and every posting is a new unit
                # with a clean retry count, exactly as editing profile.yaml's prose
                # re-asks `judge`. The posting is already in `TaskUnit.ident`, so folding
                # it in here too would only duplicate it in the attempts key.
                #
                # The keyword lists are the other half of the question — `allowed` is in
                # the prompt and `denied` is a refusal applied to the answer — so ruling
                # on a term re-keys every posting, exactly as a rewritten resume does.
                unit_key=_unit_key(ctx),
                title=row["title"],
                payload={"description": row["description"]},
            )
            for row in rows
        ]

    async def run(self, unit: TaskUnit, client, ctx: TaskContext):
        keywords = _keywords(ctx)
        # Omitted entirely when there is no list, rather than sent as an empty one. A
        # prompt that says "the technologies the candidate knows are: (none)" is a
        # statement about the candidate; the absence of the block is a statement about
        # the configuration, which is what an empty file actually means.
        vocabulary = (
            f"TECHNOLOGIES THE CANDIDATE KNOWS — the only ones you may write:\n"
            f"{keywords.prompt_block()}\n\n"
            if keywords.restricted else ""
        )
        # The refusals, stated as well as applied. `denied` is enforced in `parse_edits`
        # whatever the prompt says — that is the point of it being Python — but a model
        # that has not been told keeps proposing the same word every night, and every one
        # of those is a call spent on an answer that is thrown away.
        if keywords.denied:
            vocabulary += (
                "TECHNOLOGIES THE CANDIDATE HAS RULED OUT — never write these, and never "
                "flag them:\n"
                + "\n".join(f"- {t}" for t in keywords.denied) + "\n\n"
            )
        text = await client.complete(
            system=SYSTEM_PROMPT,
            user=(
                f"RESUME ({ctx.resume_format.name} source)\n"
                f"{ctx.resume_text[:MAX_RESUME_CHARS]}\n\n"
                f"---\n\n{vocabulary}"
                f"Job title: {unit.title}\n"
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
            text, ctx.resume_text, unit.payload["description"], ctx.resume_format,
            keywords,
        )

    def apply(self, conn, unit: TaskUnit, result: Suggestions, ctx: TaskContext) -> str:
        store.record_suggestions(
            conn, unit.company, unit.ats_job_id, result.as_json(),
            ctx.resume_hash, ctx.today,
            flagged=result.flagged_json(), keywords_hash=_keywords(ctx).hash,
        )
        log.info(
            "tailor proposes %d edit(s) and flags %d term(s) at %s — %s",
            len(result.edits), len(result.flagged), unit.company, unit.title[:60],
        )
        # Bounded label: at most MAX_EDITS x MAX_FLAGGED values, so the outcome counter
        # stays small. The terms themselves are deliberately not in it — an outcome label
        # is a metric attribute, and a technology name is unbounded cardinality.
        return f"{len(result.edits)} edit(s), {len(result.flagged)} flagged"


def _keywords(ctx: TaskContext):
    """The lists on `ctx`, or empty ones. Empty means unrestricted — see keywords.py.

    Defaulted here rather than required, so a caller that predates the lists — a test, a
    context built by hand — behaves exactly as it did before them.
    """
    return getattr(ctx, "keywords", None) or kw_mod.Keywords()


def _unit_key(ctx: TaskContext) -> str:
    """The question: this resume, under these keyword lists."""
    return f"{ctx.resume_hash}:{_keywords(ctx).hash}"


def _resume_path():
    from .. import config

    return config.RESUME_TEX


register(TailorTask())
