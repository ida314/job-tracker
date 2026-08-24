"""Task: work out what goes in every box on an application form.

The fourth bounded model role (DESIGN.md §8 numbers it fourth; this docstring said third
until 2026-08-16), and the most constrained of them all. Where `level` reads a description
for a fact and `judge` reads it for a fit judgment, this one only ever *points at an answer
you already wrote*. It cannot produce prose, and there is no code
path here by which a sentence the model composed reaches a form field. An unanswered
free-text question is a gap, exactly like an unanswered dropdown.

    the form's questions        (Greenhouse publishes them; others are learned from
        │                        the DOM on the first browser visit)
        ├─ exact  canonical ATS field name -> identity          no model call
        ├─ alias  the same question, already answered elsewhere no model call
        ├─ model  "which of these answers, if any, answers this?"
        └─ gap    nothing fits -> recorded, and appended to answers.yaml for you

The gap arm is the point of the whole design. A field the system cannot fill is not
silently skipped: it is named, attributed to the companies that asked it, and turned
into a stub you fill in once — after which every future prefill knows it. The bank of
answers grows by being used.

Nothing here submits anything, and nothing here opens a browser. This task produces a
*plan*; `browser.py` is what carries it to a page, and only when you ask it to.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, replace
from typing import Optional

from .. import resumes, store
from ..answers import normalize_label, slugify
from ..models import FormField
from ..sources import get_source
from .base import Task, TaskContext, TaskUnit, register

log = logging.getLogger("jobtracker.tasks.prefill")

# ATS field names that mean the same thing everywhere. The exact pass.
CANONICAL_FIELDS = {
    "first_name": "first_name",
    "last_name": "last_name",
    "email": "email",
    "phone": "phone",
    "location": "location",
    "school": "school",
    "degree": "degree",
}

# Normalized question text that means an identity field. This is what makes the DOM path
# work: a browser-discovered input has no canonical name, only a visible label, and
# "Email Address" on an Ashby form is the same question as `email` on a Greenhouse one.
LABEL_ALIASES = {
    "first name": "first_name",
    "given name": "first_name",
    "last name": "last_name",
    "family name": "last_name",
    "surname": "last_name",
    "preferred name": "preferred_name",
    "email": "email",
    "email address": "email",
    "e mail": "email",
    "phone": "phone",
    "phone number": "phone",
    "mobile phone": "phone",
    "telephone": "phone",
    "location": "location",
    "current location": "location",
    "city": "location",
    "linkedin": "linkedin",
    "linkedin profile": "linkedin",
    "linkedin url": "linkedin",
    "github": "github",
    "github profile": "github",
    "github url": "github",
    "website": "website",
    "personal website": "website",
    "portfolio": "website",
    "pronouns": "pronouns",
    "school": "school",
    "university": "school",
    "degree": "degree",
}

# File inputs, by canonical name. A file field is filled from a path in answers.yaml
# rather than from an answer, and only a real browser can act on it.
RESUME_FIELDS = {"resume", "cv"}
COVER_LETTER_FIELDS = {"cover_letter"}

# Questions every employer asks eventually. A first sighting at one company is still an
# answer worth writing once, so these are generic at count 1 — the list exists only to
# stop the very first sighting of `work_authorization` being filed under whichever
# employer happened to ask first.
COMMON_QUESTION_KEYS = frozenset({
    "work_authorization", "sponsorship", "sponsorship_required", "visa_status",
    "start_date", "salary_expectation", "notice_period", "how_did_you_hear",
    "current_employer", "country_of_residence",
    # The EEO block. Asked by essentially every US employer, worth answering once.
    "gender", "race", "ethnicity", "hispanic_latino", "veteran_status",
    "disability_status",
    "resume", "cover_letter",
})

# Everything that is generic by construction rather than by observation.
GENERIC_KEYS = (
    frozenset(CANONICAL_FIELDS.values())
    | frozenset(LABEL_ALIASES.values())
    | COMMON_QUESTION_KEYS
)

MATCH_SCHEMA_NAME = "question_match"

MATCH_SYSTEM_PROMPT = """\
You match a question from a job-application form to a bank of answers the candidate has
already written.

You are given one question and a list of answer keys. Reply with the ONE key whose stored
answer genuinely answers that question, or "none".

Rules:
- You are matching, not writing. You never compose an answer, and you never see the
  stored text — only the keys. If no key clearly fits, the answer is "none".
- "none" is a correct and useful answer, and it is the right one whenever you are
  unsure. A wrong match puts the wrong text into a real job application; "none" just
  asks the candidate to type it once.
- Match on meaning, not wording. "Who is your current employer?" and
  "current_employer" are the same question. "Why do you want to work here?" and
  "why_backend" are NOT — one is about the company, the other about a discipline.
- Never match a question asking for something specific to this employer to a generic
  key unless the key is explicitly about that.
"""


def match_schema(keys: list[str]) -> dict:
    """A schema that can only produce a key we already hold, or "none".

    Constrained decoding does the enforcement, and the parser re-checks — the same
    belt-and-braces as everywhere else in this pipeline, for the same reason: a server
    that ignores `response_format` must produce no match rather than a fabricated one.
    """
    return {
        "type": "object",
        "properties": {"question_key": {"type": "string", "enum": [*keys, "none"]}},
        "required": ["question_key"],
        "additionalProperties": False,
    }


def parse_match(text: Optional[str], keys: set[str]) -> Optional[str]:
    """The chosen key, or None for "no match" and for anything unexpected."""
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    chosen = data.get("question_key")
    if not isinstance(chosen, str) or chosen not in keys:
        return None
    return chosen


@dataclass
class PlanEntry:
    """One field, and what we decided goes in it."""

    form_key: str
    label: str
    type: str
    required: bool
    value: Optional[str] = None
    question_key: Optional[str] = None
    source: str = "gap"  # exact | alias | model | file | gap
    options: tuple = ()
    group: str = ""
    option: str = ""

    @property
    def filled(self) -> bool:
        return self.value is not None

    def as_dict(self) -> dict:
        return {
            "form_key": self.form_key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "value": self.value,
            "question_key": self.question_key,
            "source": self.source,
            # Carried so a stored plan can be re-checked against the field's vocabulary
            # rather than trusted. `browser._plan_index` lets a plan value beat a fresh
            # `resolve_field`, so without this an option list that has since changed —
            # or a value planned when the list was unknown — reaches the form unread.
            "options": list(self.options),
            "group": self.group,
            "option": self.option,
        }


@dataclass
class PrefillResult:
    entries: list[PlanEntry] = field(default_factory=list)
    form_source: str = "greenhouse-api"

    @property
    def gaps(self) -> list[PlanEntry]:
        # 'alternative' is not a gap. Greenhouse renders one question as two inputs when
        # either will satisfy it — "Resume/CV" is a file input *and* a textarea — and
        # having attached the file, the textarea is not something to go and ask about.
        out, seen = [], set()
        for e in self.entries:
            if e.filled or e.source == "alternative":
                continue
            # A checkbox set is one question with several boxes, so it is one gap.
            ident = e.group or e.form_key
            if ident in seen:
                continue
            seen.add(ident)
            out.append(e)
        return out

    @property
    def summary(self) -> str:
        return f"{len(self.entries) - len(self.gaps)}/{len(self.entries)}"


def match_option(value: str, options) -> Optional[str]:
    """The option that `value` selects, or None if it selects nothing.

    A select whose stored answer is not one of its options is a gap, not a fill. Typing
    "Yes" into a dropdown offering "Authorized" and "Not authorized" would either fail
    silently or pick the wrong entry, and both are worse than being asked.

    No options means no vocabulary to check against, and this returns the value — which
    is right for a text box and, for a dropdown, is a statement that we could not check.
    `vocabulary_known` is what separates the two; see its note for why only the model
    pass is held to it.
    """
    if not options:
        return value
    wanted = normalize_label(value)
    for option in options:
        if normalize_label(option) == wanted:
            return option
    return None


def vocabulary_known(entry: PlanEntry) -> bool:
    """Whether we know what this field will accept.

    A dropdown whose options we hold can have an answer checked against them. A
    `combobox` — which is what every Greenhouse dropdown is now — renders its menu in
    JavaScript, so a DOM reading finds no options at all, and `match_option` then waves
    any string through. That is how `location` = "New York, New York" was typed into a
    phone country selector: not a bad guess exactly, a guess nothing was in a position
    to check.

    Only the **model** pass is held to this. An `exact` canonical match, or an alias the
    user wrote themselves, is a person saying "this answer belongs in this field", and
    refusing that would make every unlisted dropdown permanently unanswerable. The rule
    is the existing one — "a dropdown that does not offer our answer is a gap" — extended
    to "a dropdown whose offer we cannot see".
    """
    return bool(entry.options) or entry.type not in ("select", "multiselect", "combobox")


def resolve_field(field_: FormField, answers, alias_map: dict) -> PlanEntry:
    """Everything that can be decided without a model. `source='gap'` means ask one."""
    entry = PlanEntry(
        form_key=field_.key,
        label=field_.label,
        type=field_.type,
        required=field_.required,
        options=field_.options,
        group=field_.group,
        option=field_.option,
    )
    lowered = field_.key.lower()

    if field_.is_file:
        entry.source = "file"
        if lowered in RESUME_FIELDS or "resume" in lowered or "cv" in lowered:
            entry.question_key = "resume"
            entry.value = str(answers.resume) if answers.resume else None
        elif lowered in COVER_LETTER_FIELDS or "cover" in lowered:
            entry.question_key = "cover_letter"
            entry.value = str(answers.cover_letter) if answers.cover_letter else None
        if entry.value is None:
            entry.source = "gap"
        return entry

    canonical = CANONICAL_FIELDS.get(lowered) or LABEL_ALIASES.get(
        normalize_label(field_.label)
    )
    if canonical:
        value = answers.get(canonical)
        if value is not None:
            entry.question_key, entry.source = canonical, "exact"
            entry.value = match_option(value, field_.options)
            if entry.value is None:
                entry.source = "gap"  # right answer, wrong vocabulary for this dropdown
            return entry

    key = alias_map.get(normalize_label(field_.label))
    if key:
        value = answers.get(key)
        if value is not None:
            entry.question_key, entry.source = key, "alias"
            entry.value = match_option(value, field_.options)
            if entry.value is None:
                entry.source = "gap"
            return entry

    return entry


def mark_alternatives(entries: list[PlanEntry]) -> None:
    """Silence the second input of a question one input already answered.

    Greenhouse renders "Resume/CV" as a file input *and* a textarea, either of which
    satisfies the question. Having attached the file, the textarea is not a question we
    failed to answer — reporting it as a gap would put "Resume/CV" on the list of things
    to go and write an answer for, which is exactly the wrong instruction.

    Matching is by label, because that is what the two inputs share and what a person
    sees as "one question" on the page.
    """
    answered = {e.label for e in entries if e.filled and e.label}
    for entry in entries:
        if not entry.filled and entry.label in answered:
            entry.source = "alternative"


def split_gaps(gaps) -> tuple[list, list[tuple[str, list]]]:
    """`(generic, [(company, gaps), …])` — which unanswered questions pay off repeatedly.

    Generic means a question that is canonical or common (GENERIC_KEYS), or that two or
    more employers have already asked. Everything else is one company's own question and
    is grouped under that company.

    The rule needs no new state and no maintained list, which is the point: a question
    migrates into the generic list on its own the day a second employer asks it, the same
    way `tuning`'s suggestions avoid a hand-maintained blocklist. Generic sorts by how
    many ask, descending, because that is the order in which answering them pays.

    This decides which list a question is *rendered* in and nothing else. No write and no
    fill reads it, so a misfiled question costs ordering, never correctness.
    """
    generic, owned = [], {}
    for gap in gaps:
        companies = store.gap_companies(gap)
        if gap["question_key"] in GENERIC_KEYS or len(companies) > 1:
            generic.append((len(companies), gap))
        elif companies:
            owned.setdefault(companies[0], []).append(gap)
        else:
            # No company recorded at all. Nothing to file it under, and dropping it would
            # hide a question you still owe someone an answer to.
            generic.append((0, gap))

    generic.sort(key=lambda pair: (-pair[0], pair[1]["first_seen"] or "",
                                   pair[1]["question_key"]))
    groups = sorted(owned.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for _, rows in groups:
        rows.sort(key=lambda g: (g["first_seen"] or "", g["question_key"]))
    return [gap for _, gap in generic], groups


def gap_ask_count(gap) -> int:
    """How many employers have asked this question. Rendered beside a generic gap."""
    return len(store.gap_companies(gap))


def retarget_resume(plan_json: Optional[str], resume: Optional[str]) -> Optional[str]:
    """Point a stored plan's resume entries at `resume`.

    Required, not defensive. `browser._plan_index` lets a stored plan value win over a
    fresh `resolve_field`, so swapping `answers.resume` alone would still attach the
    bank's file at every posting that already had a plan — silently, and under your name.
    Both halves are applied wherever an application is opened.
    """
    if not plan_json or not resume:
        return plan_json
    try:
        entries = json.loads(plan_json)
    except (ValueError, TypeError):
        return plan_json
    if not isinstance(entries, list):
        return plan_json
    for entry in entries:
        if isinstance(entry, dict) and entry.get("question_key") == "resume":
            entry["value"] = resume
    return json.dumps(entries)


def plan_from_fields(fields, answers, alias_map: dict, form_source: str) -> "PrefillResult":
    """Everything a plan can be given without asking a model.

    Shared by the nightly task and by the Rebuild button, which is what stops the two
    from drifting: one of them running a resolution pass the other does not have is
    exactly how a button comes to report counts the pipeline disagrees with.
    """
    entries = [resolve_field(f, answers, alias_map) for f in fields]
    mark_alternatives(entries)
    return PrefillResult(entries=entries, form_source=form_source)


class PrefillTask(Task):
    name = "prefill"
    priority = 30
    summary = "prefill applications for the best-matched jobs"

    def unavailable_reason(self, ctx: TaskContext) -> Optional[str]:
        if ctx.answers is None:
            return "answers.yaml not loaded (copy answers.example.yaml and fill it in)"
        return None

    def pending(self, conn, ctx, limit=None):
        rows = store.matches_needing_prefill(
            conn, ctx.answers.hash, ctx.today, limit=None
        )
        alias_map = dict(ctx.answers.by_alias)
        alias_map.update(store.known_question_keys(conn))
        overrides = store.posting_resumes(conn)

        units: list[TaskUnit] = []
        for row in rows:
            company_name = row["company"]
            cached = store.form_fields_for(conn, company_name)
            company = ctx.companies.get(company_name)
            source = get_source(company.ats) if company else None
            publishes = bool(
                company
                and company.slug
                and source is not None
                and source.application_form_url(company.slug, row["ats_job_id"])
            )
            # A company whose form we neither hold nor can fetch is not pending work —
            # it is waiting on a browser visit to teach us its form. Counting it as
            # queued would make the backlog look like something a model could drain.
            if not cached and not publishes:
                continue

            units.append(TaskUnit(
                task=self.name,
                company=company_name,
                ats_job_id=row["ats_job_id"],
                # The answers hash IS the question: "given everything I know today,
                # what goes in this form?" Write one more answer and it is a new
                # question, so every plan is rebuilt — for free, since the rebuild is
                # rules-only for every field that was already resolved.
                #
                # The posting's own resume is deliberately NOT in this key. It is a
                # different question, tracked by `prefill_plans.resume_key`, and folding
                # it in here would reset the attempt ledger of a posting whose form
                # question has not changed at all.
                unit_key=ctx.answers.hash,
                title=row["title"],
                payload={
                    "url": row["url"],
                    "score": row["score"],
                    "cached_fields": [_field_of(r) for r in cached],
                    "alias_map": alias_map,
                    "resume_override": (
                        overrides[(company_name, row["ats_job_id"])]["filename"]
                        if (company_name, row["ats_job_id"]) in overrides else None
                    ),
                },
            ))
            if limit is not None and len(units) >= limit:
                break
        return units

    async def run(self, unit, client, ctx):
        fields = unit.payload["cached_fields"]
        form_source = "dom" if fields else "greenhouse-api"

        if not fields and ctx.fetcher is not None:
            company = ctx.companies.get(unit.company)
            if company is not None:
                # The Fetcher is synchronous and rate-limited; off the event loop it
                # goes, so one slow board does not stall the other units in flight.
                fields = await asyncio.to_thread(
                    ctx.fetcher.fetch_application_form, company, unit.ats_job_id
                )
        if not fields:
            return None

        # This posting's own resume, if it has one. A copy of the frozen bank, never a
        # mutation: `ctx.answers` is shared across every unit in flight. The copy's
        # `.hash` is deliberately not used for anything — see `apply`.
        answers = ctx.answers
        override = unit.payload.get("resume_override")
        if override:
            path = resumes.path_for(override)
            if path.is_file():
                answers = replace(answers, resume=path)
            else:
                log.warning("resume %s for %s is missing — planning the bank's",
                            override, unit.label)

        alias_map = unit.payload["alias_map"]
        result = plan_from_fields(fields, answers, alias_map, form_source)
        entries = result.entries

        # The model pass, and only for what the rules could not place. Most nights this
        # is zero calls: a form is mostly fields we have seen before.
        candidates = answers.answerable
        if candidates:
            for entry in entries:
                if entry.filled or entry.type == "file" or entry.source == "alternative":
                    continue
                if not vocabulary_known(entry):
                    # A dropdown whose options we cannot see. The model may point at a
                    # key, but nothing here could check the stored answer against what
                    # the field actually accepts, so pointing is guessing — see
                    # `vocabulary_known`. Left as a gap, which is a question you answer
                    # once and then have everywhere.
                    continue
                key = await self._ask(entry, candidates, client, unit)
                if key is None:
                    continue
                value = answers.get(key)
                if value is None:
                    continue
                chosen = match_option(value, entry.options)
                if chosen is None:
                    continue
                entry.question_key, entry.value, entry.source = key, chosen, "model"

        return result

    async def _ask(self, entry: PlanEntry, candidates, client, unit) -> Optional[str]:
        text = await client.complete(
            system=MATCH_SYSTEM_PROMPT,
            user=(
                f"Question on {unit.company}'s application form:\n"
                f"  {entry.label}\n"
                f"  (type: {entry.type}"
                + (f", one of: {', '.join(entry.options[:12])}" if entry.options else "")
                + ")\n\n"
                "Answer keys available:\n"
                + "\n".join(f"  {k}" for k in candidates)
            ),
            schema=match_schema(list(candidates)),
            schema_name=MATCH_SCHEMA_NAME,
            # The field is part of the question, so it is part of the key — otherwise
            # every field of one form would collapse onto the same cached answer.
            idempotency_key=f"{unit.idempotency_key()}:{entry.form_key}",
            max_tokens=64,
        )
        return parse_match(text, set(candidates))

    def apply(self, conn, unit, result: PrefillResult, ctx):
        for entry in result.entries:
            store.upsert_form_field(
                conn,
                company=unit.company,
                form_key=entry.form_key,
                label=entry.label,
                field_type=entry.type,
                now=ctx.today,
                required=entry.required,
                options=json.dumps(list(entry.options)) if entry.options else None,
                # Only a key that actually placed a value. A match the rules then
                # refused — the right answer in the wrong vocabulary — keeps its
                # `question_key` on the entry, and storing that taught it to
                # `known_question_keys`, which replays it as a *deterministic* alias at
                # every company from then on. One rejected guess became permanent and
                # model-free; that is how "Country*" came to mean `location`.
                question_key=entry.question_key if entry.filled else None,
                source=result.form_source,
            )
        for entry in result.gaps:
            key = entry.question_key or slugify(entry.label)
            if store.record_gap(
                conn,
                question_key=key,
                ask=entry.label,
                field_type=entry.type,
                company=unit.company,
                now=ctx.today,
                options=" | ".join(entry.options[:20]) if entry.options else None,
            ):
                log.info("new question with no answer: %s (%s)", entry.label, unit.company)

        store.record_plan(
            conn,
            company=unit.company,
            ats_job_id=unit.ats_job_id,
            plan_json=json.dumps([e.as_dict() for e in result.entries]),
            fields=len(result.entries),
            gaps=len(result.gaps),
            # The BANK's hash, never the copy's. The copy carries the override in its
            # resume basename, so storing its hash would make this plan look stale
            # against `ctx.answers.hash` and rebuild it every night forever. Which resume
            # was used is `resume_key`'s question, and `matches_needing_prefill` compares
            # that column separately.
            answers_hash=ctx.answers.hash,
            now=ctx.today,
            resume_key=unit.payload.get("resume_override"),
        )
        return result.summary


def _field_of(row) -> FormField:
    """A stored form_fields row back into the shared vocabulary."""
    options = json.loads(row["options"]) if row["options"] else []
    return FormField(
        key=row["form_key"],
        label=row["label"],
        type=row["type"],
        required=bool(row["required"]),
        options=tuple(str(o) for o in options),
    )


register(PrefillTask())
