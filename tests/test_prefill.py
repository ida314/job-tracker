"""Prefill: reading a form, placing answers in it, and naming what is missing.

The Greenhouse payload here is real — recorded from
`boards-api.greenhouse.io/v1/boards/stripe/jobs/8077887?questions=true` — and trimmed.
That endpoint is keyless and complete, which is what makes gap detection possible at all
without opening a browser, and it is the one ATS that offers it.

What the tests are protecting:

* three resolution passes, only the last of which costs a model call;
* a dropdown whose options do not include our answer is a gap, not a wrong fill;
* the model can only ever point at an answer the user wrote — it cannot produce text;
* answering a gap re-queues the plans that needed it, and nothing else.
"""

import asyncio
import json

import pytest

from jobtracker import store
from jobtracker.answers import load_answers
from jobtracker.models import Company, Decision, FormField, Posting, Verdict
from jobtracker.sources.greenhouse import Greenhouse
from jobtracker.tasks import TaskContext, get_task, run_task
from jobtracker.tasks.judge import RankJudgment
from jobtracker.tasks.prefill import (
    PlanEntry,
    mark_alternatives,
    match_option,
    match_schema,
    parse_match,
    resolve_field,
)

TODAY = "2026-08-13"

# Recorded from a live board on 2026-08-13, trimmed to the shapes that matter.
GREENHOUSE_QUESTIONS = {
    "id": 8077887,
    "title": "Backend Engineer",
    "questions": [
        {"label": "First Name", "required": True,
         "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
        {"label": "Email", "required": True,
         "fields": [{"name": "email", "type": "input_text", "values": []}]},
        {"label": "Resume/CV", "required": False,
         "fields": [{"name": "resume", "type": "input_file", "values": []},
                    {"name": "resume_text", "type": "textarea", "values": []}]},
        {"label": "Who is your current or previous employer?", "required": True,
         "fields": [{"name": "question_68184536", "type": "input_text", "values": []}]},
        {"label": "Please select the country where you currently reside.", "required": True,
         "fields": [{"name": "question_68184538", "type": "multi_value_single_select",
                     "values": [{"label": "United States", "value": 738075129},
                                {"label": "Canada", "value": 738075134}]}]},
    ],
}


@pytest.fixture
def answers(tmp_path):
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4")
    path = tmp_path / "answers.yaml"
    path.write_text("""\
identity:
  first_name: Dylan
  last_name: D
  email: dyd2008@nyu.edu
  phone: "+1 555 0100"
resume: ./resume.pdf

answers:
  current_employer:
    value: "New York University"
    aliases: ["Who is your current or previous employer?"]
  country_of_residence:
    value: "United States"
    aliases: ["Please select the country where you currently reside."]
""")
    return load_answers(path)


@pytest.fixture
def unaliased(tmp_path):
    """The same bank with no aliases — every opaque question falls to the model."""
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4")
    path = tmp_path / "answers.yaml"
    path.write_text("""\
identity:
  first_name: Dylan
  last_name: D
  email: dyd2008@nyu.edu
resume: ./resume.pdf

answers:
  country_of_residence: "United States"
""")
    return load_answers(path)


# -- reading the form ---------------------------------------------------------------
def test_the_greenhouse_payload_yields_one_field_per_input():
    """One question can render as several inputs, and they are kept separate.

    "Resume/CV" is a file input *and* a textarea, either of which satisfies it —
    representable only if they are two fields sharing one label.
    """
    fields = Greenhouse().parse_application_form(GREENHOUSE_QUESTIONS)
    by_key = {f.key: f for f in fields}
    assert set(by_key) == {"first_name", "email", "resume", "resume_text",
                           "question_68184536", "question_68184538"}
    assert by_key["resume"].type == "file"
    assert by_key["resume"].label == by_key["resume_text"].label == "Resume/CV"
    assert by_key["question_68184538"].type == "select"
    assert by_key["question_68184538"].options == ("United States", "Canada")
    assert by_key["first_name"].required is True


@pytest.mark.parametrize("raw", ["nonsense", None, {}, {"questions": None},
                                 {"questions": [{"fields": [{}]}]}])
def test_a_malformed_form_payload_is_no_fields_not_a_crash(raw):
    assert Greenhouse().parse_application_form(raw) == []


def test_only_greenhouse_publishes_a_form():
    """Stated as a test because it is a coverage limit, not an oversight.

    Ashby's per-job posting-api answers 401 and its GraphQL introspection is off; Lever
    publishes no custom questions. Their forms are learned from the DOM instead, and
    pretending otherwise here would hide that.
    """
    from jobtracker.sources import get_source

    assert get_source("greenhouse").application_form_url("stripe", "1") is not None
    assert get_source("ashby").application_form_url("ramp", "1") is None
    assert get_source("lever").application_form_url("Onehouse", "1") is None


# -- resolution ---------------------------------------------------------------------
def _resolve(answers, field_):
    return resolve_field(field_, answers, dict(answers.by_alias))


def test_a_canonical_ats_name_resolves_with_no_model_call(answers):
    entry = _resolve(answers, FormField("first_name", "First Name", "text", True))
    assert entry.value == "Dylan" and entry.source == "exact"


def test_a_label_resolves_when_the_field_name_is_meaningless(answers):
    """The DOM path has only labels, so "Email Address" must reach `email`."""
    entry = _resolve(answers, FormField("field_7", "Email Address", "text", True))
    assert entry.value == "dyd2008@nyu.edu" and entry.source == "exact"


def test_an_alias_resolves_an_opaque_question_id(answers):
    entry = _resolve(answers, FormField(
        "question_68184536", "Who is your current or previous employer?", "text", True))
    assert entry.value == "New York University" and entry.source == "alias"


def test_the_resume_is_a_path_not_an_answer(answers):
    entry = _resolve(answers, FormField("resume", "Resume/CV", "file", False))
    assert entry.source == "file" and entry.value.endswith("resume.pdf")


def test_a_missing_resume_makes_the_file_field_a_gap(tmp_path):
    path = tmp_path / "answers.yaml"
    path.write_text("identity:\n  first_name: D\n  last_name: D\n  email: e@x.edu\n")
    a = load_answers(path)
    entry = resolve_field(FormField("resume", "Resume/CV", "file", True), a, {})
    assert entry.value is None and entry.source == "gap"


def test_an_unknown_question_is_left_for_the_model(answers):
    entry = _resolve(answers, FormField("question_9", "Why do you want to work here?",
                                        "textarea", True))
    assert entry.value is None and entry.source == "gap"


# -- selects ------------------------------------------------------------------------
def test_a_dropdown_we_can_answer_is_filled(answers):
    entry = _resolve(answers, FormField(
        "question_68184538", "Please select the country where you currently reside.",
        "select", True, ("United States", "Canada")))
    assert entry.value == "United States" and entry.source == "alias"


def test_a_dropdown_that_does_not_offer_our_answer_is_a_gap(answers):
    """Right answer, wrong vocabulary. Picking the nearest entry would be worse.

    Silently choosing "Authorized" for a stored "Yes" puts an answer the candidate did
    not give onto a submitted application.
    """
    entry = _resolve(answers, FormField(
        "q", "Please select the country where you currently reside.", "select", True,
        ("Authorized", "Not authorized")))
    assert entry.value is None and entry.source == "gap"


def test_match_option_is_forgiving_about_case_and_punctuation():
    assert match_option("united states", ("United States",)) == "United States"
    assert match_option("Yes", ("Yes!", "No")) == "Yes!"
    assert match_option("Maybe", ("Yes", "No")) is None
    assert match_option("anything", ()) == "anything"      # not a select


# -- the second input of one question ------------------------------------------------
def test_a_satisfied_question_does_not_report_its_alternative_as_a_gap():
    """Having attached the resume, "Resume/CV" is not a question to go and answer."""
    entries = [
        PlanEntry("resume", "Resume/CV", "file", False, value="/tmp/r.pdf", source="file"),
        PlanEntry("resume_text", "Resume/CV", "textarea", False),
    ]
    mark_alternatives(entries)
    assert entries[1].source == "alternative"


# -- the model pass ------------------------------------------------------------------
def test_the_model_can_only_point_at_an_answer_that_exists(answers):
    """The schema is an enum of keys plus "none". It cannot return prose.

    This is the boundary that keeps prefill inside "the model reads, never decides":
    there is no code path by which a sentence the model composed reaches a form field.
    """
    schema = match_schema(answers.answerable)
    allowed = schema["properties"]["question_key"]["enum"]
    assert "none" in allowed
    assert set(allowed) - {"none"} == set(answers.answerable)
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("text", [
    None, "", "current_employer", "[]",
    '{"question_key": "something_i_invented"}',
    '{"question_key": "none"}',
    "It looks like current_employer to me.",
])
def test_anything_but_a_known_key_is_no_match(text):
    assert parse_match(text, {"current_employer", "email"}) is None


def test_a_known_key_is_accepted():
    assert parse_match('{"question_key": "email"}', {"email"}) == "email"


# -- end to end -----------------------------------------------------------------------
class _Stub:
    """A client that answers every question-match with the key it was constructed with."""

    def __init__(self, key="none"):
        self.key = key
        self.asked = []

    async def complete(self, system, user, schema, schema_name="", **_k):
        self.asked.append(user)
        return json.dumps({"question_key": self.key})


def _seed(conn, answers):
    store.sync_postings(
        conn, "Stripe",
        [Posting("Stripe", "8077887", "Backend Engineer",
                 "https://job-boards.greenhouse.io/stripe/jobs/8077887")],
        TODAY,
    )
    store.set_description(conn, "Stripe", "8077887", "backend, new grads welcome")
    store.record_verdict(
        conn, Verdict("Stripe", "8077887", Decision.MATCH, "r", "rules"), TODAY)
    store.record_judgment(conn, "Stripe", "8077887",
                          RankJudgment("strong", "strong", "low", "why"), "h", TODAY)
    store.set_score(conn, "Stripe", "8077887", 88.0, TODAY)
    conn.commit()


class _FormFetcher:
    def __init__(self):
        self.calls = 0

    def fetch_application_form(self, company, job_id):
        self.calls += 1
        return Greenhouse().parse_application_form(GREENHOUSE_QUESTIONS)


def _ctx(answers, fetcher=None):
    return TaskContext(
        today=TODAY,
        answers=answers,
        answers_path=answers.path,
        companies={"Stripe": Company(name="Stripe", ats="greenhouse", slug="stripe",
                                     tier=1)},
        fetcher=fetcher or _FormFetcher(),
    )


def test_a_plan_names_what_it_filled_and_what_it_could_not(answers):
    conn = store.connect(":memory:")
    _seed(conn, answers)
    client = _Stub("none")

    report = asyncio.run(run_task(conn, get_task("prefill"), client, _ctx(answers)))
    assert report.applied == 1

    plan = store.get_plan(conn, "Stripe", "8077887")
    entries = {e["form_key"]: e for e in json.loads(plan["plan"])}
    assert entries["first_name"]["value"] == "Dylan"
    assert entries["question_68184536"]["value"] == "New York University"
    assert entries["question_68184538"]["value"] == "United States"
    assert entries["resume"]["value"].endswith("resume.pdf")
    assert entries["resume_text"]["source"] == "alternative"
    assert plan["fields"] == 6 and plan["gaps"] == 0
    # Every field resolved by rule, so the model was never asked anything.
    assert client.asked == []
    conn.close()


def test_an_unanswerable_question_becomes_a_gap_once_per_question(answers):
    """The same question asked by six employers is one thing for you to answer."""
    conn = store.connect(":memory:")
    _seed(conn, answers)

    extra = dict(GREENHOUSE_QUESTIONS)
    extra["questions"] = GREENHOUSE_QUESTIONS["questions"] + [
        {"label": "Why do you want to work here?", "required": True,
         "fields": [{"name": "question_777", "type": "textarea", "values": []}]},
    ]

    class _Extra(_FormFetcher):
        def fetch_application_form(self, company, job_id):
            return Greenhouse().parse_application_form(extra)

    client = _Stub("none")
    asyncio.run(run_task(conn, get_task("prefill"), client, _ctx(answers, _Extra())))

    gaps = store.open_gaps(conn)
    assert [g["question_key"] for g in gaps] == ["why_do_you_want_to_work_here"]
    assert gaps[0]["ask"] == "Why do you want to work here?"
    assert gaps[0]["seen_on"] == "Stripe"
    # And the model was asked about exactly the one field the rules could not place.
    assert len(client.asked) == 1
    assert "Why do you want to work here?" in client.asked[0]
    conn.close()


def test_the_model_places_a_question_the_rules_could_not(unaliased):
    """With no alias written down, the model is what connects question to answer.

    And what it returns is a key — the value still comes from the file, so the text on
    the application is text the candidate wrote.
    """
    conn = store.connect(":memory:")
    _seed(conn, unaliased)
    client = _Stub("country_of_residence")

    asyncio.run(run_task(conn, get_task("prefill"), client, _ctx(unaliased)))
    entries = {
        e["form_key"]: e
        for e in json.loads(store.get_plan(conn, "Stripe", "8077887")["plan"])
    }
    country = entries["question_68184538"]
    assert country["source"] == "model"
    assert country["question_key"] == "country_of_residence"
    assert country["value"] == "United States"      # from answers.yaml, not the model
    conn.close()


def test_a_model_match_still_has_to_fit_the_dropdown(unaliased):
    """The model names a key; the option list still decides whether it can be used."""
    conn = store.connect(":memory:")
    _seed(conn, unaliased)
    # It points at an answer whose text is not one of this dropdown's options.
    client = _Stub("first_name")

    asyncio.run(run_task(conn, get_task("prefill"), client, _ctx(unaliased)))
    entries = {
        e["form_key"]: e
        for e in json.loads(store.get_plan(conn, "Stripe", "8077887")["plan"])
    }
    assert entries["question_68184538"]["value"] is None
    assert entries["question_68184538"]["source"] == "gap"
    conn.close()


def test_a_company_whose_form_we_cannot_read_is_not_queued_work(answers):
    """Waiting on a browser visit is not a backlog a model could drain.

    Ashby publishes no form, so until `apply-to` has visited once there is nothing for
    this task to do — and saying "1 pending" would be a lie about what `work` can fix.
    """
    conn = store.connect(":memory:")
    _seed(conn, answers)
    ctx = _ctx(answers)
    ctx.companies["Stripe"] = Company(name="Stripe", ats="ashby", slug="stripe", tier=1)
    assert get_task("prefill").pending_count(conn, ctx) == 0
    conn.close()


def test_answering_a_gap_rebuilds_the_plans_that_needed_it(answers, tmp_path):
    from jobtracker.answers import insert_answer

    conn = store.connect(":memory:")
    _seed(conn, answers)
    fetcher = _FormFetcher()
    ctx = _ctx(answers, fetcher)

    asyncio.run(run_task(conn, get_task("prefill"), _Stub(), ctx))
    assert get_task("prefill").pending_count(conn, ctx) == 0     # nothing to redo

    path = answers.path
    path.write_text(insert_answer(path.read_text(), "why_us", "Because of the platform."))
    ctx.answers = load_answers(path)
    assert get_task("prefill").pending_count(conn, ctx) == 1     # the question changed

    asyncio.run(run_task(conn, get_task("prefill"), _Stub(), ctx))
    assert store.get_plan(conn, "Stripe", "8077887")["answers_hash"] == ctx.answers.hash
    conn.close()


def test_a_form_we_already_hold_is_not_refetched(answers):
    """A cached form is why re-running prefill costs no ATS request at all."""
    conn = store.connect(":memory:")
    _seed(conn, answers)
    fetcher = _FormFetcher()
    ctx = _ctx(answers, fetcher)

    asyncio.run(run_task(conn, get_task("prefill"), _Stub(), ctx))
    assert fetcher.calls == 1

    store.record_plan(conn, "Stripe", "8077887", "[]", 0, 0, "stale", TODAY)
    conn.commit()
    asyncio.run(run_task(conn, get_task("prefill"), _Stub(), ctx))
    assert fetcher.calls == 1        # served from form_fields the second time
    conn.close()


def test_an_applied_posting_is_never_prefilled_again(answers):
    conn = store.connect(":memory:")
    _seed(conn, answers)
    ctx = _ctx(answers)
    assert get_task("prefill").pending_count(conn, ctx) == 1

    store.record_application(conn, "Stripe", "8077887", "Backend Engineer", "applied",
                             TODAY, note=None)
    conn.commit()
    assert get_task("prefill").pending_count(conn, ctx) == 0
    conn.close()


def test_an_unscored_match_is_not_at_the_front_of_the_application_queue(answers):
    """The queue is "highest-matched first", so an unread posting has no claim to a slot."""
    conn = store.connect(":memory:")
    _seed(conn, answers)
    conn.execute("UPDATE rankings SET score=NULL")
    conn.commit()
    assert get_task("prefill").pending_count(conn, _ctx(answers)) == 0
    conn.close()
