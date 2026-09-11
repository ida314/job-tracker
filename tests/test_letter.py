"""The `coverletter` role: what the template declares, and what cannot get through it.

The second bounded role that composes prose, and the first that composes a whole
document, so the tests that matter are again about the refusals — but they are different
refusals from `tailor`'s, and the difference is the point:

  * **The model never writes LaTeX.** Not "writes it under an allowlist". Every reserved
    character is escaped, the backslash first, so there is no control sequence left to
    permit or refuse. `test_a_composed_paragraph_cannot_carry_a_control_sequence` is the
    one that would see a regression here.
  * **The facts are not the model's to state.** Employer, title and date are substituted
    from the record. A letter addressed to the wrong company is the one mistake a cover
    letter does not survive.
  * **A letter is all-or-nothing.** `fill` leaves an unanswered slot as its placeholder
    prose, so a partial letter compiles to a PDF with SHOUTING CAPS in the middle. The
    row is not written unless every slot came back grounded.
  * **A slot is what the template already looks like** — a `%` comment and prose with a
    placeholder in it. Prose with nothing to fill in is prose you wrote, and is passed
    through untouched.

No network, no router, no LaTeX toolchain. Stubs are hand-written, per house style.
"""

import json

import pytest

from jobtracker import letter as letter_mod
from jobtracker import store
from jobtracker.keywords import Keywords
from jobtracker.tasks import coverletter

TEMPLATE = r"""\documentclass[letterpaper,11pt]{article}
\usepackage{hyperref}

\newcommand{\YourName}{Dylan Dodds}
\newcommand{\RecipientOrg}{COMPANY}
\newcommand{\LetterDate}{DATE}
\newcommand{\LetterSubject}{Re: JOB TITLE}
\newcommand{\LetterGreeting}{Dear COMPANY team,}

\begin{document}

% ----- Header -----
\begin{center}
    \textbf{\Huge \YourName}
\end{center}

\RecipientOrg \\
\LetterDate

\LetterGreeting

% Paragraph 1:
% State the role and why it fits.
I am applying for the JOB TITLE role at COMPANY. My work has focused on RELEVANT AREA.

% Paragraph 2:
% Use the strongest directly relevant experience.
In my work at MOST RELEVANT EXPERIENCE, I MOST RELEVANT ACCOMPLISHMENT.

% Closing:
% Keep this short.
Thank you for your consideration.

\end{document}
"""

RESUME = r"""\documentclass{article}
\begin{document}
\item Built a REST API in Flask for an internal analytics tool
\item Wrote batch jobs that moved data between two Postgres databases
\end{document}
"""

DESCRIPTION = (
    "You will design and operate high-throughput HTTP services on a distributed "
    "platform. We use Python and Postgres, and care about observability."
)


@pytest.fixture(scope="module")
def template():
    return letter_mod.parse(TEMPLATE)


def _answer(**over):
    paragraphs = [
        {"key": "p1", "text": "I am applying for the role, which asks for services "
                              "at scale.",
         "evidence": "high-throughput HTTP services"},
        {"key": "p2", "text": "I built a REST API in Flask and moved data between "
                              "Postgres databases.",
         "evidence": "Built a REST API in Flask for an internal analytics tool"},
    ]
    for item in paragraphs:
        item.update(over.pop(item["key"], {}))
    if "paragraphs" in over:
        paragraphs = over["paragraphs"]
    return json.dumps({"paragraphs": paragraphs})


# -- what the template declares --------------------------------------------------------
def test_a_slot_is_a_comment_plus_prose_carrying_a_placeholder(template):
    """The two paragraphs with CAPS in them, and nothing else in the document."""
    assert template.keys == ["p1", "p2"]
    assert "RELEVANT AREA" in template.slots[0].body
    assert "State the role" in template.slots[0].brief


def test_prose_with_nothing_to_fill_in_is_not_a_slot(template):
    """The closing carries a comment and no placeholder, so it is already written.

    The rule is mechanical and this is what it buys: fixed prose in a template stays
    fixed. A rule that took every commented paragraph would hand "Thank you for your
    consideration" to a model to improve, nightly, forever.
    """
    assert all("consideration" not in s.body for s in template.slots)
    assert "Thank you for your consideration." in letter_mod.fill(
        template, {"p1": "a", "p2": "b"}, "Acme", "Engineer", "2026-09-11"
    )


def test_the_preamble_is_out_of_every_slots_reach(template):
    """Slots are found only inside \\begin{document}.

    The same by-construction safety `apply_edits` gets from refusing to insert: the
    packages, the page geometry and your own name are not addressable, so no answer from
    a model can reach them however it is shaped.
    """
    begin = TEMPLATE.index("\\begin{document}")
    assert all(slot.start > begin for slot in template.slots)


def test_a_comment_mentioning_begin_document_does_not_move_the_body():
    """The regression the tracked example template found on its first parse.

    A template worth reading explains itself, and the natural way to explain this one is
    to write "everything outside \\begin{document} is out of reach" in a comment at the
    top. Under a plain `text.find` that mention became the document start, dragging the
    whole preamble into slot range — so the sentence describing the safety property was
    the thing that removed it, and `\\newcommand{\\YourName}{YOUR NAME}` became a slot the
    model was asked to rewrite.
    """
    doc = (
        "% everything outside \\begin{document} is out of reach\n"
        "\\documentclass{article}\n"
        "\\newcommand{\\YourName}{YOUR NAME}\n"
        "\\begin{document}\n"
        "% Paragraph 1:\n"
        "% Say something.\n"
        "I did RELEVANT THING.\n"
        "\\end{document}\n"
    )
    parsed = letter_mod.parse(doc)
    assert parsed.keys == ["p1"]
    assert "YOUR NAME" not in parsed.slots[0].body


def test_the_tracked_example_template_is_a_working_one():
    """`coverletter.example.tex` is the documentation of this format, so it has to parse
    as the format — four slots, and the closing left fixed."""
    from pathlib import Path

    parsed, error = letter_mod.load(Path(__file__).resolve().parents[1]
                                    / "coverletter.example.tex")
    assert parsed is not None, error
    assert parsed.keys == ["p1", "p2", "p3", "p4"]
    assert all("YOUR NAME" not in s.body for s in parsed.slots)
    out = letter_mod.fill(parsed, {k: "written" for k in parsed.keys},
                          "Acme", "Engineer", "2026-09-11")
    assert "Thank you for your consideration." in out


def test_a_structural_block_is_never_a_slot(template):
    """`\\begin{center}` is commented and is layout, not prose."""
    assert all("\\begin" not in s.body for s in template.slots)


def test_a_template_with_no_slots_is_refused_by_name(tmp_path):
    """An absence reported as what it is, so you know what to go and write."""
    path = tmp_path / "letter.tex"
    path.write_text("\\documentclass{article}\n\\begin{document}\nHi.\n\\end{document}\n")
    parsed, error = letter_mod.load(path)
    assert parsed is None
    assert "no paragraphs to fill" in error


def test_a_missing_template_and_a_broken_one_read_differently(tmp_path):
    """Two absences, two things to do — the `unavailable_reason` distinction.

    Folding them together would report a template that will not parse as one that is not
    there, and send you to write a document you already have.
    """
    _absent, missing = letter_mod.load(tmp_path / "nope.tex")
    assert "no cover-letter template" in missing

    broken = tmp_path / "letter.tex"
    broken.write_text("just some prose, not a document at all\n")
    _none, error = letter_mod.load(broken)
    assert "\\documentclass" in error


# -- the model never writes LaTeX ------------------------------------------------------
def test_a_composed_paragraph_cannot_carry_a_control_sequence(template):
    r"""The whole security argument in one assertion.

    `tailor` needs an allowlist because it must let `\resumeItem{...}` through. Nothing
    here does, so the backslash itself is escaped and there is no command left to reason
    about — no list to keep current, and no `\csname` to compose one out of characters.
    """
    out = letter_mod.fill(
        template,
        {"p1": r"I use \input{/etc/passwd} daily", "p2": "ok"},
        "Acme", "Engineer", "2026-09-11",
    )
    body = out[out.index("\\begin{document}"):]
    assert "\\input{/etc/passwd}" not in body
    assert "\\textbackslash{}input" in body


def test_every_reserved_character_survives_as_itself():
    """Escaped, not stripped. A model writing "100%" means 100 percent."""
    out = letter_mod.escape_text("100% of $5 & #1 _x^2 ~ {a}")
    for raw in ("\\%", "\\$", "\\&", "\\#", "\\_", "\\{", "\\}"):
        assert raw in out
    assert "\\textasciicircum{}" in out and "\\textasciitilde{}" in out


# -- the facts are not the model's to state --------------------------------------------
def test_the_employer_and_title_come_from_the_record(template):
    """Substituted by `fill`, never asked of the model.

    The database already knows who the letter is addressed to, and a letter addressed to
    the wrong company is the one mistake a cover letter does not survive.
    """
    out = letter_mod.fill(
        template, {"p1": "a", "p2": "b"}, "Acme Corp", "Backend Engineer", "2026-09-11"
    )
    assert "\\newcommand{\\RecipientOrg}{Acme Corp}" in out
    assert "Re: Backend Engineer}" in out
    assert "Dear Acme Corp team,}" in out
    assert "September 11, 2026" in out
    assert "COMPANY" not in out.replace("% ", "")


def test_a_company_name_is_escaped_where_it_lands(template):
    """It is third-party text going into a document that gets compiled."""
    out = letter_mod.fill(
        template, {"p1": "a", "p2": "b"}, "Smith & Co", "Engineer", "2026-09-11"
    )
    assert "Smith \\& Co" in out


def test_a_model_paragraph_cannot_steer_a_later_substitution(template):
    """Paragraphs are spliced by offset FIRST, then the facts are substituted.

    Reversed, a paragraph containing the word COMPANY would be rewritten by the fact
    substitution that follows it — the model's output steering a step it is not part of.
    Splicing first means the only text the substitution ever walks is the template's own.
    """
    out = letter_mod.fill(
        template, {"p1": "We discussed COMPANY at length", "p2": "b"},
        "Acme", "Engineer", "2026-09-11",
    )
    assert "We discussed COMPANY at length" in out


def test_an_unreadable_date_is_left_alone_not_replaced_with_today(template):
    """`normalize_posted_at`'s rule: a date this cannot read is shown, never invented."""
    assert letter_mod.format_date("not a date") == "not a date"
    assert letter_mod.format_date("") == ""


# -- grounding, and all-or-nothing -----------------------------------------------------
def test_a_complete_grounded_letter_is_kept(template):
    out = coverletter.parse_letter(_answer(), template, DESCRIPTION, RESUME)
    assert [p.key for p in out.paragraphs] == ["p1", "p2"]
    # Which document each quote was found in, which is the one thing that says whether a
    # paragraph is answering the posting or describing the candidate.
    assert [p.source for p in out.paragraphs] == ["description", "resume"]


def test_evidence_may_be_grounded_in_the_resume_or_the_description(template):
    """Two documents, because a paragraph about your own experience is grounded in one
    and a paragraph about the role in the other."""
    out = coverletter.parse_letter(_answer(), template, DESCRIPTION, RESUME)
    assert {p.source for p in out.paragraphs} == {"description", "resume"}


def test_an_ungrounded_paragraph_discards_the_whole_letter(template):
    """A quote in neither document is a requirement nobody wrote down."""
    answer = _answer(p1={"evidence": "we require ten years of Kubernetes"})
    assert coverletter.parse_letter(answer, template, DESCRIPTION, RESUME) is None


def test_a_missing_slot_discards_the_whole_letter(template):
    """The reason `parse_letter` is all-or-nothing where `parse_edits` is not.

    `fill` leaves an unanswered slot as its placeholder prose, so writing this row down
    would put a download button on the page over a PDF with `RELEVANT AREA` printed in
    the middle of it — and the posting would leave the queue, so it would never be asked
    again either.
    """
    answer = json.dumps({"paragraphs": json.loads(_answer())["paragraphs"][:1]})
    assert coverletter.parse_letter(answer, template, DESCRIPTION, RESUME) is None


def test_an_unfilled_slot_keeps_its_placeholder_which_is_why(template):
    """The behaviour the test above exists to keep off the page.

    Deliberate, not a gap: a letter with visible SHOUTING CAPS is obviously unfinished,
    where a silently dropped paragraph reads as a letter somebody meant to be short.
    """
    out = letter_mod.fill(template, {"p1": "written"}, "Acme", "Eng", "2026-09-11")
    assert "MOST RELEVANT ACCOMPLISHMENT" in out


def test_a_denied_term_discards_the_whole_letter(template):
    """`denied` is a ruling you made, applied in Python where a prompt cannot argue.

    It discards the letter rather than one paragraph — unlike `tailor`, where the other
    edits still compile. Here there is no rest: one bad paragraph is a letter that cannot
    be built, so re-asking is the only way to get one that can.
    """
    answer = _answer(p1={"text": "I have deep Kubernetes experience.",
                         "evidence": "high-throughput HTTP services"})
    kw = Keywords(denied=["Kubernetes"])
    assert coverletter.parse_letter(answer, template, DESCRIPTION, RESUME, kw) is None


def test_an_overlong_paragraph_discards_the_letter(template):
    """Four paragraphs is a page, which is the length a cover letter has to be."""
    answer = _answer(p1={"text": "x " * coverletter.MAX_PARAGRAPH_CHARS})
    assert coverletter.parse_letter(answer, template, DESCRIPTION, RESUME) is None


def test_a_key_the_template_did_not_declare_is_ignored(template):
    """An invented slot cannot address anything, so it is dropped rather than fatal —
    and the letter still has to be complete, which is what the assertion checks."""
    payload = json.loads(_answer())
    payload["paragraphs"].append(
        {"key": "p9", "text": "extra", "evidence": "Python and Postgres"}
    )
    out = coverletter.parse_letter(json.dumps(payload), template, DESCRIPTION, RESUME)
    assert [p.key for p in out.paragraphs] == ["p1", "p2"]


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"paragraphs": {}}'])
def test_every_failure_path_writes_nothing(template, text):
    """The failure contract inherited from `llm/client.py`: no answer, never a wrong one."""
    assert coverletter.parse_letter(text, template, DESCRIPTION, RESUME) is None


# -- the question, and the file ---------------------------------------------------------
def test_the_template_is_half_the_unit_key(template):
    """Edit a brief and every posting is a new unit with a clean retry count.

    The call `tailor` makes about the resume, made here about two documents: a letter
    written under the old brief is an answer to something you no longer asked.
    """
    from jobtracker.tasks.base import TaskContext

    base = TaskContext(today="2026-09-11", letter_template=template, resume_hash="r1")
    moved = TaskContext(
        today="2026-09-11",
        letter_template=letter_mod.parse(TEMPLATE.replace("State the role", "Say why")),
        resume_hash="r1",
    )
    assert coverletter._unit_key(base) != coverletter._unit_key(moved)


def test_a_letter_and_a_resume_never_collide_on_disk():
    """Two documents for one posting, and the stems have to differ.

    They are minted from the same `resumes.stored_name` pair, so without the suffix a
    build of one would overwrite the other and the download buttons would hand over the
    same file.
    """
    from jobtracker import resume as resume_mod

    assert (letter_mod.letter_stem("Acme", "j1")
            != resume_mod.tailored_stem("Acme", "j1"))


def test_the_stem_cannot_carry_a_separator_out_of_a_company_name():
    """What lets the download route take both strings straight off a query string."""
    stem = letter_mod.letter_stem("../../etc", "a/b/c")
    assert "/" not in stem and ".." not in stem


# -- the queue --------------------------------------------------------------------------
def test_a_letter_leaves_the_queue_once_written(template):
    """`run_task` recomputes `remaining` by re-reading `pending_count` rather than
    subtracting, so a task whose queue does not shrink after `apply` reports a backlog
    forever."""
    conn = store.connect(":memory:")
    conn.execute(
        "INSERT INTO postings (company, ats_job_id, title, url, first_seen, last_seen,"
        " description) VALUES ('Acme', 'j1', 'Backend Engineer', 'u', '2026-09-11',"
        " '2026-09-11', ?)",
        (DESCRIPTION,),
    )
    conn.execute(
        "INSERT INTO verdicts (company, ats_job_id, verdict, reason, decided_by,"
        " decided_at) VALUES ('Acme', 'j1', 'match', '', 'rules', '2026-09-11')"
    )
    conn.execute(
        "INSERT INTO rankings (company, ats_job_id, backend_fit, growth, entry_risk,"
        " why, prose_hash, judged_at, score)"
        " VALUES ('Acme', 'j1', 'high', 'high', 'low', '', 'h1', '2026-09-11', 8.0)"
    )
    conn.commit()

    assert len(store.matches_needing_a_letter(conn, "key-1")) == 1
    store.record_letter(conn, "Acme", "j1", "[]", "key-1", "2026-09-11")
    conn.commit()
    assert store.matches_needing_a_letter(conn, "key-1") == []
    # A moved template is a different question, so it comes back.
    assert len(store.matches_needing_a_letter(conn, "key-2")) == 1
    conn.close()
