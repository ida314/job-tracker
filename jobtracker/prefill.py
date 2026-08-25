"""Work out what goes in every box on an application form, from what you have written.

Everything here is deterministic. A field is filled from an answer *you* typed,
matched to the question by its canonical ATS name or by a question wording you have
attached to that answer; anything else is a gap. There is no model in this module, and
no socket of any kind.

    the form's questions        (Greenhouse publishes them; others are learned from
        │                        the DOM on the first browser visit)
        ├─ exact  canonical ATS field name -> identity
        ├─ alias  the same question, already answered elsewhere
        └─ gap    nothing fits -> recorded, and appended to answers.yaml for you

The gap arm is the point of the whole design. A field the system cannot fill is not
silently skipped: it is named, attributed to the companies that asked it, and turned
into a stub you fill in once — after which every future prefill knows it. The bank of
answers grows by being used.

**There used to be a fourth arm.** Between 2026-08-13 and 2026-08-25 a model was asked
"which of these answer keys, if any, answers this question?" for every field the rules
could not place. It was the most tightly bounded role in DESIGN.md §8 — an enum of keys
you had already written, plus `none`, with no path by which a sentence it composed could
reach a form field. It was still removed, because pointing at the wrong key is the same
harm as writing the wrong sentence: measured against the live database it had matched
*"Protected Veteran Status"* to `are_you_a_current_mongodb_employee`, *"Are you at
least 18 years of age?"* to a work-eligibility answer, and every *"do you require
sponsorship?"* to an authorization answer at **inverted polarity**. 229 of 383 resolved
fields were explicable only as its output. What replaces it is a person attaching a
question to a key on `/apply` or in Settings — the same choice from the same list, made
by someone who knows the answer. `jobtracker forget-learned` unwinds what it taught.

Nothing here submits anything, and nothing here opens a browser. This module produces a
*plan*; `browser.py` is what carries it to a page, and only when you ask it to.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from . import resumes, store
from .answers import normalize_label, slugify
from .models import FormField
from .sources import get_source

log = logging.getLogger("jobtracker.prefill")

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
    "linkedin profile url": "linkedin",
    "github": "github",
    "github profile": "github",
    "github url": "github",
    "github profile url": "github",
    "website": "website",
    "personal website": "website",
    "personal website url": "website",
    "portfolio": "website",
    "pronouns": "pronouns",
    "preferred pronouns": "pronouns",
    "school": "school",
    "university": "school",
    "college university": "school",
    "what college university did do you attend": "school",
    "degree": "degree",
    "what is your degree in": "degree",
}

# Every entry above is a label that names its own field and nothing else. That is the
# whole admission test, and it is worth stating because eleven of them were added on
# 2026-08-25 out of the sweep `forget-learned` performed — wordings the model had matched
# correctly, which would otherwise have become questions to retype. The ones deliberately
# left out of that harvest are the reason the test exists:
#
#   "Preferred First Name"   is not `first_name`; it is a different question with a
#                            different answer, and the model called it one.
#   "Home Address City"      is one component of an address, not where you live.
#   "Present Location:"      is, but only in context — a label this terse is exactly
#                            what a person should confirm once rather than a table guess.
#
# A wording that needs to know the employer, the surrounding question, or which of two
# readings was meant belongs in *your* alias list, attached on `/apply` while you are
# looking at the form. This table is for what is true everywhere.

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


@dataclass
class PlanEntry:
    """One field, and what we decided goes in it."""

    form_key: str
    label: str
    type: str
    required: bool
    value: Optional[str] = None
    question_key: Optional[str] = None
    source: str = "gap"  # exact | alias | file | alternative | gap
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
    That gap used to be guarded by `vocabulary_known`, which refused to let the *model*
    point at a menu whose options nobody had published — the rule that stopped identity
    `location` ("New York, New York") landing in a phone-number country selector. It went
    with the model pass, because the only writers left are a canonical name and an alias
    a person attached on purpose, and refusing those would make every combobox
    permanently unanswerable. A wrong answer here is now a wrong alias, and
    `jobtracker forget-learned` is the way back out of one.
    """
    if not options:
        return value
    wanted = normalize_label(value)
    for option in options:
        if normalize_label(option) == wanted:
            return option
    return None


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


def derivable_key(answers):
    """A predicate: can `(label, form_key)` be resolved without anyone having guessed?

    True when a canonical ATS name, a known label, a file field, or an alias **the user
    wrote themselves** produces a key. Everything else in `form_fields.question_key` was
    put there by the model pass that ran until 2026-08-25, and `store.forget_learned`
    uses this to sweep those out.

    Deliberately not "is this key in the bank" — the bad matches all pointed at real
    bank keys; that is what made them fill a form instead of being ignored. The question
    is whether anything other than a guess connects this *question* to that key.

    `known_question_keys` is deliberately not consulted either: it is the table being
    cleaned, so trusting it would make every learned key vouch for itself.
    """
    by_alias = dict(answers.by_alias) if answers is not None else {}

    def derivable(label: str, form_key: str) -> bool:
        lowered = (form_key or "").lower()
        if lowered in RESUME_FIELDS or lowered in COVER_LETTER_FIELDS:
            return True
        if CANONICAL_FIELDS.get(lowered):
            return True
        normalized = normalize_label(label or "")
        return bool(LABEL_ALIASES.get(normalized) or by_alias.get(normalized))

    return derivable


def plan_from_fields(fields, answers, alias_map: dict, form_source: str) -> "PrefillResult":
    """Resolve every field of a form against the answer bank.

    Shared by the nightly pass and by the Rebuild button, which is what stops the two
    from drifting: one of them running a resolution pass the other does not have is
    exactly how a button comes to report counts the pipeline disagrees with. Since the
    model pass was removed the two are the *same* pass — the button is now a full
    rebuild, not a degraded one.
    """
    entries = [resolve_field(f, answers, alias_map) for f in fields]
    mark_alternatives(entries)
    return PrefillResult(entries=entries, form_source=form_source)


@dataclass
class PlanContext:
    """What planning needs to know, loaded once per run.

    Deliberately the same field names `tasks.TaskContext` uses, so that bag still works
    where one is already in hand (`cli` builds exactly one for the whole pipeline) and
    a caller holding only an answer bank — the Rebuild button — can build this instead
    of importing the task package for a shape.
    """

    today: str
    answers: Any = None
    companies: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PrefillUnit:
    """One posting's worth of planning.

    What `tasks.TaskUnit` was, minus everything that only meant something to a model: no
    `unit_key` (it was the answers hash, and it existed so a failure against an old
    question was not held against a new one — there are no model failures left to hold),
    and no `task` field. `resume_override` stays, because which resume goes out is still
    a per-posting decision.
    """

    company: str
    ats_job_id: str
    title: str = ""
    url: str = ""
    score: Optional[float] = None
    cached_fields: tuple = ()
    alias_map: dict = field(default_factory=dict)
    resume_override: Optional[str] = None

    @property
    def label(self) -> str:
        return f"{self.company} — {self.title[:50]}" if self.title else self.company


@dataclass
class Report:
    """What one `build_plans` run did. The shape `tasks.runner` used to report."""

    attempted: int = 0
    applied: int = 0
    no_form: int = 0
    errors: int = 0
    gaps: int = 0
    closed: int = 0

    def summary(self) -> str:
        out = (f"prefill: {self.attempted} attempted · {self.applied} planned · "
               f"{self.no_form} no form · {self.errors} error · {self.gaps} gap(s)")
        if self.closed:
            out += f" · {self.closed} question(s) already answered"
        return out


def unavailable_reason(ctx) -> Optional[str]:
    """Why planning cannot run at all, or None. Missing config, never missing work."""
    if ctx.answers is None:
        return "answers.yaml not loaded (copy answers.example.yaml and fill it in)"
    return None


def pending(conn, ctx, limit=None) -> list[PrefillUnit]:
    """Postings whose plan is missing or stale, best-scored first.

    A pure read, derived from tables that already exist: a posting that closes overnight
    simply stops appearing. `ctx` is duck-typed — it needs `.answers`, `.companies` and
    `.today`, which is exactly what `tasks.TaskContext` carries, so the one bag still
    serves both halves of the pipeline.
    """
    rows = store.matches_needing_prefill(conn, ctx.answers.hash, ctx.today, limit=None)
    alias_map = dict(ctx.answers.by_alias)
    alias_map.update(store.known_question_keys(conn))
    overrides = store.posting_resumes(conn)

    units: list[PrefillUnit] = []
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
        # A company whose form we neither hold nor can fetch is not pending work — it is
        # waiting on a browser visit to teach us its form. Counting it as queued would
        # make the backlog look like something this pass could drain.
        if not cached and not publishes:
            continue

        units.append(PrefillUnit(
            company=company_name,
            ats_job_id=row["ats_job_id"],
            title=row["title"],
            url=row["url"],
            score=row["score"],
            cached_fields=tuple(_field_of(r) for r in cached),
            alias_map=alias_map,
            resume_override=(
                overrides[(company_name, row["ats_job_id"])]["filename"]
                if (company_name, row["ats_job_id"]) in overrides else None
            ),
        ))
        if limit is not None and len(units) >= limit:
            break
    return units


def build(unit: PrefillUnit, ctx, fetcher=None) -> Optional[PrefillResult]:
    """Plan one posting. None means "no form to plan against", never "nothing to fill".

    Synchronous, and that is the whole point of the 2026-08-25 change: this used to be a
    coroutine so its per-field model calls could overlap, and the fetch below had to go
    through `asyncio.to_thread` to stay off that event loop. With no calls to overlap
    there is no loop, so `prepare` can build plans on a box with no router at all.
    """
    fields = list(unit.cached_fields)
    form_source = "dom" if fields else "greenhouse-api"

    if not fields and fetcher is not None:
        company = ctx.companies.get(unit.company)
        if company is not None:
            fields = fetcher.fetch_application_form(company, unit.ats_job_id)
    if not fields:
        return None

    # This posting's own resume, if it has one. A copy of the frozen bank, never a
    # mutation. The copy's `.hash` is deliberately not used for anything — see `record`.
    answers = ctx.answers
    if unit.resume_override:
        path = resumes.path_for(unit.resume_override)
        if path.is_file():
            answers = replace(answers, resume=path)
        else:
            log.warning("resume %s for %s is missing — planning the bank's",
                        unit.resume_override, unit.label)

    return plan_from_fields(fields, answers, dict(unit.alias_map), form_source)


def record(conn, unit: PrefillUnit, result: PrefillResult, ctx) -> str:
    """Write the plan, the form's shape, and every question it could not answer.

    The caller commits. Kept separate from `build` so the Rebuild button and this pass
    write a plan the same way — one of them writing a different shape is how a button
    comes to report counts the pipeline disagrees with.
    """
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
            # Only a key that actually placed a value. A match the rules then refused —
            # the right answer in the wrong vocabulary — keeps its `question_key` on the
            # entry, and storing that would teach it to `known_question_keys`, which
            # replays it as an alias at every company from then on. That mechanism is
            # how one guess used to become permanent; it now only ever carries a
            # canonical name or an alias a person attached on purpose.
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
        # The BANK's hash, never the resume-swapped copy's. The copy carries the
        # override in its resume basename, so storing its hash would make this plan
        # look stale against `ctx.answers.hash` and rebuild it every night forever.
        # Which resume was used is `resume_key`'s question, and
        # `matches_needing_prefill` compares that column separately.
        answers_hash=ctx.answers.hash,
        now=ctx.today,
        resume_key=unit.resume_override,
    )
    return result.summary


def close_answered_gaps(conn, ctx) -> list[str]:
    """Resolve every open gap the bank can now answer. Returns the keys it closed.

    A gap is a question *the bank could not answer at the time it was asked*, and nothing
    re-examined that. `_api_answer` closes the one key you just wrote, which covers the
    common path and misses every other route to the same place: an identity field filled
    in Settings, an answer edited in the file by hand, an alias attached to a different
    key, or `LABEL_ALIASES` gaining the wording. Measured on the live database right
    after `forget-learned`: 11 of 200 open gaps were already answerable, and three of
    them — "Phone", "LinkedIn Profile", "Website" — sat in the top of the most-asked
    list, which is the first thing you see and now the main place you work.

    That is the ordinary failure of this project in its most ordinary form: a derived
    state that is only ever written, never re-derived. Cheap to fix here because
    `resolve_field` is the same function the plan uses, so a gap closes on exactly the
    condition that would have filled the field.

    The options go through too. A dropdown whose menu does not offer our answer is a gap,
    and asking without them would close it on a value `match_option` would then refuse.
    """
    if ctx.answers is None:
        return []
    alias_map = dict(ctx.answers.by_alias)
    alias_map.update(store.known_question_keys(conn))

    closed = []
    for gap in store.open_gaps(conn):
        options = tuple(
            o.strip() for o in (gap["options"] or "").split("|") if o.strip()
        )
        entry = resolve_field(
            FormField(key=gap["question_key"], label=gap["ask"], type=gap["type"],
                      required=True, options=options),
            ctx.answers, alias_map,
        )
        if entry.value is not None:
            store.resolve_gap(conn, gap["question_key"], ctx.today)
            closed.append(gap["question_key"])
    if closed:
        conn.commit()
        log.info("%d question(s) you had already answered are no longer listed",
                 len(closed))
    return closed


def build_plans(conn, ctx, fetcher=None, only=None, limit=None) -> Report:
    """Plan every posting that needs it. `only` restricts to (company, job) pairs.

    **Commits per unit**, which is the one guarantee worth keeping from the task
    runner it replaces: an interrupted run keeps everything it had already finished,
    rather than holding it all until a single commit at the end that never comes.
    """
    units = pending(conn, ctx, limit=limit)
    if only is not None:
        wanted = {(c, j) for c, j in only}
        units = [u for u in units if (u.company, u.ats_job_id) in wanted]

    report = Report()
    for unit in units:
        report.attempted += 1
        try:
            result = build(unit, ctx, fetcher)
        except Exception as exc:  # a bad form must not abandon the rest of the queue
            log.warning("could not read %s's form: %s", unit.label, exc)
            report.errors += 1
            continue
        if result is None:
            log.info("no application form for %s", unit.label)
            report.no_form += 1
            continue
        try:
            summary = record(conn, unit, result, ctx)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            log.warning("could not record %s's plan: %s", unit.label, exc)
            report.errors += 1
            continue
        report.applied += 1
        report.gaps += len(result.gaps)
        log.info("prefilled %s  %s", unit.label, summary)

    # Last, so it sees the gaps this run recorded as well as the ones already there.
    report.closed = len(close_answered_gaps(conn, ctx))
    return report


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

