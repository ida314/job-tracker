"""The posting page: purity, escaping, the freeze, and where a title link goes.

No sockets. `render_posting` is connection-in / string-out for the reason every other
`render_*` here is: escaping and read-purity are the security-relevant behaviours, and
testing them should not require standing up HTTP.
"""

import base64
import json
import re
from html.parser import HTMLParser

import pytest

from jobtracker import (config, dashboard, questions, resumes, server, store,
                        submissions)

PDF = b"%PDF-1.4 a real enough pdf"
DOCX = b"PK\x03\x04 a real enough docx"

# Third-party ATS strings are attacker-controllable in principle; the question and the
# answer are strings the user typed, which is the same problem from the other side.
EVIL_TITLE = '<script>alert("pwned")</script> New Grad Engineer'
EVIL_LOCATION = '<img src=x onerror=alert(1)>'
EVIL_URL = "javascript:alert(document.cookie)"
EVIL_QUESTION = 'Why us?" onmouseover="alert(1)'
EVIL_ANSWER = '</textarea><script>alert(2)</script>'


def _b64(blob):
    return base64.b64encode(blob).decode()


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Every directory this feature writes into, redirected under tmp_path."""
    for name, sub in (("RESUMES_DIR", "resumes"), ("SUBMISSIONS_DIR", "submissions"),
                      ("LETTERS_DIR", "letters"), ("TAILORED_DIR", "tailored")):
        monkeypatch.setattr(config, name, tmp_path / sub)
    return tmp_path


def _bank(tmp_path):
    (tmp_path / "resume.pdf").write_bytes(PDF)
    path = tmp_path / "answers.yaml"
    path.write_text(
        "identity:\n  first_name: Dylan\n  last_name: D\n  email: d@example.edu\n"
        "resume: ./resume.pdf\n"
    )
    return path


def _db(tmp_path, title="Backend Engineer, New Grad", location="New York, NY",
        url="https://boards.greenhouse.io/acme/jobs/1", company="Acme", job="1"):
    db = tmp_path / "s.db"
    conn = store.connect(db)
    conn.execute(
        "INSERT INTO postings (company, ats_job_id, title, location, url, first_seen, "
        "last_seen, description, posted_on) VALUES (?,?,?,?,?,?,?,?,?)",
        (company, job, title, location, url, "2026-09-08", "2026-09-12",
         "We run real distributed systems.", "2026-09-08"),
    )
    conn.execute(
        "INSERT INTO verdicts (company, ats_job_id, verdict, reason, decided_by, "
        "decided_at) VALUES (?,?,?,?,?,?)",
        (company, job, "match", "level:new grad", "rules", "2026-09-08"),
    )
    conn.commit()
    return db, conn


def _handler(db, answers_path=None):
    from tests.test_server import _handler_for

    return _handler_for(db, config.CRITERIA_YAML, answers_path)


def _body(page):
    """The rendered markup without the trailing script.

    Every control's class name also appears in `_JS`, so "is this button on the page"
    has to be asked of the markup — asking the whole document answers yes for every
    handler the script carries, on every page that emits it.
    """
    return page[:page.rindex("<script>")]


def _page(conn, company="Acme", job="1", answers=None):
    page, found = server.render_posting(
        conn, company, job, [], None, answers, "2026-09-12"
    )
    return page, found


# -- purity -------------------------------------------------------------------------
_WATCHED = ("posting_answers", "application_submissions", "applications",
            "posting_resumes", "posting_letters", "question_template")


def _snapshot(conn):
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in _WATCHED}


def test_the_posting_page_is_a_pure_read(paths):
    """Opening a view must not mutate data — and here that is not a formality.

    The page merges the question template into this posting's answers on the way out. A
    merge that wrote would turn *opening* a page into a claim that you had answered
    something, inside the one feature whose job is to record what you actually sent.
    """
    db, conn = _db(paths)
    store.set_template_question(conn, "auth", "Work authorization?", "US citizen",
                                "2026-09-12")
    conn.commit()
    before = _snapshot(conn)
    _page(conn)
    _page(conn)
    assert _snapshot(conn) == before


def test_a_template_question_renders_but_is_not_stored(paths):
    """The template seeds; it does not own. Until you save one it is a suggestion on a
    page, and `questions.answered` will not record it as submitted."""
    db, conn = _db(paths)
    store.set_template_question(conn, "auth", "Work authorization?", "US citizen",
                                "2026-09-12")
    conn.commit()
    page, _ = _page(conn)
    assert "Work authorization?" in page
    assert "from your template" in page
    assert store.posting_answers(conn, "Acme", "1") == []


# -- escaping ----------------------------------------------------------------------
class _AttrCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.attrs = []

    def handle_starttag(self, tag, attrs):
        for name, _value in attrs:
            self.attrs.append(name)


def test_a_hostile_title_and_location_are_escaped(paths):
    """Escaped text may still read as an attack when you grep for one — `onerror=alert`
    appears inside `&lt;img …&gt;` and is inert there. What matters is that no *tag* and no
    *handler* survives, which is what the parser checks."""
    db, conn = _db(paths, title=EVIL_TITLE, location=EVIL_LOCATION)
    page, _ = _page(conn)
    assert "<script>alert" not in page
    assert "<img src=x" not in page
    assert "&lt;script&gt;alert" in page
    parser = _AttrCollector()
    parser.feed(page)
    assert {a for a in parser.attrs if a.startswith("on")} == set()


def test_a_hostile_question_and_answer_cannot_break_out(paths):
    """Both land in markup the user's own text reaches: a `value="…"` attribute and a
    `<textarea>` body. Nothing on the page may carry an on* handler."""
    db, conn = _db(paths)
    store.set_posting_answer(conn, "Acme", "1", "q1", EVIL_QUESTION, EVIL_ANSWER,
                             "2026-09-12")
    conn.commit()
    page, _ = _page(conn)
    parser = _AttrCollector()
    parser.feed(page)
    assert {a for a in parser.attrs if a.startswith("on")} == set()
    assert "<script>alert(2)" not in page
    assert "&quot;" in page


def test_a_hostile_posting_url_is_neutralised(paths):
    db, conn = _db(paths, url=EVIL_URL)
    page, _ = _page(conn)
    assert "javascript:alert" not in page


# -- what the page must say ---------------------------------------------------------
def test_the_page_carries_the_nav_but_is_not_in_it(paths):
    """A detail page with no way back is the one place its absence would be felt — and a
    destination you reach *from* a posting is not a tab."""
    db, conn = _db(paths)
    page, found = _page(conn)
    assert found is True
    assert '<nav class=nav>' in page
    assert "/posting" not in server._NAV


def test_a_pair_that_exists_nowhere_is_a_404_page(paths):
    """A detail page for a pair that does not exist is not a page that happens to be
    empty, so the caller sends 404 and the page says so."""
    db, conn = _db(paths)
    page, found = _page(conn, company="Nobody", job="nope")
    assert found is False
    assert "Not found" in page


def test_a_manual_application_renders_its_posting_page(paths):
    """A manual entry has no `postings` row at all. It is also the one you most want a
    record of what you sent, so the page reads `applications` and never joins."""
    db, conn = _db(paths)
    jid = store.manual_job_id("Backend Engineer (Referral)")
    store.advance_application(conn, "Some Startup", jid, "Backend Engineer (Referral)",
                              "applied", "2026-09-12T09:00:00", url="", location="NYC",
                              source="manual")
    conn.commit()
    page, found = server.render_posting(
        conn, "Some Startup", jid, [], None, None, "2026-09-12"
    )
    assert found is True
    assert "Backend Engineer (Referral)" in page
    assert "added by hand" in page


# -- the freeze ---------------------------------------------------------------------
def _apply(h, company="Acme", job="1"):
    return h._api_disposition(
        {"company": company, "ats_job_id": job, "action": "applied"})


def test_applying_freezes_the_answers_you_saved(paths):
    db, conn = _db(paths)
    store.set_posting_answer(conn, "Acme", "1", "auth", "Work authorization?",
                             "US citizen", "2026-09-12")
    conn.commit()
    conn.close()

    assert _apply(_handler(db, _bank(paths)))["ok"] is True
    conn = store.connect(db)
    row = store.get_submission(conn, "Acme", "1")
    assert store.submitted_answers(row) == [
        {"qid": "auth", "question": "Work authorization?", "answer": "US citizen"}
    ]


def test_a_blank_answer_is_not_recorded_as_submitted(paths):
    """Recording a question with no reply reads afterwards as "I left this blank", which
    is a claim about the form rather than about you."""
    db, conn = _db(paths)
    store.set_posting_answer(conn, "Acme", "1", "why", "Why us?", "   ", "2026-09-12")
    conn.commit()
    conn.close()

    _apply(_handler(db, _bank(paths)))
    conn = store.connect(db)
    assert store.submitted_answers(store.get_submission(conn, "Acme", "1")) == []


def test_the_freeze_is_write_once(paths):
    """A submission is a fact about a moment. Pressing `+ tracker` again, or the CLI
    re-running, must find the row standing and leave it alone."""
    db, conn = _db(paths)
    conn.close()
    h = _handler(db, _bank(paths))
    _apply(h)
    conn = store.connect(db)
    first = store.get_submission(conn, "Acme", "1")["submitted_at"]
    store.set_posting_answer(conn, "Acme", "1", "q", "Late question?", "late",
                             "2026-09-12")
    conn.commit()
    conn.close()

    _apply(h)
    conn = store.connect(db)
    row = store.get_submission(conn, "Acme", "1")
    assert row["submitted_at"] == first
    assert store.submitted_answers(row) == []


def test_updating_what_you_submitted_is_the_one_way_to_replace_it(paths):
    """`+ tracker` on a table row records an application before the page was ever opened,
    so the snapshot it froze is of a page you had not filled in."""
    db, conn = _db(paths)
    conn.close()
    h = _handler(db, _bank(paths))
    _apply(h)
    conn = store.connect(db)
    store.set_posting_answer(conn, "Acme", "1", "q", "Why us?", "the systems work",
                             "2026-09-12")
    conn.commit()
    conn.close()

    assert h._api_submission({"company": "Acme", "ats_job_id": "1"})["ok"] is True
    conn = store.connect(db)
    assert store.submitted_answers(store.get_submission(conn, "Acme", "1")) == [
        {"qid": "q", "question": "Why us?", "answer": "the systems work"}
    ]


def test_updating_refuses_when_there_is_no_application(paths):
    """A submission with nothing to belong to is a record of something that did not
    happen. A refusal writes nothing."""
    db, conn = _db(paths)
    conn.close()
    h = _handler(db, _bank(paths))
    res = h._api_submission({"company": "Acme", "ats_job_id": "1"})
    assert res["ok"] is False
    conn = store.connect(db)
    assert store.get_submission(conn, "Acme", "1") is None


def test_the_archive_keeps_the_bytes_that_went_out(paths):
    """The whole reason the archive copies rather than naming. `tailor build` rewrites a
    posting's PDF at a deterministic path, so a stored name would go on resolving forever
    while coming to mean a document you never sent."""
    db, conn = _db(paths)
    name = resumes.stored_name("Acme", "1", ".pdf")
    resumes.write_atomic(resumes.path_for(name), b"%PDF-1.4 THE ONE I SENT")
    store.set_posting_resume(conn, "Acme", "1", name, 23, "2026-09-12")
    conn.commit()
    conn.close()

    _apply(_handler(db, _bank(paths)))
    conn = store.connect(db)
    row = store.get_submission(conn, "Acme", "1")
    archived = submissions.archived_path(row, "resume")
    assert archived.read_bytes() == b"%PDF-1.4 THE ONE I SENT"

    # The working document is rebuilt underneath it.
    resumes.write_atomic(resumes.path_for(name), b"%PDF-1.4 REBUILT LATER")
    assert archived.read_bytes() == b"%PDF-1.4 THE ONE I SENT"
    assert row["resume_kind"] == "override"


def test_the_tailored_pdf_is_not_the_resume_until_it_is_attached(paths):
    """Nothing anywhere accepts an edit on a click. A built tailored PDF is a diff to
    read, not this posting's resume — `tailor build --attach` is what promotes it."""
    from jobtracker import resume as resume_mod

    db, conn = _db(paths)
    out = resume_mod.tailored_path("Acme", "1")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"%PDF-1.4 tailored")
    conn.commit()

    bank_path = _bank(paths)
    from jobtracker.answers import load_answers

    path, kind = submissions.effective_resume(
        conn, load_answers(bank_path), "Acme", "1"
    )
    assert kind == "default"
    assert path.name == "resume.pdf"


# -- every control has a handler on the page that renders it -------------------------
def test_every_control_on_the_posting_page_has_a_handler_in_its_own_script(paths):
    """The regression this repo already shipped: a button rendered by one file with its
    handler in another file's script, so every click did nothing at all.

    Seeded so that *every* control renders — an uploaded resume and letter for the two
    clear buttons, a saved answer for `q-del`, an application for the stage controls and
    a submission for the update button. An equality that only ever sees half the set is
    not the guard it looks like.
    """
    db, conn = _db(paths)
    rname = resumes.stored_name("Acme", "1", ".pdf")
    resumes.write_atomic(resumes.path_for(rname), PDF)
    store.set_posting_resume(conn, "Acme", "1", rname, len(PDF), "2026-09-12")
    lname = resumes.letter_upload_name("Acme", "1", ".pdf")
    resumes.write_atomic(resumes.path_for(lname), PDF)
    store.set_posting_letter(conn, "Acme", "1", lname, len(PDF), "2026-09-12")
    store.set_posting_answer(conn, "Acme", "1", "q", "Why us?", "systems", "2026-09-12")
    store.advance_application(conn, "Acme", "1", "Backend Engineer, New Grad",
                              "applied", "2026-09-12T09:00:00")
    store.freeze_submission(conn, "Acme", "1", "2026-09-12T09:00:00",
                            resume="resume.pdf", resume_kind="override",
                            answers=json.dumps([{"qid": "q", "question": "Why us?",
                                                 "answer": "systems"}]))
    conn.commit()

    page, _ = _page(conn)
    script = page[page.rindex("<script>"):page.rindex("</script>")]
    body = _body(page)
    classes = set(re.findall(r'<button class="([a-z-]+)', body))
    classes |= set(re.findall(r'<button class=([a-z-]+)>', body))
    assert classes == {"q-save", "q-del", "q-add", "p-upload", "p-clear",
                       "p-resubmit", "app-save", "app-meta"}
    for cls in classes:
        assert f"button.{cls}" in script, cls
    for endpoint in ("/api/posting-answer", "/api/posting-resume", "/api/posting-letter",
                     "/api/submission", "/api/application", "/api/disposition"):
        assert endpoint in script, endpoint
    assert page.count("<script>") == 1


def test_the_i_applied_button_is_there_before_an_application_and_gone_after(paths):
    db, conn = _db(paths)
    body = _body(_page(conn)[0])
    assert "p-applied" in body and "p-resubmit" not in body

    store.advance_application(conn, "Acme", "1", "Backend Engineer, New Grad",
                              "applied", "2026-09-12T09:00:00")
    conn.commit()
    body = _body(_page(conn)[0])
    assert "p-applied" not in body and "p-resubmit" in body


def test_the_update_control_goes_away_once_an_employer_has_replied(paths):
    """Once someone has come back, what you sent is history — rewriting it is not a
    correction."""
    db, conn = _db(paths)
    store.advance_application(conn, "Acme", "1", "Backend Engineer, New Grad",
                              "interview", "2026-09-12T09:00:00")
    conn.commit()
    assert "p-resubmit" not in _body(_page(conn)[0])


# -- uploads ------------------------------------------------------------------------
def test_an_uploaded_letter_is_checked_by_content_not_only_by_name(paths):
    db, conn = _db(paths)
    conn.close()
    h = _handler(db, _bank(paths))
    res = h._api_posting_letter({"company": "Acme", "ats_job_id": "1",
                                 "filename": "letter.pdf",
                                 "content_b64": _b64(b"not a pdf at all")})
    assert res["ok"] is False
    assert "not really a .pdf" in res["error"]


def test_an_uploaded_letter_never_lands_in_the_letters_directory(paths):
    """`LETTERS_DIR` is rebuildable generated state, `letter_path` already owns the .pdf
    in it, and `Dockerfile.serve` does not mount it — so a hand-written letter there
    would be overwritten by the next build or deleted by the next `docker pull`."""
    db, conn = _db(paths)
    conn.close()
    h = _handler(db, _bank(paths))
    res = h._api_posting_letter({"company": "Acme", "ats_job_id": "1",
                                 "filename": "letter.pdf", "content_b64": _b64(PDF)})
    assert res["ok"] is True
    stored = resumes.path_for(res["filename"])
    assert stored.is_file()
    assert stored.parent == config.RESUMES_DIR
    assert not (config.LETTERS_DIR / res["filename"]).exists()


def test_an_uploaded_letter_cannot_collide_with_the_posting_resume(paths):
    """Both are minted from the same digest of the same pair; only the `_letter` infix
    keeps one from landing on the other."""
    assert (resumes.letter_upload_name("Acme", "1", ".pdf")
            != resumes.stored_name("Acme", "1", ".pdf"))


def test_an_uploaded_letter_beats_the_written_one(paths):
    db, conn = _db(paths)
    conn.close()
    h = _handler(db, _bank(paths))
    h._api_posting_letter({"company": "Acme", "ats_job_id": "1",
                           "filename": "mine.docx", "content_b64": _b64(DOCX)})
    conn = store.connect(db)
    path, kind = submissions.effective_letter(conn, None, "Acme", "1")
    assert kind == "override"
    assert path.suffix == ".docx"


def test_clearing_a_letter_removes_the_row_and_the_file(paths):
    db, conn = _db(paths)
    conn.close()
    h = _handler(db, _bank(paths))
    res = h._api_posting_letter({"company": "Acme", "ats_job_id": "1",
                                 "filename": "l.pdf", "content_b64": _b64(PDF)})
    path = resumes.path_for(res["filename"])
    assert h._api_posting_letter_clear({"company": "Acme", "ats_job_id": "1"})["ok"]
    conn = store.connect(db)
    assert store.get_posting_letter(conn, "Acme", "1") is None
    assert not path.exists()


# -- refusals write nothing ---------------------------------------------------------
@pytest.mark.parametrize("payload", [
    {},
    {"company": "Acme"},
    {"company": "Acme", "ats_job_id": "1", "question": ""},
    {"company": "Acme", "ats_job_id": "1", "question": "Q", "op": "nonsense"},
    {"company": "Nobody", "ats_job_id": "x", "question": "Q"},
])
def test_a_refused_posting_answer_writes_nothing(paths, payload):
    db, conn = _db(paths)
    conn.close()
    h = _handler(db, _bank(paths))
    assert h._api_posting_answer(payload)["ok"] is False
    conn = store.connect(db)
    assert store.posting_answers(conn, "Acme", "1") == []


def test_saving_a_question_stores_the_wording_the_page_showed(paths):
    """A row saved from a template seed is this posting's own from that moment. Editing
    the template later must not rewrite an answer you already gave."""
    db, conn = _db(paths)
    store.set_template_question(conn, "auth", "Work authorization?", "US citizen",
                                "2026-09-12")
    conn.commit()
    conn.close()
    h = _handler(db, _bank(paths))
    h._api_posting_answer({"company": "Acme", "ats_job_id": "1", "qid": "auth",
                           "question": "Work authorization?", "answer": "US citizen"})
    conn = store.connect(db)
    store.set_template_question(conn, "auth", "Are you authorized to work?", "Yes",
                                "2026-09-13")
    conn.commit()
    rows = store.posting_answers(conn, "Acme", "1")
    assert [(r["question"], r["answer"]) for r in rows] == [
        ("Work authorization?", "US citizen")
    ]


def test_deleting_a_template_question_keeps_answers_already_given(paths):
    """A question you stopped being asked is not a question you never answered."""
    db, conn = _db(paths)
    store.set_template_question(conn, "auth", "Work authorization?", "", "2026-09-12")
    store.set_posting_answer(conn, "Acme", "1", "auth", "Work authorization?",
                             "US citizen", "2026-09-12")
    conn.commit()
    conn.close()
    h = _handler(db, _bank(paths))
    assert h._api_question({"op": "delete", "qid": "auth"})["ok"] is True
    conn = store.connect(db)
    assert store.template_questions(conn) == []
    assert len(store.posting_answers(conn, "Acme", "1")) == 1


# -- qid minting --------------------------------------------------------------------
def test_a_qid_is_minted_once_and_survives_a_reworded_question(paths):
    """Minted from the text and never re-derived, which is what lets you fix a question's
    wording without orphaning every answer already given to it."""
    db, conn = _db(paths)
    conn.close()
    h = _handler(db, _bank(paths))
    first = h._api_posting_answer({"company": "Acme", "ats_job_id": "1",
                                   "question": "Why us?", "answer": "a"})["qid"]
    second = h._api_posting_answer({"company": "Acme", "ats_job_id": "1",
                                    "qid": first, "question": "Why this company?",
                                    "answer": "a"})["qid"]
    assert first == second
    conn = store.connect(db)
    rows = store.posting_answers(conn, "Acme", "1")
    assert len(rows) == 1
    assert rows[0]["question"] == "Why this company?"


def test_two_questions_that_slug_alike_do_not_collapse_onto_one_row(paths):
    db, conn = _db(paths)
    conn.close()
    h = _handler(db, _bank(paths))
    a = h._api_posting_answer({"company": "Acme", "ats_job_id": "1",
                               "question": "Why us?", "answer": "one"})["qid"]
    b = h._api_posting_answer({"company": "Acme", "ats_job_id": "1",
                               "question": "Why us!", "answer": "two"})["qid"]
    assert a != b
    conn = store.connect(db)
    assert len(store.posting_answers(conn, "Acme", "1")) == 2


def test_a_question_of_pure_punctuation_still_gets_its_own_key():
    """A slug of nothing would collapse every such question onto one row."""
    assert questions.mint_qid("???") != questions.mint_qid("!!!")


# -- where a posting title points ----------------------------------------------------
def _dash(conn, interactive):
    from tests.test_dashboard import _company

    return dashboard.build_dashboard(conn, [_company("Acme", 1)], "2026-09-12",
                                     interactive=interactive)


def _seed_for_dashboard(conn, company="Acme", job="1"):
    from jobtracker.tasks.judge import RankJudgment

    store.record_judgment(conn, company, job,
                          RankJudgment("strong", "strong", "low", "why"),
                          "h", "2026-09-09")
    store.set_score(conn, company, job, 90.0, "2026-09-09")
    conn.commit()


def test_a_posting_title_is_internal_under_serve_and_external_in_the_static_file(paths):
    """The static dashboard is an artifact you mail and open offline — `/posting` there
    is a link to nothing, which is the same defect as a button with no handler."""
    db, conn = _db(paths)
    _seed_for_dashboard(conn)

    # `href="` and not a bare substring: `/api/posting-resume/clear` lives in the script
    # on both pages and contains "/posting" too.
    served = _dash(conn, interactive=True)
    assert 'href="/posting?company=Acme' in served

    static = _dash(conn, interactive=False)
    assert 'href="/posting' not in static
    assert "https://boards.greenhouse.io/acme/jobs/1" in static


def test_the_internal_link_does_not_open_a_new_tab(paths):
    """Same-origin navigation. `target=_blank` belongs to the employer's link, and
    carrying it here would put the posting page in a second tab every time."""
    db, conn = _db(paths)
    _seed_for_dashboard(conn)
    served = _dash(conn, interactive=True)
    for match in re.findall(r'<a [^>]*href="/posting[^"]*"[^>]*>', served):
        assert "target=" not in match, match


def test_a_hostile_company_name_cannot_escape_the_posting_href(paths):
    """The specific job `dashboard._q` exists for: a company named `A&B "C"` written into
    an href with an f-string would close the attribute it sits in."""
    row = {"company": 'A&B "C"', "ats_job_id": "1", "url": "https://x/1"}
    href = dashboard._posting_href(row, interactive=True)
    assert '"' not in href
    assert "&" not in href.replace("&amp;", "")


def test_the_static_applications_tab_still_links_to_the_employer(paths):
    """`dashboard._application` renders into the offline file, which has no server."""
    db, conn = _db(paths)
    store.advance_application(conn, "Acme", "1", "Backend Engineer, New Grad",
                              "applied", "2026-09-12T09:00:00",
                              url="https://boards.greenhouse.io/acme/jobs/1")
    conn.commit()
    static = _dash(conn, interactive=False)
    assert 'href="/posting' not in static
    assert "https://boards.greenhouse.io/acme/jobs/1" in static


def test_the_prepare_link_carries_no_data_act(paths):
    """`.pick [data-act]` has to keep meaning exactly the three disposition buttons."""
    db, conn = _db(paths)
    _seed_for_dashboard(conn)
    served = _dash(conn, interactive=True)
    for match in re.findall(r'<a [^>]*class="apply"[^>]*>', served):
        assert "data-act" not in match, match
    assert "Prepare" in served


# -- purge ----------------------------------------------------------------------------
def test_purge_keeps_what_you_typed_and_what_you_submitted(paths):
    """`purge` removes what a run produced and could produce again. Prose you typed is
    the one thing here nothing can write a second time, and a submission whose
    application is kept must not lose the evidence behind it."""
    db, conn = _db(paths)
    store.set_posting_answer(conn, "Acme", "1", "q", "Why us?", "systems", "2026-09-12")
    store.advance_application(conn, "Acme", "1", "Backend Engineer, New Grad",
                              "applied", "2026-09-12T09:00:00")
    store.freeze_submission(conn, "Acme", "1", "2026-09-12T09:00:00",
                            answers=json.dumps([{"qid": "q", "question": "Why us?",
                                                 "answer": "systems"}]))
    conn.commit()

    store.purge_company(conn, "Acme")
    conn.commit()
    assert len(store.posting_answers(conn, "Acme", "1")) == 1
    assert store.get_submission(conn, "Acme", "1") is not None
    assert store.get_application(conn, "Acme", "1") is not None


def test_an_uploaded_letter_blocks_a_purge_the_way_an_attached_resume_does(paths):
    db, conn = _db(paths)
    store.set_posting_letter(conn, "Acme", "2", "acme_2_x_letter.pdf", 10, "2026-09-12")
    conn.commit()
    blockers = store.purge_blockers(conn, "Acme")
    assert [b["status"] for b in blockers] == ["letter attached"]


def test_a_purge_still_takes_the_uploaded_letter_row(paths):
    """It mirrors `posting_resumes`, which a purge does remove — the blocker above is
    what puts that in front of you first."""
    db, conn = _db(paths)
    store.set_posting_letter(conn, "Acme", "1", "x_letter.pdf", 10, "2026-09-12")
    conn.commit()
    store.purge_company(conn, "Acme")
    conn.commit()
    assert store.get_posting_letter(conn, "Acme", "1") is None


# -- the merge ------------------------------------------------------------------------
def test_a_saved_answer_wins_over_the_template_row_with_the_same_key():
    stored = [{"qid": "auth", "question": "Work authorization?", "answer": "US citizen",
               "ordinal": 0}]
    template = [{"qid": "auth", "question": "Authorized?", "answer": "Yes",
                 "ordinal": 0}]
    merged = questions.merge(stored, template)
    assert len(merged) == 1
    assert merged[0]["answer"] == "US citizen"
    assert merged[0]["saved"] is True


def test_the_merge_orders_on_ordinal_and_key_across_both_sources():
    """"Stored first, then template" would put two rows at ordinal 3 and swap them under
    the cursor after a save."""
    stored = [{"qid": "b", "question": "B", "answer": "x", "ordinal": 1}]
    template = [{"qid": "a", "question": "A", "answer": "", "ordinal": 0},
                {"qid": "c", "question": "C", "answer": "", "ordinal": 2}]
    assert [r["qid"] for r in questions.merge(stored, template)] == ["a", "b", "c"]


def test_a_newly_saved_answer_goes_last_and_moves_nothing(paths):
    db, conn = _db(paths)
    store.set_posting_answer(conn, "Acme", "1", "a", "A", "x", "2026-09-12")
    store.set_posting_answer(conn, "Acme", "1", "b", "B", "y", "2026-09-12")
    conn.commit()
    rows = store.posting_answers(conn, "Acme", "1")
    assert [(r["qid"], r["ordinal"]) for r in rows] == [("a", 0), ("b", 1)]


def test_an_edit_keeps_the_place_the_row_already_had(paths):
    db, conn = _db(paths)
    store.set_posting_answer(conn, "Acme", "1", "a", "A", "x", "2026-09-12")
    store.set_posting_answer(conn, "Acme", "1", "b", "B", "y", "2026-09-12")
    store.set_posting_answer(conn, "Acme", "1", "a", "A again", "z", "2026-09-13")
    conn.commit()
    rows = store.posting_answers(conn, "Acme", "1")
    assert [(r["qid"], r["ordinal"]) for r in rows] == [("a", 0), ("b", 1)]


def test_a_broken_answers_blob_reads_as_empty_rather_than_raising():
    """`flags_of`'s rule: a blob that will not parse can only have come from a database
    somebody edited by hand, and that is not worth raising out of a render."""
    assert store.submitted_answers({"answers": "{not json"}) == []
    assert store.submitted_answers(None) == []


# -- the document route ---------------------------------------------------------------
def test_the_document_route_refuses_a_kind_it_does_not_know(paths):
    db, conn = _db(paths)
    conn.close()
    h = _handler(db, _bank(paths))
    h.path = "/api/document?company=Acme&job=1&kind=../../etc/passwd"
    sent = {}
    h._send_json = lambda payload, status=200: sent.update(payload=payload, status=status)
    h._send_bytes = lambda *a, **k: sent.update(bytes=True)
    h._send_document()
    assert sent["payload"]["ok"] is False
    assert sent["status"] == 400


def test_the_document_route_hands_back_the_archived_bytes(paths):
    db, conn = _db(paths)
    name = resumes.stored_name("Acme", "1", ".pdf")
    resumes.write_atomic(resumes.path_for(name), b"%PDF-1.4 SENT")
    store.set_posting_resume(conn, "Acme", "1", name, 13, "2026-09-12")
    conn.commit()
    conn.close()
    h = _handler(db, _bank(paths))
    _apply(h)

    h.path = "/api/document?company=Acme&job=1&kind=submitted-resume"
    sent = {}
    h._send_json = lambda payload, status=200: sent.update(payload=payload, status=status)
    h._send_bytes = lambda blob, ctype, **k: sent.update(blob=blob, ctype=ctype)
    h._send_document()
    assert sent["blob"] == b"%PDF-1.4 SENT"
    assert sent["ctype"] == "application/pdf"
