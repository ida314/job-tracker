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
from jobtracker.tasks import prefill
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


# -- `jobtracker prepare` ------------------------------------------------------------
# The "is tomorrow morning actually useful?" check. Its exit code is the only thing an
# unattended scheduler sees, so what it does and does not treat as a failure matters.
def _prepare(db, answers, tmp_path, count=3):
    from jobtracker.cli import main

    return main(["prepare", "--db", str(db), "--answers", str(answers.path),
                 "--since", TODAY, "--count", str(count)])


def test_prepare_reports_ready_when_every_pick_has_a_plan(answers, tmp_path, capsys):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    _seed(conn, answers)
    store.record_plan(conn, "Stripe", "8077887", "[]", fields=6, gaps=0,
                      answers_hash=answers.hash, now=TODAY)
    conn.commit()
    conn.close()

    assert _prepare(db, answers, tmp_path) == 0
    out = capsys.readouterr().out
    assert "1/1 ready" in out
    assert "nothing left to type" in out


def test_gaps_never_make_it_fail(answers, tmp_path, capsys):
    """A form with questions you have not answered is the normal state, not a fault.

    Failing on gaps would leave the nightly job permanently red for a condition only
    the user can clear — the same trap as flagging dbt Labs' legitimately empty board.
    """
    db = tmp_path / "s.db"
    conn = store.connect(db)
    _seed(conn, answers)
    store.record_plan(conn, "Stripe", "8077887", "[]", fields=16, gaps=9,
                      answers_hash=answers.hash, now=TODAY)
    conn.commit()
    conn.close()

    assert _prepare(db, answers, tmp_path) == 0
    assert "9 need you" in capsys.readouterr().out


def test_a_pick_with_no_plan_at_all_is_not_ready(answers, tmp_path, capsys):
    """That is the one state that leaves you opening a blank form in the morning."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    _seed(conn, answers)
    conn.close()

    # No router configured, so nothing can be built and the pick stays planless.
    assert _prepare(db, answers, tmp_path) == 2
    out = capsys.readouterr().out
    assert "0/1 ready" in out
    assert "NOT READY" in out


def test_it_names_the_reason_a_pick_could_not_be_prepared(answers, tmp_path, capsys,
                                                          monkeypatch):
    """Three situations look identical in the DB and need different things from you."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    _seed(conn, answers)
    conn.close()

    # An Ashby company: no published form, so a browser visit is the only way forward.
    import jobtracker.config as cfg

    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "- name: Stripe\n  ats: ashby\n  slug: stripe\n  tier: 1\n"
        "  check_method: api\n  expected_board_name: Stripe\n"
    )
    monkeypatch.setattr(cfg, "COMPANIES_YAML", companies)

    from jobtracker.cli import main

    # --companies is a global flag, so it precedes the subcommand.
    assert main(["--companies", str(companies), "prepare", "--db", str(db),
                 "--answers", str(answers.path), "--since", TODAY]) == 2
    out = capsys.readouterr().out
    assert "does not publish its form" in out
    assert "apply-to Stripe 8077887" in out


def test_prepare_with_nothing_queued_is_not_a_failure(answers, tmp_path, capsys):
    db = tmp_path / "s.db"
    store.connect(db).close()
    assert _prepare(db, answers, tmp_path) == 0
    assert "Nothing queued" in capsys.readouterr().out


def test_prepare_rescores_before_choosing_the_picks(answers, tmp_path):
    """The picks must reflect the judgments as they stand now, not as they were scored.

    Otherwise a weight edit, or a `judge` run that has not been followed by a `rank`,
    silently prepares yesterday's three.
    """
    db = tmp_path / "s.db"
    conn = store.connect(db)
    _seed(conn, answers)
    conn.execute("UPDATE rankings SET score=NULL, scored_at=NULL")  # judged, unscored
    conn.commit()
    conn.close()

    _prepare(db, answers, tmp_path)

    conn = store.connect(db)
    assert conn.execute("SELECT score FROM rankings").fetchone()[0] is not None
    conn.close()


# -- generic vs one company's own ---------------------------------------------------
def _gap_row(key, seen_on, first_seen="2026-08-01"):
    return {"question_key": key, "seen_on": seen_on, "first_seen": first_seen,
            "ask": key, "type": "text", "options": None}


def test_a_question_two_employers_ask_is_generic_and_one_employers_is_not():
    generic, per_company = prefill.split_gaps([
        _gap_row("how_did_you_hear", "Stripe,Ramp"),
        _gap_row("why_stripe", "Stripe"),
    ])
    assert [g["question_key"] for g in generic] == ["how_did_you_hear"]
    assert [(c, [g["question_key"] for g in rows]) for c, rows in per_company] == [
        ("Stripe", ["why_stripe"])]


def test_a_canonical_field_is_generic_even_at_one_employer():
    """A first sighting of "work authorization" is still an answer worth writing once."""
    generic, per_company = prefill.split_gaps([_gap_row("work_authorization", "Stripe")])
    assert [g["question_key"] for g in generic] == ["work_authorization"]
    assert per_company == []


def test_generic_questions_sort_by_how_many_employers_ask():
    generic, _ = prefill.split_gaps([
        _gap_row("two", "A,B"), _gap_row("four", "A,B,C,D"), _gap_row("three", "A,B,C"),
    ])
    assert [g["question_key"] for g in generic] == ["four", "three", "two"]


def test_a_gap_nobody_is_recorded_against_is_still_shown():
    """Dropping it would hide a question you still owe someone an answer to."""
    generic, per_company = prefill.split_gaps([_gap_row("orphan", "")])
    assert [g["question_key"] for g in generic] == ["orphan"]
    assert per_company == []


def test_the_yaml_stubs_are_ordered_like_the_settings_page():
    """The block is a rendering of `prefill_gaps` and so is that page; two renderings of
    one table should not disagree about what to do first."""
    from jobtracker import answers as answers_mod

    block = answers_mod.render_gap_block([
        _gap_row("why_stripe", "Stripe"),
        _gap_row("how_did_you_hear", "Stripe,Ramp"),
    ])
    assert block.index("how_did_you_hear") < block.index("why_stripe")


# -- a resume for one posting -------------------------------------------------------
def test_retargeting_a_plan_moves_only_the_resume_entry():
    """`browser._plan_index` lets a stored plan value beat a fresh `resolve_field`, so
    swapping `answers.resume` alone would still attach the bank's file."""
    plan = json.dumps([
        {"form_key": "resume", "question_key": "resume", "value": "/bank/resume.pdf"},
        {"form_key": "email", "question_key": "email", "value": "d@example.edu"},
    ])
    out = json.loads(prefill.retarget_resume(plan, "/data/resumes/acme_1.pdf"))
    assert out[0]["value"] == "/data/resumes/acme_1.pdf"
    assert out[1]["value"] == "d@example.edu"


def test_retargeting_survives_a_plan_it_cannot_read():
    for plan in (None, "", "not json", "[]", json.dumps({"not": "a list"})):
        assert prefill.retarget_resume(plan, "/x.pdf") == plan


def test_a_posting_resume_does_not_change_the_answers_hash(answers, tmp_path):
    """Two questions, two columns. Folding the override into the hash would make every
    plan built with one look permanently stale and rebuild it every night forever."""
    import dataclasses

    override = tmp_path / "tailored.pdf"
    override.write_bytes(b"%PDF-1.4 x")
    swapped = dataclasses.replace(answers, resume=override)
    assert swapped.resume != answers.resume
    assert swapped.hash != answers.hash          # the copy is a different question...
    # ...which is exactly why `apply` stores ctx.answers.hash and never the copy's.


def test_a_changed_posting_resume_puts_that_posting_back_in_the_queue(answers):
    """And only that posting: the disjunct compares two stored columns, per row."""
    conn = store.connect(":memory:")
    _seed(conn, answers)
    store.record_plan(conn, "Stripe", "8077887", "[]", 3, 0, answers.hash, TODAY)
    conn.commit()
    assert store.matches_needing_prefill(conn, answers.hash, TODAY) == []

    store.set_posting_resume(conn, "Stripe", "8077887", "stripe_1_ab12.pdf", 10, TODAY)
    conn.commit()
    queued = store.matches_needing_prefill(conn, answers.hash, TODAY)
    assert [r["ats_job_id"] for r in queued] == ["8077887"]

    # Re-planned with that resume recorded, it is settled again.
    store.record_plan(conn, "Stripe", "8077887", "[]", 3, 0, answers.hash, TODAY,
                      resume_key="stripe_1_ab12.pdf")
    conn.commit()
    assert store.matches_needing_prefill(conn, answers.hash, TODAY) == []
    conn.close()
