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

from jobtracker import browser, live, store
from jobtracker.answers import load_answers
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


# -- the mirrored form ------------------------------------------------------------------
# `serve` fills the form and then hands you the fields rather than the window. The
# window still exists — it is where you read the application over and send it — but the
# typing happens on a page with no video stream between you and the keyboard.


class _Recorder:
    """A page that records what was asked of it. Enough to drive `_drain` with no browser."""

    def __init__(self, found=None):
        self.found = found or []
        self.filled = {}
        self.selected = {}
        self.checked = {}
        self.files = {}
        self.shots = 0
        self.evaluated = []

    def evaluate(self, script, arg=None):
        self.evaluated.append(arg)
        return self.found

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
