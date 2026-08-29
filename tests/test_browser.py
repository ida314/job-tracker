"""The browser fill: what it must never do, and what it does to a real form.

Most of this file runs with no browser at all. The tests that need one are marked
`browser` and skip when the optional extra is absent, so a checkout without Playwright
still runs a clean suite — the same posture the LLM pass takes.

The browser tests drive a static HTML fixture over `file://`. Real Playwright, real DOM,
no network and no ATS. That is enough to exercise the two things that actually break:
label discovery across the four conventions forms use, and writing into each input type.
"""

import ast
import inspect
import pathlib
import re
from types import SimpleNamespace

import pytest

from jobtracker import browser, live, store
from jobtracker.answers import load_answers, normalize_label
from jobtracker.models import Company, FormField

try:  # pragma: no cover - depends on the optional extra
    import playwright.sync_api  # noqa: F401

    HAS_PLAYWRIGHT = True
except ImportError:  # pragma: no cover
    HAS_PLAYWRIGHT = False

needs_browser = pytest.mark.skipif(
    not HAS_PLAYWRIGHT, reason="pip install 'jobtracker[browser]'"
)

TODAY = "2026-08-13"

# Four label conventions in one page, because real forms use all of them: a `for=`
# label, a wrapping label, an aria-label, and a heading-ish sibling.
FORM_HTML = """
<!doctype html><meta charset=utf-8><title>Apply</title>
<form>
  <label for="fn">First Name</label>
  <input id="fn" name="first_name" required>

  <label>Email Address
    <input name="email" type="email" required>
  </label>

  <input name="phone" aria-label="Phone Number">

  <div><div class="label">Who is your current or previous employer?</div>
       <input name="question_68184536" required></div>

  <label for="ctry">Please select the country where you currently reside.</label>
  <select id="ctry" name="question_68184538" required>
    <option value="">Select…</option>
    <option>United States</option>
    <option>Canada</option>
  </select>

  <label for="cv">Resume/CV</label>
  <input id="cv" type="file" name="resume">

  <label for="why">Why do you want to work here?</label>
  <textarea id="why" name="question_777" required></textarea>

  <input type="hidden" name="csrf" value="xyz">
  <button type="submit">Submit application</button>
</form>
"""


@pytest.fixture
def answers(tmp_path):
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4 fake")
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


# -- the invariant that matters most -------------------------------------------------
def _code_without_prose(module):
    """The module's source with every docstring removed.

    Required, and the requirement is itself the old test's warning coming true: that one
    scanned the whole source and cautioned that "prose in this module says never submits
    a lot". The prose now names the mechanisms it bans, in order to explain why they are
    banned, so a scan of the raw text finds `requestSubmit` in a sentence about not using
    `requestSubmit`. Assert on the code.
    """
    src = inspect.getsource(module)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                src = src.replace(doc, "")
    return src


def test_the_only_activations_in_this_module_are_the_two_named_ones():
    """Two clicks, in two functions, and each one presses a control that belongs to it.

    This has narrowed twice, not loosened. The first rule was that there was no click
    path at all; then `_submit` gained one, behind a gate, because a **click** on the
    employer's own control runs their validation, their required-field checks and their
    captcha hooks, while `requestSubmit`, `form.submit` and a synthesized `dispatchEvent`
    skip some or all of that and send the form anyway — which is how you submit an
    application the employer's own page would have rejected.

    `_press` is here for the same reason one layer in. Greenhouse's current form has no
    `<select>` on it; every dropdown is a react-select widget holding its value in
    JavaScript, so `page.fill` typed a *search query* the widget never committed, the row
    reported `filled`, and the submit gate counted an empty field as answered. The
    widget's own option is the only thing that teaches it a value. What is asserted here
    is that this remains two functions, reached from where they say they are reached
    from — and that `_press` cannot reach a submit control, which is true structurally:
    `_DISCOVER_JS` mints no handle for one.
    """
    code = _code_without_prose(browser)
    assert code.count(".click(") == 2, "there is an activation here that is not named"
    assert ".click(" in _code_without_prose_of(browser._submit)
    assert ".click(" in _code_without_prose_of(browser._press)

    # The mechanisms that would bypass the page's own checks, not the word: prose in
    # this module says "submit" a great many times.
    for mechanism in ("requestSubmit", "form.submit", "keyboard.press", "dispatchEvent"):
        assert mechanism not in code, f"{mechanism} sends a form without pressing it"

    # And nothing reaches that click except the hold loop, which reaches it only through
    # the armed flag. A second caller is how a code path arrives at sending somebody's
    # application without anybody having asked for it.
    assert _callers_of("_submit") == ["_hold_until_closed"], _callers_of("_submit")
    # `_press` is reached from three places, and all three operate one field's own
    # widget: choosing a value in it, taking one back out, and opening it to read what it
    # offers. Anywhere else is a click on something nobody asked to be pressed. `_pick`,
    # in turn, is only ever a branch of the one writer.
    assert _callers_of("_press") == ["_clear", "_pick", "_read_vocabulary"], \
        _callers_of("_press")
    assert _callers_of("_pick") == ["_write"], _callers_of("_pick")
    # And reading a vocabulary must never *choose* one. It presses the widget's toggle
    # twice — open, read, closed again — and the only thing it returns is text. That is
    # true whether it was given a query or not: `_obey` reaches it for the `search`
    # command, which types into the widget's own search box exactly as `_pick` already
    # does before choosing, and then stops instead of pressing an option.
    assert ".click(" not in _code_without_prose_of(browser._read_vocabulary)
    assert _callers_of("_read_vocabulary") == ["_learn_vocabularies", "_obey"], \
        _callers_of("_read_vocabulary")


def _code_without_prose_of(fn) -> str:
    src = inspect.getsource(fn)
    doc = inspect.getdoc(fn)
    return src.replace(doc, "") if doc else src


def _callers_of(name: str) -> list:
    calls = re.compile(r"(?<![\w])" + name + r"\(")
    return sorted(
        other for other, obj in vars(browser).items()
        if callable(obj)
        and getattr(obj, "__module__", "") == browser.__name__
        and other != name
        and calls.search(_code_without_prose_of(obj))
    )


def test_the_module_never_navigates_anywhere_but_the_apply_url():
    source = inspect.getsource(browser)
    assert source.count("page.goto(") == 1


# -- how long the window lives ---------------------------------------------------------
# The context is closed on the way out of `fill_application`, so a caller that wants to
# hand the window to a human has to block until they are done with it. `serve` has no
# terminal to prompt at, which is why it needs a second way of waiting.
class _FakePage:
    def __init__(self, context):
        self.context = context

    def wait_for_timeout(self, ms):
        self.context.waits += 1
        if self.context.waits >= self.context.closes_after:
            self.context.open = False


class _FakeContext:
    """A context whose window closes after `closes_after` waits."""

    def __init__(self, closes_after=3, page=None):
        self.waits = 0
        self.closes_after = closes_after
        self.open = True
        # A real surface to act on, for the tests that care what reached the page. The
        # default is the ticking stub the close tests need.
        self.page = page

    @property
    def pages(self):
        if not self.open:
            return []
        return [self.page if self.page is not None else _FakePage(self)]


def test_the_hold_returns_only_once_the_window_is_gone():
    context = _FakeContext(closes_after=3)
    browser._hold_until_closed(browser.FillReport(url="x", discovered=1), context)
    assert context.waits == 3          # it waited, rather than returning immediately


def test_the_hold_waits_inside_playwright_so_the_close_can_reach_it():
    """The bug this replaced: `time.sleep` plus a look at `context.pages`.

    The sync API only dispatches driver events while a call is in flight, so a wait held
    outside one sees a frozen page list — a browser killed outright still read as one
    open page and the thread never returned, pinning the one-window lock until `serve`
    itself was restarted. Measured against a real browser, 2026-08-16.
    """
    source = inspect.getsource(browser._hold_until_closed)
    assert "wait_for_timeout" in source
    assert "time.sleep(" not in source     # the call, not the docstring naming it


def test_the_hold_ends_when_the_browser_is_gone_rather_than_raising():
    """A killed browser raises `TargetClosedError` from the next call. That is the exit."""
    class _Dead:
        @property
        def pages(self):
            raise RuntimeError("Target page, context or browser has been closed")

    browser._hold_until_closed(browser.FillReport(url="x"), _Dead())

    class _DiesMidWait:
        @property
        def pages(self):
            class _P:
                def wait_for_timeout(self, ms):
                    raise RuntimeError("Target page, context or browser has been closed")
            return [_P()]

    browser._hold_until_closed(browser.FillReport(url="x"), _DiesMidWait())


def test_serve_holds_the_window_open():
    """The bug this replaced: `wait=False` alone fills the form and closes the browser.

    Asserted against the source because the alternative is standing up a real browser
    from a real HTTP request. The symptom — a window that flashes and vanishes — reads
    as "the button does not work", the same as no handler at all.
    """
    from jobtracker import server

    source = inspect.getsource(server.Handler._api_apply_to)
    assert "hold=True" in source


def test_serve_records_a_submit_that_landed(tmp_path):
    """The callback is what keeps the submit a reading and the recording a separate act.

    Exercised rather than read for. This started as a closure nested in the wrong scope,
    so the connection it named was never bound — every recording would have raised inside
    the callback's own `except`, reaching the log and nowhere else, and a submit would
    have looked complete while landing nothing. A test that only grepped the source for
    the right words passed over exactly that.
    """
    from jobtracker import server

    conn = store.connect(tmp_path / "state.db")
    wrote = server.record_submission(
        conn, "Acme", "1", "Backend Engineer", "https://x/apply",
        {"changed": True, "note": "the page went to https://x/thanks"}, TODAY)

    assert wrote is True
    row = store.all_applications(conn)[0]
    assert (row["company"], row["status"]) == ("Acme", "applied")
    assert row["url"] == "https://x/apply"
    assert "thanks" in row["note"]
    conn.close()


def test_serve_records_nothing_for_a_submit_nobody_can_vouch_for(tmp_path):
    """`applied` is the status that stops a job coming back round."""
    from jobtracker import server

    conn = store.connect(tmp_path / "state.db")
    wrote = server.record_submission(
        conn, "Acme", "1", "Backend Engineer", "https://x/apply",
        {"changed": False, "note": "nothing on the page changed"}, TODAY)

    assert wrote is False
    assert store.all_applications(conn) == []
    conn.close()


def test_the_browser_module_knows_nothing_about_applications():
    """It reports what it saw; somebody else decides what that means."""
    from jobtracker import server

    assert "advance_application" not in inspect.getsource(browser)
    assert "on_submitted=lambda result: record_submission(" in inspect.getsource(
        server.Handler._api_apply_to)


# -- apply URLs ----------------------------------------------------------------------
@pytest.mark.parametrize("ats, url, expected", [
    ("greenhouse", "https://job-boards.greenhouse.io/stripe/jobs/1",
     "https://job-boards.greenhouse.io/stripe/jobs/1#app"),
    ("ashby", "https://jobs.ashbyhq.com/ramp/abc",
     "https://jobs.ashbyhq.com/ramp/abc/application"),
    ("lever", "https://jobs.lever.co/Onehouse/abc",
     "https://jobs.lever.co/Onehouse/abc/apply"),
    ("workday", "https://x.wd5.myworkdayjobs.com/jobs/1",
     "https://x.wd5.myworkdayjobs.com/jobs/1"),
])
def test_the_apply_url_is_derived_per_ats(ats, url, expected):
    assert browser.apply_url_for(url, ats) == expected


def test_an_apply_url_is_not_appended_twice():
    """Re-running must not produce .../application/application."""
    once = browser.apply_url_for("https://jobs.ashbyhq.com/ramp/abc", "ashby")
    assert browser.apply_url_for(once, "ashby") == once


def test_an_empty_url_stays_empty():
    assert browser.apply_url_for("", "greenhouse") == ""


# -- plan indexing --------------------------------------------------------------------
def test_a_plan_is_found_by_ats_name_or_by_label():
    """A plan built from Greenhouse's API is keyed by field name; one built from a DOM
    visit is keyed by a slug of the label. Both have to apply to the same page."""
    import json

    index = browser._plan_index(json.dumps([
        {"form_key": "question_68184536", "label": "Who is your current employer?",
         "type": "text", "value": "NYU", "question_key": "current_employer"},
        {"form_key": "why_us", "label": "Why us?", "type": "textarea",
         "value": None, "question_key": None},
    ]))
    assert index["question_68184536"]["value"] == "NYU"
    assert index["who_is_your_current_employer"]["value"] == "NYU"
    assert "why_us" not in index          # an unfilled entry is not a value to apply


def test_a_missing_plan_is_an_empty_index():
    assert browser._plan_index(None) == {}


# -- against a real DOM ----------------------------------------------------------------
@needs_browser
def test_it_fills_what_it_knows_and_names_what_it_does_not(tmp_path, answers):
    page = tmp_path / "form.html"
    page.write_text(FORM_HTML)

    conn = store.connect(":memory:")
    report = browser.fill_application(
        conn,
        company=Company(name="Stripe", ats="", slug="stripe", tier=1),
        ats_job_id="1",
        url=page.as_uri(),
        answers=answers,
        today=TODAY,
        user_data_dir=tmp_path / "profile",
        headless=True,
        wait=False,
    )

    filled = {f.label: f.value for f in report.filled}
    assert filled["First Name"] == "Dylan"
    assert filled["Email Address"] == "dyd2008@nyu.edu"
    assert filled["Phone Number"] == "+1 555 0100"
    assert filled["Who is your current or previous employer?"] == "New York University"
    assert filled["Please select the country where you currently reside."] == "United States"
    assert filled["Resume/CV"].endswith("resume.pdf")

    # The one question with no answer is reported, not silently skipped.
    assert [g.label for g in report.gaps] == ["Why do you want to work here?"]
    assert report.new_questions == 1

    # A hidden input is not a question.
    assert "csrf" not in {f.key for f in report.gaps}
    conn.close()


@needs_browser
def test_a_visit_teaches_the_system_that_companys_form(tmp_path, answers):
    """This is what makes Ashby, Lever and Workday participate in the gap loop.

    None of them publishes its questions, so the DOM is the only place the form exists —
    and having read it once, the offline prefill task can plan for that employer.
    """
    page = tmp_path / "form.html"
    page.write_text(FORM_HTML)

    conn = store.connect(":memory:")
    browser.fill_application(
        conn,
        # `ats` is blank so the fixture URL is used as-is; the per-ATS apply-URL rules
        # are covered above, and appending `/application` to a file:// path would only
        # test the fixture.
        company=Company(name="Ramp", ats="", slug="ramp", tier=1),
        ats_job_id="1",
        url=page.as_uri(),
        answers=answers,
        today=TODAY,
        user_data_dir=tmp_path / "profile",
        headless=True,
        wait=False,
    )

    fields = {r["form_key"]: r for r in store.form_fields_for(conn, "Ramp")}
    assert fields["first_name"]["question_key"] == "first_name"
    assert fields["first_name"]["source"] == "dom"
    assert fields["question_68184538"]["type"] == "select"
    assert "United States" in fields["question_68184538"]["options"]
    assert fields["question_777"]["question_key"] is None     # the gap, recorded as one
    conn.close()


@needs_browser
def test_a_dropdown_that_does_not_offer_our_answer_is_left_alone(tmp_path, answers):
    page = tmp_path / "form.html"
    page.write_text(FORM_HTML.replace("<option>United States</option>", ""))

    conn = store.connect(":memory:")
    report = browser.fill_application(
        conn,
        company=Company(name="Stripe", ats="", slug="stripe", tier=1),
        ats_job_id="1",
        url=page.as_uri(),
        answers=answers,
        today=TODAY,
        user_data_dir=tmp_path / "profile",
        headless=True,
        wait=False,
    )
    labels = {g.label for g in report.gaps}
    assert "Please select the country where you currently reside." in labels
    conn.close()


# -- the mirrored form ------------------------------------------------------------------
# `serve` fills the form and then hands you the fields rather than the window. The
# window still exists — it is where you read the application over and send it — but the
# typing happens on a page with no video stream between you and the keyboard.


class _Recorder:
    """A page that records what was asked of it. Enough to drive `_drain` with no browser."""

    def __init__(self, found=None, submit=None):
        self.found = found or []
        # What `_SUBMIT_JS` would report. None is the real "this page has no way to send
        # itself" case, which the gate has to treat as a finding rather than a blank.
        self.submit = submit if submit is not None else {
            "handle": "go", "label": "Submit application"}
        self.clicked = []
        self.url = "https://x/apply"
        self.filled = {}
        self.selected = {}
        self.checked = {}
        self.files = {}
        self.shots = 0
        self.evaluated = []

    def evaluate(self, script, arg=None):
        # The three scripts are told apart the way the page tells them apart: by what
        # they ask for. A recorder that answered them all with the field list made
        # `_find_submit` report a list of inputs as a button.
        if "data-jt-submit" in script:
            return self.submit
        self.evaluated.append(arg)
        return self.found

    def click(self, selector):
        self.clicked.append(selector)

    def wait_for_load_state(self, state, timeout=None):
        pass

    def fill(self, selector, value):
        self.filled[selector] = value

    def select_option(self, selector, value=None, label=None):
        self.selected[selector] = label if label is not None else value

    def check(self, selector):
        self.checked[selector] = True

    def uncheck(self, selector):
        self.checked[selector] = False

    def set_input_files(self, selector, files):
        self.files[selector] = files

    def screenshot(self, **kwargs):
        self.shots += 1
        self.shot_kwargs = kwargs
        return b"\xff\xd8jpeg"

    def wait_for_timeout(self, ms):
        pass


def _raw(handle, key, label, type_):
    """A DOM finding shaped exactly as `_DISCOVER_JS` returns one."""
    return {"handle": handle, "name": key, "elementId": "", "label": label,
            "type": type_, "required": False, "options": []}


def _mirror(*specs):
    session = live.start("Acme", "1", "Backend Engineer", "https://x/apply")
    found = [_raw(f"jt{i}", k, lab, t) for i, (k, lab, t) in enumerate(specs)]
    fields = [FormField(key=k, label=lab, type=t) for k, lab, t in specs]
    session.absorb(live.rows_from(found, fields))
    session.set_phase(live.READY)
    return session, found


def test_a_command_from_before_the_form_moved_is_never_written():
    """The one way this feature could put an answer you did not give into a hidden field.

    The page holds handles from the reading it rendered. If the form is read again and
    the numbering shifts, those handles now name their neighbours — so a write in flight
    has to land nowhere rather than somewhere plausible.
    """
    session, found = _mirror(("first_name", "First Name", "text"),
                             ("email", "Email", "text"))
    page = _Recorder(found)
    stale = session.epoch

    # The form grows a question, which moves every handle below it.
    grew = [_raw("jt0", "first_name", "First Name", "text"),
            _raw("jt1", "sponsorship", "Sponsorship?", "select"),
            _raw("jt2", "email", "Email", "text")]
    session.absorb(live.rows_from(
        grew,
        [FormField(key="first_name", label="First Name", type="text"),
         FormField(key="sponsorship", label="Sponsorship?", type="select"),
         FormField(key="email", label="Email", type="text")],
        session.carried(),
    ))
    assert session.epoch != stale

    session.submit(live.Command(kind=live.SET, handle="jt1", value="dyd2008@nyu.edu",
                                epoch=stale))
    browser._drain(session, page)

    assert page.filled == {}, "a stale handle reached the page"
    by_key = {r["key"]: r for r in session.snapshot()["fields"]}
    assert by_key["sponsorship"]["status"] == live.PENDING


def test_a_current_command_is_written_through_the_one_writer():
    session, found = _mirror(("first_name", "First Name", "text"))
    page = _Recorder(found)

    session.submit(live.Command(kind=live.SET, handle="jt0", value="Dylan",
                                epoch=session.epoch))
    browser._drain(session, page)

    assert page.filled == {'[data-jt-id="jt0"]': "Dylan"}
    assert session.snapshot()["fields"][0]["status"] == live.FILLED


def test_a_successful_write_reads_the_form_again():
    """Forms reveal questions once you answer others. Without this the mirror goes stale
    exactly when you are making progress."""
    session, found = _mirror(("first_name", "First Name", "text"))
    page = _Recorder(found)
    session.submit(live.Command(kind=live.SET, handle="jt0", value="Dylan",
                                epoch=session.epoch))
    browser._drain(session, page)
    # _DISCOVER_JS ran again — the re-read — and so did a screenshot.
    assert len(page.evaluated) >= 1
    assert page.shots >= 1


def test_a_field_that_would_not_take_the_value_says_so_rather_than_going_quiet():
    """Same rule the fill follows: a refusal is an outcome, not an error."""
    session, found = _mirror(("country", "Country", "select"))
    page = _Recorder(found)

    def refuse(selector, label=None):
        raise RuntimeError("no such option")

    page.select_option = refuse
    session.submit(live.Command(kind=live.SET, handle="jt0", value="Atlantis",
                                epoch=session.epoch))
    browser._drain(session, page)
    assert session.snapshot()["fields"][0]["status"] == live.REFUSED


# -- deleting ---------------------------------------------------------------------------
# Typing into the mirror was always carried to the real form. Deleting was not: an empty
# `set` reached `page.fill(selector, "")`, which succeeds, and the row was then recorded
# `filled` holding nothing — counted as done and counted out of "need you". Three of the
# four control kinds could not be emptied at all.


def test_clearing_a_field_is_not_the_same_as_filling_it_with_nothing():
    """The field empties on the real page, and the count says so.

    Both halves are the test. Landing the clear and then calling the result `filled` is
    the failure that shipped: the form is right and the page reporting on it is wrong,
    which is worse than either alone.
    """
    session, found = _mirror(("first_name", "First Name", "text"))
    page = _Recorder(found)
    session.submit(live.Command(kind=live.SET, handle="jt0", value="Dylan",
                                epoch=session.epoch))
    browser._drain(session, page)
    assert session.snapshot()["need"] == 0

    session.submit(live.Command(kind=live.CLEAR, handle="jt0", epoch=session.epoch))
    browser._drain(session, page)

    assert page.filled == {'[data-jt-id="jt0"]': ""}, "the real field still holds it"
    row = session.snapshot()["fields"][0]
    assert row["status"] == live.CLEARED
    assert row["value"] == ""
    assert session.snapshot()["need"] == 1


def test_an_empty_set_is_refused_rather_than_written_as_an_answer():
    """`set` with nothing in it is not a way to clear a field — `clear` is.

    Silently treating it as one would work for a text box and be wrong for a file input,
    where the value is a path on this machine and "" means no file rather than no text.
    """
    session, found = _mirror(("first_name", "First Name", "text"))
    page = _Recorder(found)
    session.mark("jt0", live.FILLED, "Dylan")

    session.submit(live.Command(kind=live.SET, handle="jt0", value="",
                                epoch=session.epoch))
    browser._drain(session, page)

    assert page.filled == {}
    assert session.snapshot()["fields"][0]["status"] == live.FILLED


def test_every_kind_of_control_can_be_emptied():
    """Text was the only one that worked. A tickbox reported "would not take it" and did
    nothing; a dropdown and a file input had no path at all."""
    session, found = _mirror(("agree", "I agree", "checkbox"),
                             ("country", "Country", "select"),
                             ("resume", "Resume", "file"),
                             ("why", "Why us", "textarea"))
    page = _Recorder(found)
    for handle in ("jt0", "jt1", "jt2", "jt3"):
        session.submit(live.Command(kind=live.CLEAR, handle=handle,
                                    epoch=session.epoch))
    browser._drain(session, page)

    assert page.checked == {'[data-jt-id="jt0"]': False}, "the tickbox is untouched"
    assert page.selected == {'[data-jt-id="jt1"]': []}
    assert page.files == {'[data-jt-id="jt2"]': []}
    assert page.filled == {'[data-jt-id="jt3"]': ""}
    assert [r["status"] for r in session.snapshot()["fields"]] == [live.CLEARED] * 4


def test_a_clear_from_before_the_form_moved_is_never_written():
    """The epoch rule is about handles, not about which way the value is going.

    Emptying the wrong field is exactly as bad as filling it, and on a form you are about
    to submit it is arguably worse — a blank looks like a question nobody asked.
    """
    session, found = _mirror(("first_name", "First Name", "text"),
                             ("email", "Email", "text"))
    page = _Recorder(found)
    stale = session.epoch

    grew = [_raw("jt0", "first_name", "First Name", "text"),
            _raw("jt1", "sponsorship", "Sponsorship?", "select"),
            _raw("jt2", "email", "Email", "text")]
    session.absorb(live.rows_from(
        grew,
        [FormField(key="first_name", label="First Name", type="text"),
         FormField(key="sponsorship", label="Sponsorship?", type="select"),
         FormField(key="email", label="Email", type="text")],
        session.carried(),
    ))
    assert session.epoch != stale

    session.submit(live.Command(kind=live.CLEAR, handle="jt1", epoch=stale))
    browser._drain(session, page)

    assert page.filled == {} and page.selected == {}


def test_a_control_that_will_not_empty_says_so_rather_than_going_quiet():
    """A radio button is the real case: Playwright can check one and cannot uncheck it."""
    session, found = _mirror(("contact", "Contact me", "checkbox"))
    page = _Recorder(found)

    def refuse(selector):
        raise RuntimeError("only checkboxes can be unchecked")

    page.uncheck = refuse
    session.submit(live.Command(kind=live.CLEAR, handle="jt0", epoch=session.epoch))
    browser._drain(session, page)
    assert session.snapshot()["fields"][0]["status"] == live.REFUSED


# -- the menu that has no list ----------------------------------------------------------
# Greenhouse's "Location (City)" is a react-select whose options are fetched per
# keystroke. There is nothing to open, so `_learn_vocabularies` correctly learns nothing,
# the row rendered as a plain text box, and any answer that was not character-for-
# character one of the widget's own suggestions came back "would not take it" with no way
# to find out what would have been taken. `search` is how the page asks.


class _Locator:
    """Playwright's locator, reduced to what `_pick` and `_read_vocabulary` use."""

    def __init__(self, page, present=True):
        self.page, self.present = page, present

    def count(self):
        return 1 if self.present else 0

    @property
    def first(self):
        return self

    def click(self):
        self.page.presses += 1


class _ComboPage(_Recorder):
    """A page whose one combobox offers nothing until something types into it.

    Modelled on the real widget rather than on a `<select>`: opening it shows an empty
    menu however long you wait, and the options that do arrive are a function of the
    query. That is the whole shape the `search` command exists for.
    """

    def __init__(self, found, offers):
        super().__init__(found)
        self.offers = offers          # query -> what the menu then shows
        self.query = ""
        self.presses = 0

    def locator(self, selector):
        return _Locator(self)

    def fill(self, selector, value):
        super().fill(selector, value)
        self.query = value

    def evaluate(self, script, arg=None):
        if "data-jt-submit" in script:
            return self.submit
        # `data-jt-id` is what tells the two *reading* scripts from the two *widget*
        # ones. `_DISCOVER_JS` names all three attributes — it is what sets them — so
        # discriminating on `data-jt-opt` or `data-jt-ctl` first answered a discovery
        # pass with a list of city names.
        if "data-jt-id" in script:                       # _DISCOVER_JS, _HIGHLIGHT_JS
            self.evaluated.append(arg)
            return self.found
        offered = self.offers.get(self.query, [])
        if "args.value" in script:                       # _OPTION_JS
            wanted = normalize_label(arg["value"])
            chose = next((o for o in offered if normalize_label(o) == wanted), None)
            return {"opened": bool(offered), "chose": chose, "offered": offered}
        return offered                                   # _VOCABULARY_JS


def test_a_menu_with_no_list_can_be_asked_what_it_offers():
    """`search` types the query into the widget's own box and reads the menu back.

    It chooses nothing — the presses are the widget's own toggle, open and closed again —
    and the value on the row does not move. What changes is what this side knows.
    """
    session, found = _mirror(("candidate-location", "Location (City)*", "combobox"))
    page = _ComboPage(found, {"new york": ["New York, NY, United States",
                                           "New York Mills, MN, United States"]})

    session.submit(live.Command(kind=live.SEARCH, handle="jt0", value="new york",
                                epoch=session.epoch))
    browser._drain(session, page)

    row = session.snapshot()["fields"][0]
    assert row["offered"] == ["New York, NY, United States",
                              "New York Mills, MN, United States"]
    assert row["offered_for"] == "new york"
    # Nothing was chosen and nothing was written: the field is exactly where it was.
    assert row["value"] == "" and row["status"] == live.PENDING


def test_searching_re_reads_the_form_without_spending_an_epoch():
    """Typing into a react-select remounts its input, which takes the `data-jt-id` with
    it — so without a re-reading the field just searched would be the one field on the
    form nothing else could reach. The handles do not move, so the page never notices.
    """
    session, found = _mirror(("candidate-location", "Location (City)*", "combobox"),
                             ("first_name", "First Name", "text"))
    page = _ComboPage(found, {"new": ["New York, NY, United States"]})
    before = session.epoch

    session.submit(live.Command(kind=live.SEARCH, handle="jt0", value="new",
                                epoch=session.epoch))
    browser._drain(session, page)

    assert session.epoch == before, "a search invalidated the page's handles"
    # And the re-reading did not throw the answer away: `rows_from` builds rows with no
    # offer, so `offer` has to land after it.
    assert session.snapshot()["fields"][0]["offered_for"] == "new"


def test_a_refused_dropdown_says_what_it_would_have_taken():
    """The dead end this whole feature is about.

    `_pick` reads the open menu in order to decide, so a refusal already knows the answer
    list — it was just being discarded. Publishing it is what turns the one status on the
    page you could do nothing about into a list to choose from, with no second search.
    """
    session, found = _mirror(("candidate-location", "Location (City)*", "combobox"))
    page = _ComboPage(found, {"New York, NY": ["New York, NY, United States"]})

    # What the answer bank holds — a perfectly reasonable answer the menu does not offer.
    session.submit(live.Command(kind=live.SET, handle="jt0", value="New York, NY",
                                epoch=session.epoch))
    browser._drain(session, page)

    row = session.snapshot()["fields"][0]
    assert row["status"] == live.REFUSED
    assert row["offered"] == ["New York, NY, United States"]
    assert row["offered_for"] == "New York, NY"


def test_choosing_one_of_its_own_wordings_is_taken():
    """The other half: a value the menu produced is one `_pick` is guaranteed to find."""
    session, found = _mirror(("candidate-location", "Location (City)*", "combobox"))
    page = _ComboPage(found, {"New York, NY, United States":
                              ["New York, NY, United States"]})

    session.submit(live.Command(kind=live.SET, handle="jt0",
                                value="New York, NY, United States",
                                epoch=session.epoch))
    browser._drain(session, page)

    row = session.snapshot()["fields"][0]
    assert row["status"] == live.FILLED
    assert row["value"] == "New York, NY, United States"


def test_a_menu_showing_something_else_is_still_asked_for_our_answer():
    """`_pick` used to type only when the menu came up *empty*.

    That was written for the place-lookup case and is too narrow by one word. A menu
    showing anything at all was refused without ever being asked the question — and a
    menu showing something is the ordinary state of one that has been searched before, or
    one that opens on a default list. Found by driving a real async combobox on
    2026-08-29: with a previous lookup's query still in the box, choosing the answer from
    the list that lookup had produced was refused, quoting the leftover list back.

    `search` is what made this routine, so it is fixed here rather than worked around by
    clearing the box: the page must be able to look something up and then pick it.
    """
    session, found = _mirror(("candidate-location", "Location (City)*", "combobox"))
    page = _ComboPage(found, {
        "newark": ["Newark, NJ, United States"],
        "New York, NY, United States": ["New York, NY, United States"],
    })
    page.query = "newark"        # what a lookup a moment ago left in the widget

    session.submit(live.Command(kind=live.SET, handle="jt0",
                                value="New York, NY, United States",
                                epoch=session.epoch))
    browser._drain(session, page)

    row = session.snapshot()["fields"][0]
    assert row["status"] == live.FILLED, row
    assert row["value"] == "New York, NY, United States"


def test_a_menu_that_is_already_showing_the_answer_is_not_typed_into():
    """The other side of that condition: a static Yes/No costs what it always did."""
    session, found = _mirror(("q1", "Work auth?", "combobox"))
    page = _ComboPage(found, {"": ["Yes", "No"]})

    session.submit(live.Command(kind=live.SET, handle="jt0", value="Yes",
                                epoch=session.epoch))
    browser._drain(session, page)

    assert session.snapshot()["fields"][0]["status"] == live.FILLED
    assert page.filled == {}, "it searched a menu that was already offering the answer"


def test_focusing_a_row_outlines_that_field_on_the_real_page():
    """`highlight` sat in the vocabulary with no route emitting it until the preview
    became the only view. A form is several thousand pixels; the outline is what ties the
    row under your cursor to a place in the picture."""
    session, found = _mirror(("first_name", "First Name", "text"),
                             ("why", "Why us", "textarea"))
    page = _Recorder(found)
    session.submit(live.Command(kind=live.HIGHLIGHT, handle="jt1",
                                epoch=session.epoch))
    browser._drain(session, page)

    assert ["jt1"] in page.evaluated
    # It moves no value, so nothing about the row changes.
    assert page.filled == {}
    assert session.snapshot()["fields"][1]["status"] == live.PENDING


def test_no_screenshots_are_taken_for_a_page_nobody_is_looking_at():
    session, found = _mirror(("first_name", "First Name", "text"))
    page = _Recorder(found)
    browser._drain(session, page)          # nobody watching
    assert page.shots == 0

    session.watch()
    browser._drain(session, page)
    assert page.shots == 1


def test_the_drain_survives_a_command_that_fails():
    """One bad command must not end a session you are halfway through filling in."""
    session, found = _mirror(("first_name", "First Name", "text"))
    page = _Recorder(found)

    def explode(script, arg=None):
        raise RuntimeError("the page is busy")

    page.evaluate = explode
    session.submit(live.Command(kind=live.REDISCOVER))
    session.submit(live.Command(kind=live.SET, handle="jt0", value="Dylan",
                                epoch=session.epoch))
    browser._drain(session, page)
    assert page.filled == {'[data-jt-id="jt0"]': "Dylan"}


def test_the_window_closing_ends_the_session():
    """Otherwise the page polls a form nobody is holding and every edit queues into
    nothing — a mirror that looks live over a browser that is gone."""
    session, _ = _mirror(("first_name", "First Name", "text"))
    browser._hold_until_closed(browser.FillReport(url="https://x"),
                               _FakeContext(0), session)
    assert session.snapshot()["phase"] == live.CLOSED
    assert session.submit(live.Command(kind=live.SET, handle="jt0", value="x")) is False


@needs_browser
def test_the_mirror_writes_to_the_real_form(tmp_path, answers):
    """End to end against a real browser: type on the page, land in the DOM.

    The whole feature is this one hop — a value typed somewhere with no latency reaching
    a form field on a machine that may be somewhere else entirely.
    """
    page_file = tmp_path / "form.html"
    page_file.write_text(FORM_HTML)
    session = live.start("Stripe", "1", "Backend Engineer", page_file.as_uri())

    conn = store.connect(":memory:")
    # The one gap this fixture leaves — "Why do you want to work here?" — is what a
    # human would type, so it is what the mirror has to be able to send.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context = browser._launch(pw, tmp_path / "profile", headless=True)
        try:
            page = context.new_page()
            page.goto(page_file.as_uri())
            found = page.evaluate(browser._DISCOVER_JS)
            fields = browser._fields_from_dom(found)
            session.absorb(live.rows_from(found, fields))
            session.set_phase(live.READY)

            row = next(r for r in session.snapshot()["fields"]
                       if r["label"] == "Why do you want to work here?")
            session.submit(live.Command(kind=live.SET, handle=row["handle"],
                                        value="Distributed systems, early.",
                                        epoch=session.epoch))
            session.watch()
            browser._drain(session, page)

            assert page.input_value("#why") == "Distributed systems, early."
            assert session.shot, "no preview was taken while watching"
            assert session.shot[:2] == b"\xff\xd8", "the preview is not a JPEG"
        finally:
            context.close()
    conn.close()


# -- sending it -------------------------------------------------------------------------


def _armed(*specs):
    """A session with a submit control, every required field filled, and the gate passed."""
    session, found = _mirror(*specs)
    session.set_submit_control({"handle": "go", "label": "Submit application"})
    ok, error = session.request_submit(session.epoch, "Acme")
    assert ok, error
    return session, found


def test_the_click_happens_only_once_however_often_the_flag_is_read():
    """The hold loop reads the flag every 500ms. Two reads must not be two applications."""
    session, found = _armed(("first_name", "First Name", "text"))
    page = _Recorder(found)

    browser._submit(session, page)
    browser._submit(session, page)
    browser._submit(session, page)

    assert page.clicked == ['[data-jt-submit="go"]']
    assert session.snapshot()["phase"] == live.SUBMITTED


def test_the_page_is_checked_again_before_the_click_not_just_at_the_gate():
    """The gate's reading is up to one poll old, and a form can reveal a required
    question in that time. Standing down leaves the submit unspent, so the button comes
    back rather than the session jamming on a state it never reached."""
    session, found = _mirror(("first_name", "First Name", "text"))
    session.set_submit_control({"handle": "go", "label": "Submit application"})
    ok, _ = session.request_submit(session.epoch, "Acme")
    assert ok

    # Between arming and the tick, the form reveals a required question.
    grew = [_raw("jt0", "first_name", "First Name", "text"),
            _raw("jt1", "sponsorship", "Sponsorship?", "select")]
    session.absorb(live.rows_from(
        grew,
        [FormField(key="first_name", label="First Name", type="text"),
         FormField(key="sponsorship", label="Sponsorship?", type="select",
                   required=True)],
        session.carried(),
    ))

    page = _Recorder(grew)
    browser._submit(session, page)

    assert page.clicked == [], "it sent an application over an unanswered question"
    snap = session.snapshot()
    assert snap["phase"] == live.READY
    assert snap["submitted"] is False
    assert "Sponsorship?" in snap["note"]


def test_a_form_that_lost_its_button_between_arming_and_the_tick_is_not_sent():
    session, found = _armed(("first_name", "First Name", "text"))
    session.set_submit_control(None)
    page = _Recorder(found)

    browser._submit(session, page)

    assert page.clicked == []
    assert session.snapshot()["phase"] == live.READY
    assert "no longer on the form" in session.snapshot()["note"]


def test_what_happened_after_the_click_is_recorded_rather_than_assumed():
    """A page that navigated and a page that did not are two different readings."""
    session, found = _armed(("first_name", "First Name", "text"))
    page = _Recorder(found)
    page.url = "https://x/apply"

    browser._submit(session, page)

    result = session.snapshot()["submit_result"]
    assert result["changed"] is False, "nothing about that page changed"
    assert "nothing on the page changed" in result["note"]
    assert "successful" not in result["note"]


def test_a_page_that_navigates_after_the_click_says_where_it_went():
    session, found = _armed(("first_name", "First Name", "text"))

    class _Navigates(_Recorder):
        def click(self, selector):
            self.clicked.append(selector)
            self.url = "https://x/thanks"

    page = _Navigates(found)
    browser._submit(session, page)

    result = session.snapshot()["submit_result"]
    assert result["changed"] is True
    assert "https://x/thanks" in result["note"]


def test_the_hold_loop_sends_it_and_then_goes_back_to_draining():
    """The one caller. It reads the flag in the tick it was already doing, before the
    drain, so a queued edit cannot land between the checks and the click."""
    session, found = _armed(("first_name", "First Name", "text"))

    class _Closing(_Recorder):
        """A recorder that also ends the hold, the way `_FakePage` does."""

        context = None

        def wait_for_timeout(self, ms):
            self.context.waits += 1
            if self.context.waits >= self.context.closes_after:
                self.context.open = False

    page = _Closing(found)
    context = _FakeContext(3, page=page)
    page.context = context

    browser._hold_until_closed(browser.FillReport(url="https://x"), context, session,
                               page)

    assert page.clicked == ['[data-jt-submit="go"]']
    assert session.snapshot()["submitted"] is True


def test_the_application_is_recorded_only_once_and_carries_what_was_seen():
    recorded = []
    session, found = _armed(("first_name", "First Name", "text"))
    page = _Recorder(found)

    browser._submit(session, page, recorded.append)
    browser._submit(session, page, recorded.append)

    assert len(recorded) == 1
    assert recorded[0]["url_after"] == "https://x/apply"
    assert "changed" in recorded[0]


def test_a_failure_to_record_does_not_lose_the_reading():
    """The submit already happened. Losing what was observed is the one thing that would
    make it unverifiable."""
    session, found = _armed(("first_name", "First Name", "text"))
    page = _Recorder(found)

    def explode(_result):
        raise RuntimeError("the database is gone")

    browser._submit(session, page, explode)
    assert session.snapshot()["phase"] == live.SUBMITTED
    assert session.snapshot()["submit_result"] is not None


# A fixture of its own rather than a checkbox added to FORM_HTML: that one's gap list is
# asserted exactly, and every kind of control needs to be emptied here.
CLEARABLE_HTML = """
<!doctype html><meta charset=utf-8><title>Apply</title>
<form>
  <label for="fn">First Name</label>
  <input id="fn" name="first_name" value="Dylan">

  <label for="ctry">Country</label>
  <select id="ctry" name="country">
    <option value="">Select…</option>
    <option selected>United States</option>
  </select>

  <label for="cv">Resume/CV</label>
  <input id="cv" type="file" name="resume">

  <label class="cbx" for="agree">I agree</label>
  <input id="agree" name="agree" type="checkbox" checked>
</form>
"""


@needs_browser
def test_deleting_on_the_page_empties_the_real_form(tmp_path):
    """The other half of the mirror, end to end: take it back off.

    Read against a real browser because three of the four are Playwright semantics, not
    ours — `select_option([])` deselecting, `set_input_files([])` detaching, and `uncheck`
    firing the `change` a controlled component listens for. A unit test with a fake page
    asserts we called them; only this asserts they do what we think.
    """
    page_file = tmp_path / "form.html"
    page_file.write_text(CLEARABLE_HTML)
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4 fake")
    session = live.start("Stripe", "1", "Backend Engineer", page_file.as_uri())

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context = browser._launch(pw, tmp_path / "profile", headless=True)
        try:
            page = context.new_page()
            page.goto(page_file.as_uri())
            page.set_input_files("#cv", str(tmp_path / "resume.pdf"))

            found = page.evaluate(browser._DISCOVER_JS)
            fields = browser._fields_from_dom(found)
            session.absorb(live.rows_from(found, fields))
            session.set_phase(live.READY)

            assert page.input_value("#fn") == "Dylan"
            assert page.input_value("#ctry") == "United States"
            assert page.is_checked("#agree")
            assert page.evaluate("document.getElementById('cv').files.length") == 1

            by_key = {r["key"]: r["handle"] for r in session.snapshot()["fields"]}
            for key in ("first_name", "country", "resume", "agree"):
                session.submit(live.Command(kind=live.CLEAR, handle=by_key[key],
                                            epoch=session.epoch))
            browser._drain(session, page)

            assert page.input_value("#fn") == ""
            assert page.input_value("#ctry") == ""
            assert not page.is_checked("#agree")
            assert page.evaluate("document.getElementById('cv').files.length") == 0

            statuses = {r["key"]: r["status"] for r in session.snapshot()["fields"]}
            assert set(statuses.values()) == {live.CLEARED}
        finally:
            context.close()


# Decoys on purpose. A real application form carries several buttons and picking the
# wrong one is at best a lost fill — Greenhouse's own pages have "Back", employers add
# cookie banners, and "Apply" is also what the button that *opens* a form says.
SUBMITTABLE_HTML = """
<!doctype html><meta charset=utf-8><title>Apply</title>
<form onsubmit="document.body.setAttribute('data-sent', '1'); return false;">
  <label for="fn">First Name</label>
  <input id="fn" name="first_name" value="Dylan" required>
  <button type="button">Cancel</button>
  <button type="button">Back</button>
  <span role="button">Accept cookies</span>
  <button type="submit">Submit application</button>
</form>
"""


@needs_browser
def test_the_submit_control_is_found_among_the_decoys_and_actually_pressed(tmp_path):
    """End to end: the right button, and a real press of it.

    A press rather than a programmatic send, because the employer's own validation,
    required-field checks and captcha hooks hang off the real event — which is what
    submitting the form directly would skip. Asserted here through the page's own
    `onsubmit`, which only fires for the genuine article.
    """
    page_file = tmp_path / "form.html"
    page_file.write_text(SUBMITTABLE_HTML)
    session = live.start("Acme", "1", "Backend Engineer", page_file.as_uri())

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context = browser._launch(pw, tmp_path / "profile", headless=True)
        try:
            page = context.new_page()
            page.goto(page_file.as_uri())
            found = page.evaluate(browser._DISCOVER_JS)
            fields = browser._fields_from_dom(found)
            session.absorb(live.rows_from(found, fields))

            control = browser._find_submit(page)
            assert control is not None, "it found nothing to press"
            assert control["label"] == "Submit application"

            session.set_submit_control(control)
            session.mark(session.snapshot()["fields"][0]["handle"], live.FILLED, "Dylan")
            session.set_phase(live.READY)
            ok, error = session.request_submit(session.epoch, "Acme")
            assert ok, error

            assert page.get_attribute("body", "data-sent") is None
            browser._submit(session, page)
            assert page.get_attribute("body", "data-sent") == "1"
            assert session.snapshot()["phase"] == live.SUBMITTED
        finally:
            context.close()


@needs_browser
def test_a_page_with_nothing_to_press_reports_that_rather_than_guessing(tmp_path):
    """Zero candidates is a finding. Pressing "Cancel" because it was the only button
    left is the failure this ranking exists to prevent."""
    page_file = tmp_path / "form.html"
    page_file.write_text("""
<!doctype html><meta charset=utf-8><title>Apply</title>
<form><input name="first_name"><button type="button">Cancel</button></form>
""")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context = browser._launch(pw, tmp_path / "profile", headless=True)
        try:
            page = context.new_page()
            page.goto(page_file.as_uri())
            assert browser._find_submit(page) is None
        finally:
            context.close()


# -- where the form actually is ---------------------------------------------------------
def test_greenhouse_is_pointed_at_the_form_not_at_the_board():
    """The board URL is not the form URL, and half the boards prove it.

    `job-boards.greenhouse.io/{slug}/jobs/{id}` is whatever the employer configured it to
    be: measured across every Greenhouse board we track on 2026-08-19, 25 of 45 redirect
    it to the employer's own careers site — Asana's lands on a JS shell whose form is a
    cross-origin iframe, Cedar's answers 403 — and the browser found zero fields there.
    The embed URL is the form itself, is keyless, and was carrying it on all 45.
    """
    assert browser.apply_url_for(
        "https://www.asana.com/jobs/apply/7766762?gh_jid=7766762",
        "greenhouse", "asana", "7766762",
    ) == "https://job-boards.greenhouse.io/embed/job_app?for=asana&token=7766762"

    # Without both halves there is nothing to build it from, so the employer's own URL
    # stands — with the anchor, which is all we can do for it.
    assert browser.apply_url_for(
        "https://job-boards.greenhouse.io/stripe/jobs/1", "greenhouse", "", "1"
    ) == "https://job-boards.greenhouse.io/stripe/jobs/1#app"


@needs_browser
def test_a_form_inside_an_iframe_is_found_rather_than_reported_as_absent(tmp_path,
                                                                        answers):
    """Embedding the ATS is ordinary practice, and `page.evaluate` cannot see into it.

    `page.evaluate` runs in the main frame only, so an employer page that renders its
    application in an iframe reads as having no form at all — which is the *"no
    application form found"* card the user saw for a job with thirty-one fields on it.
    Zero discovered is the one reading this project may never take at face value.
    """
    (tmp_path / "form.html").write_text(FORM_HTML)
    host = tmp_path / "host.html"
    host.write_text(
        "<!doctype html><meta charset=utf-8><title>Careers</title>"
        "<h1>Work with us</h1><iframe src='form.html' width=800 height=600></iframe>"
    )

    conn = store.connect(":memory:")
    report = browser.fill_application(
        conn,
        company=Company(name="Asana", ats="", slug="asana", tier=5),
        ats_job_id="1",
        url=host.as_uri(),
        answers=answers,
        today=TODAY,
        user_data_dir=tmp_path / "profile",
        headless=True,
        wait=False,
    )
    assert report.found_a_form
    assert {f.label: f.value for f in report.filled}["First Name"] == "Dylan"
    assert [g.label for g in report.gaps] == ["Why do you want to work here?"]
    conn.close()


def test_a_page_with_a_form_of_its_own_is_never_read_through_its_frames():
    """The frames are the fallback, not the rule — an analytics iframe is not the form."""
    class _Frame:
        url = "https://tag-manager.example/ns.html"

        def evaluate(self, script, arg=None):
            raise AssertionError("the main frame had the form; nothing else was needed")

    class _Page:
        frames = [_Frame()]
        main_frame = None

        def evaluate(self, script, arg=None):
            return [_raw("jt0", "first_name", "First Name", "text")]

    found, surface = browser._discover(_Page())
    assert [f["handle"] for f in found] == ["jt0"]
    assert isinstance(surface, _Page)


# -- ending a session from somewhere that is not the machine holding it -----------------
def test_the_window_can_be_closed_from_the_page_that_mirrors_it():
    """The browser opens on the machine running `serve`. On a headless host that is
    nowhere you can click, so before this the only ending `_hold_until_closed` knew about
    could not happen: the window stayed up, the one-window lock stayed held, and every
    later "Open prefilled" was refused until `serve` was restarted."""
    session, _ = _mirror(("first_name", "First Name", "text"))
    context = _FakeContext(closes_after=999)     # nobody is going to close this window
    session.request_close()

    browser._hold_until_closed(browser.FillReport(url="https://x", discovered=1),
                               context, session)

    assert session.snapshot()["phase"] == live.CLOSED
    # And it returned rather than waiting the window out, which is what releases the lock.
    assert context.waits == 0


def test_the_preview_is_the_whole_page_not_the_window():
    """A viewport is 720px and an application form is several thousand.

    Measured on Asana's, 2026-08-19: 1280x3352. A viewport-shaped shot showed five fields
    of thirty-two, over a window nobody watching `/apply` can scroll — so the picture was
    of the browser rather than of the form. Scaling it back down is the page's business
    and costs no bytes; what cannot be recovered on that side is the part never captured.
    """
    session, _ = _mirror(("first_name", "First Name", "text"))
    page = _Recorder()
    session.watch()
    browser._shoot(session, page)

    assert page.shot_kwargs.get("full_page") is True
    assert page.shot_kwargs.get("type") == "jpeg"
    # And the cadence pays for it: the whole page is ~8x the bytes of the viewport.
    assert browser.SHOT_EVERY_S >= 4.0


# -- the modern Greenhouse form, as it actually is --------------------------------------
# `tests/fixtures/greenhouse_react_form.html` is Twilio's real embedded application form,
# captured 2026-08-23. Everything below asserts against it rather than against a hand-
# written approximation, because every bug it is here for came from the gap between what
# a form was assumed to look like and what one looks like. It has **no `<select>` on it
# at all**: ten dropdowns, each a react-select combobox with a phantom validation input
# beside it, and one nine-box checkbox set that is a single question.
REAL_FORM = pathlib.Path(__file__).parent / "fixtures" / "greenhouse_react_form.html"


def _read_real_form(tmp_path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser_ = pw.chromium.launch(headless=True)
        page = browser_.new_page()
        page.goto(REAL_FORM.as_uri(), wait_until="domcontentloaded")
        found = page.evaluate(browser._DISCOVER_JS)
        browser_.close()
    return found, browser._fields_from_dom(found)


@needs_browser
def test_a_dropdown_is_read_as_a_dropdown_and_not_as_a_text_box(tmp_path):
    """Greenhouse's current form has no `<select>` on it; every menu is a combobox.

    Read as text, `page.fill` typed a search query the widget never committed — so the
    field reported `filled` while the real form held nothing, and the submit gate counted
    it as answered. The type is what separates "type anything" from "choose one of these".
    """
    _, fields = _read_real_form(tmp_path)
    by_key = {f.key: f for f in fields}

    assert by_key["country"].type == "combobox"
    assert by_key["question_65614029"].type == "combobox"   # work authorization
    assert not any(f.type == "text" and f.key == "country" for f in fields)
    # And the plain text inputs are still plain text inputs.
    assert by_key["first_name"].type == "text"
    assert by_key["resume"].type == "file"


@needs_browser
def test_a_widgets_phantom_validation_input_is_not_a_question(tmp_path):
    """react-select renders `<input required tabindex="-1" aria-hidden="true">` beside
    every combobox to drive native validation.

    It has no name and no id, so it keyed on a slug of the same label as the widget it
    shadows: one dropdown, two identical rows on `/apply`, both marked required. And
    because `Session.carried()` is keyed by field key, the phantom's stale value was
    handed back to the real row on the next reading — which is what made typing into
    Country revert to the prefilled "New York, New York" one poll later.
    """
    _, fields = _read_real_form(tmp_path)

    keys = [f.key for f in fields]
    assert len(keys) == len(set(keys)), "two fields share a key; one will overwrite the other"
    # The pair this was found through: the combobox `id="country"` and a phantom that
    # slugified "Country*" to the same string.
    assert keys.count("country") == 1
    labels = [f.label for f in fields]
    assert labels.count("Country*") == 1


@needs_browser
def test_a_checkbox_set_is_one_question_with_nine_answers(tmp_path):
    """"How did you hear about Twilio?" is one question. It used to be nine.

    Each box carries its own label — "LinkedIn", "Glassdoor", "Careers Website" — and
    reading those as questions put all nine into the gap list and into `answers.yaml`'s
    stub block, asking the user to write an answer to the word "Glassdoor".
    """
    _, fields = _read_real_form(tmp_path)
    heard = [f for f in fields if f.group == "How did you hear about Twilio? *"]

    assert len(heard) == 9, [f.label for f in fields if "hear" in f.label.lower()]
    assert {f.label for f in heard} == {"How did you hear about Twilio? *"}
    assert "LinkedIn" in {f.option for f in heard}
    # Every member knows the whole vocabulary, which is what lets `match_option` check an
    # answer and what lets the page render the set as a menu.
    assert "Glassdoor" in heard[0].options
    # And it is one gap, not nine.
    assert len(browser._one_per_question(heard)) == 1

    # A lone consent checkbox is not a menu. Grouping it would invent a question whose
    # only option is also its own label.
    consent = [f for f in fields if f.option == "Acknowledge"]
    assert consent == [], [f.label for f in consent]


@needs_browser
def test_a_combobox_learns_its_vocabulary_from_what_the_ats_published(tmp_path):
    """A combobox never carries its own options; Greenhouse's API publishes all of them.

    The keys agree across the two sources — the API calls the question
    `question_65614029` and the rendered input carries that as its `id` — so a form read
    once through the API answers what a DOM reading cannot see.
    """
    _, fields = _read_real_form(tmp_path)
    known = {"question_65614029": ["Yes", "No"]}

    lent = {f.key: f for f in browser._with_known_options(fields, known)}
    assert lent["question_65614029"].options == ("Yes", "No")
    # And nothing else is touched: a field that had options keeps them, and one nobody
    # published stays honestly empty rather than borrowing somebody else's.
    assert lent["country"].options == ()
    assert lent["first_name"].options == ()


@needs_browser
def test_the_location_field_is_the_one_menu_with_nothing_to_open(tmp_path):
    """Why `search` had to exist, measured on the form rather than argued from types.

    Ten comboboxes, and nine of them carry a "Toggle flyout" button in their indicators:
    a list you can open and read, which is what `_learn_vocabularies` does once per
    company and keeps forever. *Location (City)* carries none, because there is nothing
    to toggle — its options are fetched per keystroke by a place lookup, so an opened menu
    is genuinely empty and stays empty however long you wait.

    That is the whole reason it rendered as a text box on `/apply` and the whole reason an
    answer like "New York, NY" came back *"would not take it"*: nothing offline can learn
    what it accepts, and the only thing that can is the widget, asked.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=True)
        page = chromium.new_page()
        page.goto(REAL_FORM.as_uri(), wait_until="domcontentloaded")
        found = page.evaluate(browser._DISCOVER_JS)
        fields = browser._fields_from_dom(found)
        toggles = {}
        for raw, field_ in zip(found, fields):
            if field_.type != "combobox":
                continue
            toggles[field_.key] = page.evaluate(
                """(h) => {
                    const ctl = document.querySelector(`[data-jt-ctl="${h}"]`);
                    const ind = ctl && ctl.querySelector('[class*="indicators"]');
                    return ind ? ind.querySelectorAll('button').length : 0;
                }""",
                raw["handle"],
            )
        # And what reading it without typing gets you, which is the fill's own path.
        location = next(r for r, f in zip(found, fields)
                        if f.key == "candidate-location")
        empty = browser._read_vocabulary(page, location["handle"])
        chromium.close()

    assert len(toggles) == 10, toggles
    assert toggles["candidate-location"] == 0
    assert all(n == 1 for key, n in toggles.items()
               if key != "candidate-location"), toggles
    assert empty == [], "the menu is not empty, so the premise of `search` is wrong"


# -- what an employer's copy of the resume is called -------------------------------------
def test_the_resume_goes_out_under_a_name_a_person_would_have_chosen(tmp_path):
    """Playwright sends the basename on disk, and the disk names here are minted for
    collision safety: `twilio_7816159_1f4c9a02.pdf`, or one with a field handle in it.
    That is what a recruiter opens."""
    cv = tmp_path / "twilio_7816159_1f4c9a02.pdf"
    cv.write_bytes(b"%PDF-1.4 x")

    assert browser._upload(str(cv)) == str(cv)          # unset: the disk name
    payload = browser._upload(str(cv), "Dylan Dodds Resume.pdf")
    assert payload["name"] == "Dylan Dodds Resume.pdf"
    assert payload["mimeType"] == "application/pdf"
    assert payload["buffer"] == b"%PDF-1.4 x"


def test_the_extension_always_comes_from_the_real_file():
    """So renaming a PDF to `.docx` in a text box cannot mislabel what is attached."""
    answers = SimpleNamespace(resume_name="Dylan_Dodds_Resume.docx")

    assert browser._upload_name(answers, "resume", "/x/y.pdf") == "Dylan_Dodds_Resume.pdf"
    assert browser._upload_name(answers, "cover_letter", "/x/y.pdf") == "cover_letter.pdf"
    # Nothing else is a file, and nothing else gets renamed.
    assert browser._upload_name(answers, "first_name", "Dylan") == ""
    # No setting means `resume<ext>`, not the ugly disk name.
    assert browser._upload_name(SimpleNamespace(resume_name=""), "resume",
                                "/x/twilio_1_ab.pdf") == "resume.pdf"


# -- one question is one gap, on the report as well as in the database -------------------
@needs_browser
def test_the_gap_list_names_a_question_once(tmp_path, answers):
    """The count on the card and the list `apply-to` prints have to say the same thing as
    the database, or a nine-box question reads as nine things left to do."""
    page = tmp_path / "form.html"
    page.write_text(REAL_FORM.read_text())

    conn = store.connect(":memory:")
    report = browser.fill_application(
        conn,
        company=Company(name="Twilio", ats="", slug="twilio", tier=2),
        ats_job_id="7816159",
        url=page.as_uri(),
        answers=answers,
        today=TODAY,
        user_data_dir=tmp_path / "profile",
        headless=True,
        wait=False,
    )

    heard = [g for g in report.gaps if g.label.startswith("How did you hear")]
    assert len(heard) == 1, [g.label for g in report.gaps]
    rows = conn.execute(
        "SELECT question_key FROM prefill_gaps WHERE ask LIKE 'How did you hear%'"
    ).fetchall()
    assert [r["question_key"] for r in rows] == ["how_did_you_hear_about_twilio"]
    # And the phone-number country selector is not filled from an identity location.
    assert "Country*" not in {f.label for f in report.filled}
    conn.close()


# -- emptying the whole form -----------------------------------------------------------
def test_reset_empties_what_is_holding_something_and_leaves_a_gap_alone():
    """`cleared` and `gap` are two different answers and reset may only produce one.

    A gap is a question nobody ever had an answer for; `cleared` is one you emptied on
    purpose. Marking every row `cleared` would spend the distinction the two statuses
    exist to draw — and would say the reset did something to thirty fields when it
    touched one.
    """
    session, found = _mirror(("first_name", "First Name", "text"),
                             ("why", "Why us?", "textarea"))
    session.mark("jt0", live.FILLED, "Dylan")
    page = _Recorder(found)

    browser._reset(session, page)

    rows = {r["handle"]: r for r in session.snapshot()["fields"]}
    assert rows["jt0"]["status"] == live.CLEARED and rows["jt0"]["value"] == ""
    # Never touched, and never claimed to have been.
    assert rows["jt1"]["status"] == live.PENDING
    assert page.filled == {'[data-jt-id="jt0"]': ""}


def test_reset_reads_the_form_once_rather_than_after_every_field():
    """The reason it is one command and not a loop of clears from the page.

    `_clear` on its own path re-reads the form on the way out. Thirty of those is thirty
    chances for the shape to change under the remaining handles — and attaching or
    detaching a file is exactly that change — after which every later clear is correctly
    dropped as stale. The page would then report a whole reset over four emptied fields
    of thirty, on a form about to be sent.
    """
    session, found = _mirror(("first_name", "First Name", "text"),
                             ("email", "Email", "text"),
                             ("why", "Why us?", "textarea"))
    for handle in ("jt0", "jt1", "jt2"):
        session.mark(handle, live.FILLED, "x")
    page = _Recorder(found)

    browser._reset(session, page)

    assert len(page.filled) == 3
    # `evaluate` records one arg per discovery pass — `_find_submit` answers off the
    # script it was handed and is not counted.
    assert len(page.evaluated) == 1


def test_a_field_that_will_not_empty_keeps_saying_it_is_holding_something():
    """`refused` is a real outcome here as it is for a single clear.

    A combobox with no clear indicator cannot be emptied, and a row reporting `cleared`
    over a widget still holding an answer would be counted out of "need you" and off the
    submit gate's blocker list — the reset's own version of the emptied field that read
    as filled.
    """
    class _Empty:
        def count(self):
            return 0

    class _NoClearControl(_Recorder):
        def locator(self, selector):
            return _Empty()

    session, found = _mirror(("country", "Country", "combobox"))
    session.mark("jt0", live.FILLED, "United States +1")
    page = _NoClearControl(found)

    browser._reset(session, page)

    row = session.snapshot()["fields"][0]
    assert row["status"] == live.REFUSED
    assert row["value"] == "United States +1"


def test_reset_is_reachable_from_the_queue_and_needs_no_epoch():
    """The one command that carries no epoch, because it names no handle from outside.

    A form that has moved under the page refuses every per-field command by design — the
    handles the page is holding name their neighbours now. Reset is what still works
    there, so requiring the epoch it cannot have would take the way out of that state
    away in exactly the case it exists for.
    """
    session, found = _mirror(("first_name", "First Name", "text"))
    session.mark("jt0", live.FILLED, "Dylan")
    page = _Recorder(found)

    assert session.submit(live.Command(kind=live.RESET)) is True
    session.epoch += 99                       # the form moved under the page
    browser._drain(session, page)

    assert page.filled == {'[data-jt-id="jt0"]': ""}


@needs_browser
def test_resetting_empties_the_real_form_in_one_pass(tmp_path):
    """The same four controls as the clear test, but reached by one command.

    Read against a real browser for the same reason: three of the four are Playwright
    semantics rather than ours, and a fake page only asserts we called them. What this
    adds is that one command reaches all of them — the property that made `reset` a name
    of its own instead of a loop of clears from a page whose handles go stale the first
    time emptying something changes the form's shape.
    """
    page_file = tmp_path / "form.html"
    page_file.write_text(CLEARABLE_HTML)
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4 fake")
    session = live.start("Stripe", "1", "Backend Engineer", page_file.as_uri())

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context = browser._launch(pw, tmp_path / "profile", headless=True)
        try:
            page = context.new_page()
            page.goto(page_file.as_uri())
            page.set_input_files("#cv", str(tmp_path / "resume.pdf"))

            found = page.evaluate(browser._DISCOVER_JS)
            fields = browser._fields_from_dom(found)
            session.absorb(live.rows_from(found, fields))
            session.set_phase(live.READY)
            for row in session.snapshot()["fields"]:
                # What the fill would have left behind: everything holding something.
                session.mark(row["handle"], live.FILLED, "x")

            session.submit(live.Command(kind=live.RESET))
            browser._drain(session, page)

            assert page.input_value("#fn") == ""
            assert page.input_value("#ctry") == ""
            assert not page.is_checked("#agree")
            assert page.evaluate("document.getElementById('cv').files.length") == 0

            statuses = {r["key"]: r["status"] for r in session.snapshot()["fields"]}
            assert set(statuses.values()) == {live.CLEARED}
        finally:
            context.close()


def test_a_browser_that_will_not_start_is_not_reported_as_a_missing_one():
    """The message that cost two evenings, in both directions.

    A launch failure used to send its real exception to `log.debug` and raise a fixed
    "no browser to drive… `playwright install chromium`". On a headless host that is
    almost never true: chromium-1234 was installed and correct both times this fired,
    once with $DISPLAY pointing at a dead X server and once with a stale SingletonLock
    in the profile naming a container that had been recreated. It happens on `serve`'s
    daemon thread, where the exception is the only thing a human ever sees, so the
    reason has to travel in it.
    """
    from pathlib import Path

    could_not_start = browser._why_no_browser(
        [Exception("Target page, context or browser has been closed\nBrowser logs: …")],
        Path("/data/browser"),
    )
    assert "would not start" in could_not_start
    assert "Target page, context or browser has been closed" in could_not_start
    # It must not send the reader off to reinstall a browser that is already there.
    assert "playwright install" not in could_not_start
    # And it names where to look, which is the whole point of the change.
    assert "SingletonLock" in could_not_start and "/data/browser" in could_not_start
    # One line of the launcher's output, not the whole log dump.
    assert "Browser logs" not in could_not_start


def test_a_genuinely_missing_browser_still_says_how_to_install_one():
    """The other half: the old message was right about *this* case and stays."""
    from pathlib import Path

    missing = browser._why_no_browser(
        [Exception("Executable doesn't exist at /ms-playwright/chromium-1234/chrome")],
        Path("/data/browser"),
    )
    assert "playwright install" in missing
    assert "would not start" not in missing


def test_the_launch_failure_never_comes_back_empty_handed():
    """No attempts recorded is still a sentence, not a bare colon. Belt and braces: the
    loop always appends before raising, but a message that degrades into punctuation is
    how a reason-carrying error quietly becomes a reason-free one again."""
    from pathlib import Path

    assert browser._why_no_browser([], Path("/data/browser")).strip()
