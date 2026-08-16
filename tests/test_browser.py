"""The browser fill: what it must never do, and what it does to a real form.

Most of this file runs with no browser at all. The tests that need one are marked
`browser` and skip when the optional extra is absent, so a checkout without Playwright
still runs a clean suite — the same posture the LLM pass takes.

The browser tests drive a static HTML fixture over `file://`. Real Playwright, real DOM,
no network and no ATS. That is enough to exercise the two things that actually break:
label discovery across the four conventions forms use, and writing into each input type.
"""

import inspect

import pytest

from jobtracker import browser, store
from jobtracker.answers import load_answers
from jobtracker.models import Company

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
def test_there_is_no_submit_path_in_this_module():
    """An application is irreversible and goes out under the user's name.

    Asserted against the source rather than by behaviour, because the failure mode is
    someone adding a convenience `.click()` later and nothing noticing until a form is
    submitted. `page.fill`, `select_option` and `set_input_files` write into fields;
    none of them activates anything.
    """
    code = inspect.getsource(browser)
    # The mechanisms, not the word: prose in this module says "never submits" a lot.
    for mechanism in (".click(", ".press(", "requestSubmit", "form.submit",
                      "keyboard.press", "dispatchEvent"):
        assert mechanism not in code, f"{mechanism} could activate a form"


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

    def __init__(self, closes_after=3):
        self.waits = 0
        self.closes_after = closes_after
        self.open = True

    @property
    def pages(self):
        return [_FakePage(self)] if self.open else []


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
