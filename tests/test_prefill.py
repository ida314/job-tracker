"""Prefill: reading a form, placing answers in it, and naming what is missing.

The Greenhouse payload here is real — recorded from
`boards-api.greenhouse.io/v1/boards/stripe/jobs/8077887?questions=true` — and trimmed.
That endpoint is keyless and complete, which is what makes gap detection possible at all
without opening a browser, and it is the one ATS that offers it.

What the tests are protecting:

* two resolution passes, neither of which opens a socket to anything;
* a dropdown whose options do not include our answer is a gap, not a wrong fill;
* an unresolvable question is a gap, never a guess — there is no third arm any more;
* answering a gap re-queues the plans that needed it, and nothing else.
"""

import json
from types import SimpleNamespace

import pytest

from jobtracker import prefill, store
from jobtracker.answers import load_answers, normalize_label
from jobtracker.models import Company, Decision, FormField, Posting, Verdict
from jobtracker.prefill import (
    PlanContext,
    PlanEntry,
    mark_alternatives,
    match_option,
    resolve_field,
)
from jobtracker.sources.greenhouse import Greenhouse
from jobtracker.tasks.judge import RankJudgment

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
    """The same bank with no aliases — every opaque question is therefore a gap."""
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


def test_an_unknown_question_is_a_gap(answers):
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


# -- end to end -----------------------------------------------------------------------
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


def _ctx(answers):
    return PlanContext(
        today=TODAY,
        answers=answers,
        companies={"Stripe": Company(name="Stripe", ats="greenhouse", slug="stripe",
                                     tier=1)},
    )


def test_a_plan_names_what_it_filled_and_what_it_could_not(answers):
    conn = store.connect(":memory:")
    _seed(conn, answers)

    report = prefill.build_plans(conn, _ctx(answers), fetcher=_FormFetcher())
    assert report.applied == 1

    plan = store.get_plan(conn, "Stripe", "8077887")
    entries = {e["form_key"]: e for e in json.loads(plan["plan"])}
    assert entries["first_name"]["value"] == "Dylan"
    assert entries["question_68184536"]["value"] == "New York University"
    assert entries["question_68184538"]["value"] == "United States"
    assert entries["resume"]["value"].endswith("resume.pdf")
    assert entries["resume_text"]["source"] == "alternative"
    assert plan["fields"] == 6 and plan["gaps"] == 0
    conn.close()


def test_a_plan_is_built_with_no_model_anywhere_in_reach(answers, monkeypatch):
    """The regression the old shape hid, and the reason prefill left `work`.

    Until 2026-08-25 this ran as a task, and both doors into it — `cmd_work` and
    `prepare`'s `_prefill_picks` — built an `LlmClient` and bailed on a failed `probe()`.
    So a box with no router prepared nothing and every pick reported "no plan": the
    failure `prepare` exists to catch, manufactured by `prepare` itself. Constructing a
    client here is made an error outright, because "it happens not to be called today" is
    what quietly stops being true.
    """
    import jobtracker.llm as llm_pkg

    def _boom(*a, **k):
        raise AssertionError("prefill must never construct a model client")

    monkeypatch.setattr(llm_pkg, "LlmClient", _boom)

    conn = store.connect(":memory:")
    _seed(conn, answers)
    report = prefill.build_plans(conn, _ctx(answers), fetcher=_FormFetcher())
    assert report.applied == 1
    assert store.get_plan(conn, "Stripe", "8077887") is not None
    conn.close()


def test_the_module_opens_no_socket_and_asks_nothing():
    """Read off the source, the way `browser.py`'s no-click rule is.

    A plan decides what text goes into a real job application. The property worth pinning
    is not "the model call was deleted" but "there is nowhere in here for one to come
    back", which an import-graph assertion states and a behavioural test cannot.
    """
    import inspect

    src = inspect.getsource(prefill)
    for banned in ("import httpx", "import requests", "from ..llm", "from .llm",
                   "LlmClient", "response_format", "schema_name", "await "):
        assert banned not in src, banned


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

    prefill.build_plans(conn, _ctx(answers), fetcher=_Extra())

    gaps = store.open_gaps(conn)
    assert [g["question_key"] for g in gaps] == ["why_do_you_want_to_work_here"]
    assert gaps[0]["ask"] == "Why do you want to work here?"
    assert gaps[0]["seen_on"] == "Stripe"
    conn.close()


def test_an_opaque_question_with_no_alias_stays_a_gap(unaliased):
    """What the model used to do, and why nothing does it now.

    The bank holds `country_of_residence` and the form asks "Please select the country
    where you currently reside." — the same question in different words, and with no
    alias written down nothing here connects them. That is deliberate. The model that
    used to make this exact match also matched "Protected Veteran Status" to
    `are_you_a_current_mongodb_employee` and every "do you require sponsorship?" to a
    work-authorization answer whose stored value means the opposite. A gap costs one
    line typed once; a wrong match goes out under your name and is not recallable.
    """
    conn = store.connect(":memory:")
    _seed(conn, unaliased)

    prefill.build_plans(conn, _ctx(unaliased), fetcher=_FormFetcher())
    entries = {
        e["form_key"]: e
        for e in json.loads(store.get_plan(conn, "Stripe", "8077887")["plan"])
    }
    country = entries["question_68184538"]
    assert country["value"] is None and country["source"] == "gap"
    assert "please select the country where you currently reside" in {
        g["question_key"].replace("_", " ") for g in store.open_gaps(conn)
    }
    conn.close()


def test_a_company_whose_form_we_cannot_read_is_not_queued_work(answers):
    """Waiting on a browser visit is not a backlog this pass could drain.

    Ashby publishes no form, so until `apply-to` has visited once there is nothing to
    do here — and saying "1 pending" would be a lie about what `prefill` can fix.
    """
    conn = store.connect(":memory:")
    _seed(conn, answers)
    ctx = _ctx(answers)
    ctx.companies["Stripe"] = Company(name="Stripe", ats="ashby", slug="stripe", tier=1)
    assert len(prefill.pending(conn, ctx)) == 0
    conn.close()


def test_answering_a_gap_rebuilds_the_plans_that_needed_it(answers, tmp_path):
    from jobtracker.answers import insert_answer

    conn = store.connect(":memory:")
    _seed(conn, answers)
    ctx = _ctx(answers)

    prefill.build_plans(conn, ctx, fetcher=_FormFetcher())
    assert len(prefill.pending(conn, ctx)) == 0                  # nothing to redo

    path = answers.path
    path.write_text(insert_answer(path.read_text(), "why_us", "Because of the platform."))
    ctx.answers = load_answers(path)
    assert len(prefill.pending(conn, ctx)) == 1                  # the question changed

    prefill.build_plans(conn, ctx, fetcher=_FormFetcher())
    assert store.get_plan(conn, "Stripe", "8077887")["answers_hash"] == ctx.answers.hash
    conn.close()


def test_a_form_we_already_hold_is_not_refetched(answers):
    """A cached form is why re-running prefill costs no ATS request at all."""
    conn = store.connect(":memory:")
    _seed(conn, answers)
    fetcher = _FormFetcher()
    ctx = _ctx(answers)

    prefill.build_plans(conn, ctx, fetcher=fetcher)
    assert fetcher.calls == 1

    store.record_plan(conn, "Stripe", "8077887", "[]", 0, 0, "stale", TODAY)
    conn.commit()
    prefill.build_plans(conn, ctx, fetcher=fetcher)
    assert fetcher.calls == 1        # served from form_fields the second time
    conn.close()


def test_an_applied_posting_is_never_prefilled_again(answers):
    conn = store.connect(":memory:")
    _seed(conn, answers)
    ctx = _ctx(answers)
    assert len(prefill.pending(conn, ctx)) == 1

    store.record_application(conn, "Stripe", "8077887", "Backend Engineer", "applied",
                             TODAY, note=None)
    conn.commit()
    assert len(prefill.pending(conn, ctx)) == 0
    conn.close()


def test_an_unscored_match_is_not_at_the_front_of_the_application_queue(answers):
    """The queue is "highest-matched first", so an unread posting has no claim to a slot."""
    conn = store.connect(":memory:")
    _seed(conn, answers)
    conn.execute("UPDATE rankings SET score=NULL")
    conn.commit()
    assert len(prefill.pending(conn, _ctx(answers))) == 0
    conn.close()


# -- `jobtracker prepare` ------------------------------------------------------------
# The "is tomorrow morning actually useful?" check. Its exit code is the only thing an
# unattended scheduler sees, so what it does and does not treat as a failure matters.
@pytest.fixture
def form(monkeypatch):
    """Serve the recorded Greenhouse form to anything `prepare` builds a Fetcher for.

    Required, not tidy. Until 2026-08-25 `prepare` built an `LlmClient`, failed its
    `probe()` on a test box, and returned before reaching the ATS — so these tests
    passed *because* prefill was router-gated, and the first one below asserted exit 2
    on the strength of it. With the gate gone the same code path reaches
    `boards-api.greenhouse.io` for real. Returning `[]` from this is how a test says
    "this company's form cannot be read", which is now the only way to be planless.
    """
    from jobtracker.fetch import Fetcher

    served = SimpleNamespace(fields=Greenhouse().parse_application_form(
        GREENHOUSE_QUESTIONS))
    monkeypatch.setattr(Fetcher, "fetch_application_form",
                        lambda self, company, job_id: served.fields)
    return served


def _prepare(db, answers, tmp_path, count=3):
    from jobtracker.cli import main

    return main(["prepare", "--db", str(db), "--answers", str(answers.path),
                 "--since", TODAY, "--count", str(count)])


def test_prepare_reports_ready_when_every_pick_has_a_plan(form, answers, tmp_path, capsys):
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


def test_gaps_never_make_it_fail(form, answers, tmp_path, capsys):
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


def test_a_pick_with_no_plan_at_all_is_not_ready(form, answers, tmp_path, capsys):
    """That is the one state that leaves you opening a blank form in the morning."""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    _seed(conn, answers)
    conn.close()

    # The form cannot be read, so nothing can be planned and the pick stays planless.
    # Zero fields is "we could not read this form", never "0/0, nothing left to do".
    form.fields = []
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


def test_prepare_with_nothing_queued_is_not_a_failure(form, answers, tmp_path, capsys):
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


# -- what the model is not allowed to point at -----------------------------------------
def test_a_dropdown_whose_options_nobody_published_still_cannot_be_guessed_at():
    """The rule `vocabulary_known` used to carry, now carried by there being no guesser.

    A combobox renders its menu in JavaScript, so a DOM reading finds no options and
    `match_option` waves any string through — which is how identity `location` ("New
    York, New York") was written into a phone-number country selector labelled
    "Country*". `vocabulary_known` refused to let the *model* point at such a field; it
    went with the model pass, because the only writers left are a canonical name and an
    alias a person attached on purpose, and holding those to it would make every
    combobox permanently unanswerable.

    So the guard is now structural: with no alias, an unrecognized label is a gap
    whatever its type, and nothing is in a position to guess at the vocabulary.
    """
    answers = SimpleNamespace(
        get=lambda k: None, by_alias={},
        identity={}, answers={},
    )
    entry = resolve_field(
        FormField("country", "Country*", "combobox", True), answers, {})
    assert entry.value is None and entry.source == "gap"
    assert not hasattr(prefill, "vocabulary_known")


def test_a_match_the_rules_refused_is_not_taught_as_an_alias(tmp_path):
    """`known_question_keys` replays a stored `question_key` as a *deterministic* alias
    at every company, so a guess that was then rejected became permanent and model-free.

    `resolve_field` leaves `question_key` on an entry whose value it went on to refuse —
    the right answer in the wrong vocabulary — and `record` used to store that.
    """
    conn = store.connect(tmp_path / "state.db")
    entry = prefill.PlanEntry(
        form_key="country", label="Country*", type="select", required=True,
        options=("United States", "Canada"),
        value=None, question_key="location", source="gap",
    )
    result = prefill.PrefillResult(entries=[entry], form_source="dom")
    ctx = SimpleNamespace(today="2026-08-23",
                          answers=SimpleNamespace(hash="h"))
    unit = prefill.PrefillUnit(company="Twilio", ats_job_id="1", title="x")

    prefill.record(conn, unit, result, ctx)

    row = conn.execute("SELECT question_key FROM form_fields").fetchone()
    assert row["question_key"] is None
    assert store.known_question_keys(conn) == {}
    conn.close()


def test_a_question_you_have_since_answered_stops_being_listed(answers):
    """A gap is only ever written, and until 2026-08-25 nothing re-examined one.

    `_api_answer` closes the key you just wrote, which covers the common path and misses
    every other route to the same place: an identity field filled in Settings, a value
    edited in the file by hand, an alias attached to a different key, or `LABEL_ALIASES`
    gaining the wording. Measured on the live database straight after `forget-learned`:
    11 of 200 open gaps were already answerable, and "Phone", "LinkedIn Profile" and
    "Website" were near the top of the most-asked list — which is the first thing you see
    and now the main place you work.
    """
    conn = store.connect(":memory:")
    store.record_gap(conn, question_key="phone", ask="Phone", field_type="text",
                     company="Stripe", now=TODAY)
    store.record_gap(conn, question_key="why_stripe", ask="Why Stripe?",
                     field_type="textarea", company="Stripe", now=TODAY)
    conn.commit()

    closed = prefill.close_answered_gaps(conn, _ctx(answers))
    assert closed == ["phone"]
    assert [g["question_key"] for g in store.open_gaps(conn)] == ["why_stripe"]
    conn.close()


def test_a_dropdown_that_does_not_offer_the_answer_stays_listed(answers):
    """The options travel with the question, or a gap closes on a value the form would
    refuse — "the right answer in the wrong vocabulary", closed instead of asked."""
    conn = store.connect(":memory:")
    store.record_gap(conn, question_key="country_of_residence",
                     ask="Please select the country where you currently reside.",
                     field_type="select", company="Stripe", now=TODAY,
                     options="Canada | Mexico")
    conn.commit()

    assert prefill.close_answered_gaps(conn, _ctx(answers)) == []
    assert len(store.open_gaps(conn)) == 1
    conn.close()


def test_the_label_table_holds_only_labels_that_name_their_own_field():
    """The admission test for `LABEL_ALIASES`, stated because it was nearly widened past
    it. Eleven entries were harvested on 2026-08-25 from what `forget-learned` swept —
    wordings the model had matched correctly, which would otherwise be questions to
    retype. The temptation is to take the near-misses with them.

    "Preferred First Name" is not `first_name` — it is a different question with a
    different answer, and reading it as one is what put a nickname in a legal-name field.
    A wording that needs the employer, the surrounding question, or a choice between two
    readings belongs in the user's own alias list, attached while looking at the form.
    """
    from jobtracker.prefill import LABEL_ALIASES

    for label, key in LABEL_ALIASES.items():
        assert label == normalize_label(label), label
    assert LABEL_ALIASES["linkedin profile url"] == "linkedin"
    assert LABEL_ALIASES["what is your degree in"] == "degree"
    for near_miss in ("preferred first name", "preferred last name",
                      "home address city", "present location"):
        assert near_miss not in LABEL_ALIASES, near_miss


# -- forget-learned ------------------------------------------------------------------
# The sweep for a database that ran the model pass. Deleting `_ask` removed nothing it
# had already decided: `record` wrote every match onto `form_fields.question_key`, which
# `known_question_keys` replays as a deterministic alias at every company forever.
def _resolved(conn, company, form_key, label, key):
    store.upsert_form_field(conn, company=company, form_key=form_key, label=label,
                            field_type="text", now=TODAY, required=True, options=None,
                            question_key=key, source="dom")


def test_forget_learned_keeps_the_rules_and_drops_the_guesses(answers):
    """Three rows, one of each provenance, and only the third can have been a guess."""
    conn = store.connect(":memory:")
    # A canonical ATS field name — `CANONICAL_FIELDS` produces this with no help.
    _resolved(conn, "Stripe", "first_name", "First Name", "first_name")
    # A wording the user attached themselves, in answers.yaml's alias list.
    _resolved(conn, "Stripe", "q1", "Who is your current or previous employer?",
              "current_employer")
    # Nothing connects this question to this answer but a guess. It is also a real one:
    # the live database had "Protected Veteran Status" pointing at a current-employer
    # question, and every "do you require sponsorship?" at a work-authorization answer.
    _resolved(conn, "Twilio", "q9", "Protected Veteran Status*", "current_employer")
    conn.commit()

    rows = store.forget_learned(conn, prefill.derivable_key(answers), write=True)
    assert [(r["company"], r["form_key"]) for r in rows] == [("Twilio", "q9")]
    assert set(store.known_question_keys(conn).values()) == {
        "first_name", "current_employer"}
    conn.close()


def test_forget_learned_reopens_the_gap_and_re_queues_the_plans(answers):
    """All three tables move together, or it only looks fixed.

    `known_question_keys` is the alias, `prefill_gaps` is the question you were never
    asked because the guess was believed, and a stored plan beats a fresh
    `resolve_field` in `browser._plan_index` — so a plan built on the guess keeps
    carrying it until its hash is blanked.
    """
    conn = store.connect(":memory:")
    _resolved(conn, "Twilio", "q9", "Protected Veteran Status*", "current_employer")
    store.record_gap(conn, question_key="protected_veteran_status",
                     ask="Protected Veteran Status*", field_type="text",
                     company="Twilio", now=TODAY)
    store.resolve_gap(conn, "protected_veteran_status", TODAY)
    store.record_plan(conn, "Twilio", "1", "[]", 1, 0, answers.hash, TODAY)
    conn.commit()

    store.forget_learned(conn, prefill.derivable_key(answers), write=True)

    assert store.known_question_keys(conn) == {}
    assert [g["question_key"] for g in store.open_gaps(conn)] == [
        "protected_veteran_status"]
    assert conn.execute(
        "SELECT answers_hash FROM prefill_plans").fetchone()[0] == ""
    conn.close()


def test_forget_learned_without_write_changes_nothing(answers):
    """Dry by default — `repair`'s contract, and for its reason: this rewrites what a
    run decided, and 122 lines of it is worth reading first."""
    conn = store.connect(":memory:")
    _resolved(conn, "Twilio", "q9", "Protected Veteran Status*", "current_employer")
    conn.commit()

    rows = store.forget_learned(conn, prefill.derivable_key(answers))
    assert len(rows) == 1
    assert store.known_question_keys(conn) == {
        "protected veteran status": "current_employer"}
    conn.close()


def test_a_bank_with_no_aliases_does_not_make_every_key_a_guess(answers, tmp_path):
    """`derivable_key` reads the *user's* aliases, so running it against the wrong bank
    would sweep rows a person had attached on purpose. The CLI refuses a missing bank
    outright for this reason; the predicate itself just has to be honest about what it
    was given."""
    keep = prefill.derivable_key(answers)
    assert keep("Who is your current or previous employer?", "q1")
    assert keep("First Name", "first_name")
    assert not keep("Protected Veteran Status*", "q9")

    blank = load_answers(_bare_bank(tmp_path))
    assert not prefill.derivable_key(blank)(
        "Who is your current or previous employer?", "q1")
    assert prefill.derivable_key(blank)("First Name", "first_name")   # still a rule


def _bare_bank(tmp_path):
    path = tmp_path / "bare.yaml"
    path.write_text("identity:\n  first_name: D\n  last_name: D\n  email: e@x.edu\n")
    return path


def test_a_checkbox_set_is_one_gap_not_one_per_box():
    """"How did you hear about us?" is one question. Its members share a label and differ
    only in which answer they are, so listing each of them asked the user to write an
    answer to the word "Glassdoor"."""
    members = [
        prefill.PlanEntry(form_key=f"q[]::{n}", label="How did you hear about us?",
                          type="checkbox", required=True,
                          group="How did you hear about us?", option=n)
        for n in ("LinkedIn", "Glassdoor", "A friend")
    ]
    result = prefill.PrefillResult(entries=members, form_source="dom")

    assert len(result.gaps) == 1
    assert result.gaps[0].label == "How did you hear about us?"


def test_a_stored_plan_carries_the_vocabulary_it_was_checked_against():
    """`browser._plan_index` lets a stored value beat a fresh `resolve_field`, so a plan
    that dropped its options handed a value to the form with nothing left to re-check it
    against."""
    entry = prefill.PlanEntry(form_key="q1", label="Work auth?", type="select",
                              required=True, options=("Yes", "No"), value="Yes",
                              question_key="work_authorization", source="alias")
    assert entry.as_dict()["options"] == ["Yes", "No"]
