"""Task: write a cover letter for one scored match, into a template you control.

The sixth bounded model role, and the second that composes prose. `tailor` was the first
and its docstring argues about why the bound cannot be the shape of the answer. This one
goes further in the same direction and has to say what replaces it, because a cover
letter is *entirely* composed: there is no `current_line` to quote back, no skeleton to
hand over unchanged, and no resume line whose absence drops the edit.

So the three bounds here are different ones:

**The model never writes LaTeX.** Not "writes it under an allowlist" — never. It returns
plain paragraphs and `letter.escape_text` turns every reserved character into its escape,
the backslash included, before anything reaches a template. There is no control sequence
left to permit or refuse. That is a stronger guarantee than `resume/latex.py` gives,
which it can afford to be because nothing here needs a macro to pass through.

**The facts are not the model's to state.** The employer, the job title and the date are
substituted by `letter.fill` from the posting row and the run's `today`. A letter
addressed to the wrong company is the one mistake a cover letter does not survive, and
the database already knows the answer — so it is never asked. The model writes argument;
it does not write who the argument is addressed to.

**Every paragraph points at a real sentence.** `evidence` must occur verbatim in the job
description or in the resume — the `inbox` grounding rule, widened by one document
because a paragraph about your own experience is grounded in the resume and a paragraph
about the role is grounded in the posting. A paragraph that cannot name the real sentence
it is answering is dropped, and a letter missing a paragraph is not written at all.

That last clause is where this differs from `tailor` on purpose. A partial set of resume
edits is useful — you read the three it found. A letter with a hole in it is not a
letter, so `parse_letter` is all-or-nothing: either every slot the template declares came
back grounded, or the unit stays pending and is asked again. `MAX_ATTEMPTS` sets aside a
posting that cannot be answered three nights running, which is the right end for one.

What it does not do is send anything. `coverletter build` compiles a PDF and writes it
under `$JOBTRACKER_LETTERS`; what reaches an employer is a file you downloaded and
attached yourself. `cover_letters` has one reader, the page that shows it to you — the
DESIGN.md 8.1 rule that `resume_suggestions` already lives inside.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from .. import keywords as kw_mod
from .. import letter as letter_mod
from .. import store
from ..llm import MAX_DESCRIPTION_CHARS
from .base import Task, TaskContext, TaskUnit, register

log = logging.getLogger("jobtracker.tasks")

# How much of the resume the model is shown, matching `tailor`. A resume is one or two
# pages; this is generous and bounded, and truncating is the task's job not the client's.
MAX_RESUME_CHARS = 12000

# One paragraph of a cover letter. Four of these is a page, which is the length a cover
# letter has to be — a model asked for a paragraph with no bound writes six sentences and
# the letter runs onto a second page nobody reads. Enforced rather than requested.
MAX_PARAGRAPH_CHARS = 900

# The most slots a template may declare. Not a limit anyone should reach: it exists so a
# malformed template full of CAPS cannot turn one posting into forty model calls.
MAX_SLOTS = 8

LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "paragraphs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "text": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["key", "text", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["paragraphs"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You write the body paragraphs of ONE cover letter, for ONE candidate and ONE job posting.

You are given the candidate's resume, the job description, and a numbered brief for each
paragraph. The brief is the instruction for that paragraph — follow it.

Return one entry per paragraph, each with:
- `key`: the paragraph's key, exactly as the brief gives it.
- `text`: the paragraph itself, as PLAIN TEXT.
- `evidence`: a phrase copied VERBATIM from the job description or from the resume — the
  real sentence this paragraph is answering. If you cannot copy one, you have nothing to
  say in that paragraph and the letter will be discarded.

Copy a SHORT, CONTIGUOUS run of words for `evidence` — one clause is plenty. Do NOT join
two separate parts of a sentence with "..." and do not tidy up the wording. It is checked
against the document character for character, and the whole letter is thrown away if it
does not match.

WRITE PLAIN TEXT ONLY. No LaTeX, no markdown, no backslash commands, no formatting marks
of any kind. Write the sentences a person would read aloud. Anything else is escaped into
literal characters and will appear in the letter as the symbols you typed.

ONLY THE CANDIDATE'S OWN FACTS. Every employer, project, technology, metric and date you
write must already be in the resume. You may select from it, summarize it and connect it
to the posting — that is the entire job. You may NOT add an employer they did not work
for, a technology they have not used, a number the resume does not state, or a claim
about their motivation you cannot read off the page. If the posting asks for something
the resume does not carry, write about what the resume does carry instead. Never say the
candidate is "passionate", "excited" or "thrilled", and never praise the company.

DO NOT WRITE THE COMPANY NAME, THE JOB TITLE OR THE DATE. They are filled in from the
application record, so anything you write there would be a second, competing copy. Refer
to "the role" and "your team" and let the letter supply the names.

Match the register of the resume: plain, specific, past tense for work already done. One
paragraph per brief, no headings, no bullet points, no salutation and no sign-off —
those are already in the letter. Keep each paragraph under 120 words.
"""


@dataclass(frozen=True)
class Paragraph:
    """One composed paragraph, after grounding."""

    key: str
    text: str
    evidence: str
    # Which document the evidence was found in — 'description' or 'resume'. Stored
    # because it is the one thing that says whether a paragraph is answering the posting
    # or describing the candidate, and the page shows it beside the text.
    source: str

    def as_dict(self) -> dict:
        return {"key": self.key, "text": self.text,
                "evidence": self.evidence, "source": self.source}


@dataclass(frozen=True)
class Letter:
    """A complete, grounded letter body: one paragraph per slot the template declares."""

    paragraphs: list

    def as_json(self) -> str:
        return json.dumps([p.as_dict() for p in self.paragraphs])

    def by_key(self) -> dict:
        return {p.key: p.text for p in self.paragraphs}


def parse_letter(
    text: Optional[str],
    template,
    description: str,
    resume_text: str,
    keywords=None,
) -> Optional[Letter]:
    """The letter, or None if it is not complete and grounded.

    All-or-nothing, which is the opposite of `parse_edits` and deliberate. Six refusals,
    and any one of them discards the whole letter rather than one paragraph:

    1. not JSON, or `paragraphs` is not a list        -> no answer
    2. a key the template did not declare             -> drop that entry
    3. `text` empty, or longer than a paragraph       -> no answer
    4. `evidence` in neither source document          -> no answer
    5. `text` carrying a term you have DENIED         -> no answer
    6. a slot the template declared and nothing filled -> no answer

    Six is the reason for the all-or-nothing rule. `letter.fill` leaves an unfilled slot
    as its placeholder prose, so a letter missing paragraph 3 compiles to a PDF with
    `RELEVANT ROLE THEME` printed in the middle of it. Writing that row down would put a
    download button on the page over a document nobody can send, and the posting would
    leave the queue — so it would never be asked again either. Returning None keeps it
    pending, and three failed nights set it aside through the ledger like anything else.

    Five differs from `tailor`'s handling of the same list for the same structural
    reason. There, a denied term drops one edit and the rest still compile, so the
    refusal is *written* as an empty proposal to drain the unit. Here there is no rest:
    one bad paragraph is a letter that cannot be built, and re-asking is the only way to
    get one that can. A model that keeps proposing a denied term will fail three times
    and be set aside, which is the correct outcome for a question this configuration
    cannot answer.
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("paragraphs")
    if not isinstance(raw, list):
        return None

    keywords = keywords if keywords is not None else kw_mod.Keywords()
    wanted = set(template.keys)

    kept: dict = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if key not in wanted or key in kept:
            continue

        body = letter_mod.flat(str(item.get("text") or ""))
        if not body or len(body) > MAX_PARAGRAPH_CHARS:
            return None

        # Grounded, in one of the two documents the letter is allowed to draw on. The
        # posting for an argument about the role, the resume for one about the candidate
        # — and `_flat` on both sides, because a quote that survives a line wrap is still
        # a quote.
        evidence = letter_mod.flat(str(item.get("evidence") or ""))
        source = letter_mod.grounded_in(
            evidence, ("description", description), ("resume", resume_text)
        )
        if source is None:
            return None

        # A technology you ruled out, in prose about to be compiled and sent. Refused
        # here in Python for the reason `tailor` refuses it here: `denied` is what "no, I
        # do not know that" is recorded as, and a prompt does not get to reconsider it.
        denied = kw_mod.terms_in(keywords.denied, body)
        if denied:
            log.debug("letter refused for denied term(s) %s", denied)
            return None

        kept[key] = Paragraph(key=key, text=body, evidence=evidence, source=source)

    if set(kept) != wanted:
        return None
    return Letter(paragraphs=[kept[k] for k in template.keys])


class CoverLetterTask(Task):
    name = "coverletter"
    priority = 60
    summary = "write a cover letter for a scored match, into your own template"

    def unavailable_reason(self, ctx: TaskContext) -> Optional[str]:
        """Missing configuration only, and a missing TeX toolchain is not one.

        The same call `tailor` makes: the paragraphs are text and need no engine. Only
        `coverletter build` needs tectonic, and it says so itself and exits 0.
        """
        error = getattr(ctx, "letter_error", None)
        if error:
            return error
        template = getattr(ctx, "letter_template", None)
        if template is None:
            return (
                "no cover-letter template — write one, or set "
                "$JOBTRACKER_COVERLETTER_TEX"
            )
        if len(template.slots) > MAX_SLOTS:
            return (
                f"that template declares {len(template.slots)} paragraphs to fill; "
                f"at most {MAX_SLOTS} are written"
            )
        # The letter argues from the resume, so without one there is nothing to argue.
        # Reported as configuration rather than answered badly: a cover letter composed
        # with no resume in front of the model is exactly the fabrication this is built
        # to prevent.
        if not (getattr(ctx, "resume_text", None) or "").strip():
            return "no resume source to write a letter from — see `tailor`'s reason"
        return None

    def pending(
        self, conn, ctx: TaskContext, limit: Optional[int] = None
    ) -> list[TaskUnit]:
        rows = store.matches_needing_a_letter(
            conn, _unit_key(ctx), limit=limit
        )
        return [
            TaskUnit(
                task=self.name,
                company=row["company"],
                ats_job_id=row["ats_job_id"],
                unit_key=_unit_key(ctx),
                title=row["title"],
                payload={"description": row["description"]},
            )
            for row in rows
        ]

    async def run(self, unit: TaskUnit, client, ctx: TaskContext):
        template = ctx.letter_template
        keywords = _keywords(ctx)
        # Stated as well as applied, for the reason `tailor` states it: the refusal in
        # `parse_letter` stands whatever the prompt says, but a model that has not been
        # told keeps writing the same word every night, and here that costs the whole
        # letter rather than one edit.
        vocabulary = ""
        if keywords.denied:
            vocabulary = (
                "TECHNOLOGIES THE CANDIDATE HAS RULED OUT — never write these:\n"
                + "\n".join(f"- {t}" for t in keywords.denied) + "\n\n"
            )
        text = await client.complete(
            system=SYSTEM_PROMPT,
            user=(
                f"RESUME\n{ctx.resume_text[:MAX_RESUME_CHARS]}\n\n"
                f"---\n\n{vocabulary}"
                f"Job title: {unit.title}\n"
                f"Company: {unit.company}\n\n"
                f"Job description:\n"
                f"{unit.payload['description'][:MAX_DESCRIPTION_CHARS]}\n\n"
                f"---\n\nPARAGRAPHS TO WRITE\n\n{_briefs(template)}"
            ),
            schema=LETTER_SCHEMA,
            schema_name="cover_letter",
            idempotency_key=unit.idempotency_key(),
            max_tokens=1536,
        )
        return parse_letter(
            text, template, unit.payload["description"], ctx.resume_text, keywords,
        )

    def apply(self, conn, unit: TaskUnit, result: Letter, ctx: TaskContext) -> str:
        store.record_letter(
            conn, unit.company, unit.ats_job_id, result.as_json(),
            _unit_key(ctx), ctx.today,
        )
        log.info(
            "cover letter written for %s — %s (%d paragraphs)",
            unit.company, unit.title[:60], len(result.paragraphs),
        )
        # Bounded label: the slot count, never the prose. An outcome label is a metric
        # attribute and a paragraph is unbounded cardinality.
        return f"{len(result.paragraphs)} paragraph(s)"


def _briefs(template) -> str:
    """The template's own comments, as the numbered brief the model is given.

    There is no prompt to edit in this repo when you want a different letter — you edit
    the template, and this is where that takes effect. The placeholder prose goes in too:
    it is the shape the template author asked for, and it carries the register a bare
    instruction does not.
    """
    out = []
    for slot in template.slots:
        holes = ", ".join(slot.placeholders[:6])
        out.append(
            f"[{slot.key}] {slot.brief}\n"
            f"    The placeholder text is: {letter_mod.flat(slot.body)}\n"
            + (f"    It asks you to supply: {holes}\n" if holes else "")
        )
    return "\n".join(out)


def _keywords(ctx: TaskContext):
    """The lists on `ctx`, or empty ones. Empty means unrestricted — see keywords.py."""
    return getattr(ctx, "keywords", None) or kw_mod.Keywords()


def _unit_key(ctx: TaskContext) -> str:
    """The question: this template, this resume, under these keyword lists.

    The template is in it for the reason the resume is in `tailor`'s — it *is* half the
    question. Rewrite a paragraph's brief and every letter written under the old one is
    an answer to something you no longer asked, so every posting becomes a new unit with
    a clean retry count.
    """
    template = getattr(ctx, "letter_template", None)
    return (
        f"{template.hash if template is not None else ''}"
        f":{getattr(ctx, 'resume_hash', '')}:{_keywords(ctx).hash}"
    )


register(CoverLetterTask())
