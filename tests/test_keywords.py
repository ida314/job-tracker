"""The keyword lists: what `tailor` may write onto a resume, and what it may not.

The feature exists because `tailor`'s grounding rules say nothing at all about the
technology *names* inside a suggestion. A description that says "we use Kafka" is exactly
the input that talks a model into writing Kafka onto a resume, and the person defending
that in an interview is the candidate.

So the tests that matter are the ones about the asymmetry between the two lists:

  * `allowed` steers — it is prose the model reads, and an empty one means UNRESTRICTED.
  * `denied` refuses — it is applied in Python, at every posting, whatever the prompt says.
  * An undecided term is a *question*: the edit is held, not dropped, so answering the
    question is what releases work that already exists.

No network, no model, no LaTeX toolchain.
"""

import json

import pytest
import yaml

from jobtracker import keywords as kw
from jobtracker.resume import get_format
from jobtracker.tasks import tailor

RESUME = r"""\documentclass{article}
\begin{document}
\section*{Experience}
\begin{itemize}
  \item Wrote batch jobs that moved data between two Postgres databases
\end{itemize}
\end{document}
"""

DESCRIPTION = (
    "You will run event pipelines. Experience with Kafka is required, and we use "
    "Postgres and Kubernetes throughout."
)

LINE = r"  \item Wrote batch jobs that moved data between two Postgres databases"


@pytest.fixture(scope="module")
def latex():
    return get_format("latex")


def _answer(edits=None, flagged=None):
    return json.dumps({
        "edits": edits if edits is not None else [{
            "section": "experience",
            "current_line": LINE,
            "suggestion": r"  \item Built Kafka pipelines over two Postgres databases",
            "evidence": "Experience with Kafka is required",
        }],
        "flagged": flagged if flagged is not None else [{
            "term": "Kafka",
            "evidence": "Experience with Kafka is required",
            "why": "The team runs event pipelines.",
        }],
    })


# -- what an empty list means --------------------------------------------------------
def test_an_empty_allowed_list_means_unrestricted_not_nothing():
    """The one semantic somebody can get wrong by looking at it.

    Reading an absence as a decision is the mistake this repo keeps naming — `manual`
    asserting a company has no JSON board when nobody had looked. Here it would mean that
    installing the feature silently switched tailoring off.
    """
    assert kw.Keywords().restricted is False
    assert kw.Keywords(allowed=("Postgres",)).restricted is True


def test_a_missing_file_is_empty_lists_and_a_malformed_one_is_an_error(tmp_path):
    """Missing is normal — you grow this file by clicking. Malformed is not.

    `load_settings`' rule: reading a typo as "no keywords" would silently unrestrict the
    thing the file exists to restrict.
    """
    assert kw.load_keywords(tmp_path / "nope.yaml") == kw.Keywords()

    bad = tmp_path / "keywords.yaml"
    bad.write_text("allowed:\n  - [not, a, term]\n")
    with pytest.raises(ValueError):
        kw.load_keywords(bad)

    bad.write_text("allowed:\n  - Redis\ndenied:\n  - redis\n")
    with pytest.raises(ValueError, match="both allowed and denied"):
        kw.load_keywords(bad)


def test_the_prompt_block_is_empty_when_there_is_no_list():
    """A prompt saying "the candidate knows: (none)" is a claim about the candidate."""
    assert kw.Keywords().prompt_block() == ""
    assert kw.Keywords(allowed=("Go", "Redis")).prompt_block() == "- Go\n- Redis"


# -- term matching -------------------------------------------------------------------
@pytest.mark.parametrize("term,text,expected", [
    ("C++", "I know C++ well", True),
    ("C++", "a C+++ typo", False),
    ("Node.js", "parsing Node.json files", False),
    ("Node.js", "built Node.js services", True),
    ("Go", "Going nowhere fast", False),
    ("Go", "wrote Go daemons", True),
    ("Redis", "a redis cache", True),
    ("Kafka", "a Kafkaesque process", False),
])
def test_a_term_matches_as_a_term_and_not_inside_a_longer_word(term, text, expected):
    r"""`\b` is wrong for exactly the terms this is for.

    A word boundary after the `+` in "C++" sits between two non-word characters and never
    matches, and "Node.js" would match inside "Node.json". So the boundary is stated as
    "not adjacent to a character a technology name can contain".
    """
    assert kw.occurs(term, text) is expected


def test_the_hash_ignores_order_and_case_but_not_content():
    """Retyping the file must not re-ask every posting; changing it must.

    `profile.prose_hash`'s rule about reordering the prose blocks, applied to two lists.
    """
    a = kw.Keywords(allowed=("Redis", "Postgres"), denied=("Kubernetes",))
    b = kw.Keywords(allowed=("postgres", "redis"), denied=("kubernetes",))
    assert a.hash == b.hash
    assert kw.Keywords(allowed=("Redis",)).hash != a.hash
    # And the two lists are not interchangeable — a term moving from one to the other is
    # a different question, not the same one written differently.
    assert (kw.Keywords(allowed=("Redis",)).hash
            != kw.Keywords(denied=("Redis",)).hash)


# -- the file writer -----------------------------------------------------------------
def test_editing_the_file_keeps_every_comment_in_it():
    """Text surgery, not a YAML round trip — `answers.insert_answer`'s rule.

    This file is mostly the comments explaining what the two lists mean, including the
    empty-means-unrestricted semantic, which a person reading it at 2am has no other way
    to learn. `yaml.safe_dump` deletes all of them.
    """
    original = kw.render(kw.Keywords())
    comments = [ln for ln in original.splitlines() if ln.startswith("#")]
    assert comments

    out = kw.edit(original, "allowed", "PostgreSQL")
    out = kw.edit(out, "allowed", "Redis")
    out = kw.edit(out, "denied", "Kubernetes")
    assert [ln for ln in out.splitlines() if ln.startswith("#")] == comments
    assert yaml.safe_load(out) == {"allowed": ["PostgreSQL", "Redis"],
                                   "denied": ["Kubernetes"]}


def test_forget_removes_a_term_from_whichever_list_holds_it():
    """One action, because "I did not mean that" should not be a question about where it
    landed."""
    text = kw.edit(kw.edit(kw.render(kw.Keywords()), "allowed", "Redis"),
                   "denied", "Kubernetes")
    gone = kw.edit(text, "allowed", "Kubernetes", remove=True)
    assert yaml.safe_load(gone)["denied"] in (None, [])
    assert yaml.safe_load(gone)["allowed"] == ["Redis"]


def test_adding_a_term_the_other_list_holds_is_refused_before_the_write():
    """Refused here rather than at the swap.

    Writing it would produce a file that fails its own loader, which surfaces as a
    `RefusedWrite` naming a rule the person clicking never saw.
    """
    text = kw.edit(kw.render(kw.Keywords()), "denied", "Kubernetes")
    with pytest.raises(kw.RefusedTerm, match="remove it from there first"):
        kw.edit(text, "allowed", "kubernetes")
    with pytest.raises(kw.RefusedTerm, match="already in"):
        kw.edit(text, "denied", "KUBERNETES")


def test_a_pasted_paragraph_is_not_a_term():
    """A term is a technology name. The cap is what makes that assertion true."""
    with pytest.raises(kw.RefusedTerm):
        kw.validate_term("x" * (kw.MAX_TERM_CHARS + 1))
    with pytest.raises(kw.RefusedTerm):
        kw.validate_term("   ")
    assert kw.validate_term("  Amazon   S3 ") == "Amazon S3"


# -- the two mechanisms --------------------------------------------------------------
def test_a_denied_term_drops_the_edit_and_a_prompt_cannot_argue_with_it(latex):
    """`denied` is applied in Python, which is the whole point of it being a second list.

    `allowed` steers a model and a model is steerable, not bound. A ruling that a
    technology is one you do not know is not something a prompt gets to reconsider.
    """
    keywords = kw.Keywords(allowed=("Postgres",), denied=("Kafka",))
    out = tailor.parse_edits(_answer(flagged=[]), RESUME, DESCRIPTION, latex, keywords)
    assert out.edits == []


def test_an_edit_dropped_for_a_denied_term_is_still_written(latex):
    """Otherwise the posting is re-asked every night, forever, for the same answer.

    A denied drop is a *settled* outcome: you ruled the technology out, the model
    proposed it anyway, and nothing about tomorrow changes either fact. Returning None
    would leave the unit pending and spend one model call a night on it.
    """
    keywords = kw.Keywords(denied=("Kafka",))
    assert tailor.parse_edits(
        _answer(flagged=[]), RESUME, DESCRIPTION, latex, keywords
    ) is not None
    # ...whereas a genuinely empty answer still leaves the posting in the queue.
    assert tailor.parse_edits(
        json.dumps({"edits": [], "flagged": []}), RESUME, DESCRIPTION, latex, keywords
    ) is None


def test_an_undecided_term_holds_an_edit_rather_than_dropping_it(latex):
    """The question is open, so the work is kept and withheld — not thrown away.

    This is what makes Include cost zero model calls: the edit already exists, and ruling
    on the term releases it.
    """
    keywords = kw.Keywords(allowed=("Postgres",))
    out = tailor.parse_edits(_answer(), RESUME, DESCRIPTION, latex, keywords)
    assert len(out.edits) == 1 and [f.term for f in out.flagged] == ["Kafka"]

    flags = [f.as_dict() for f in out.flagged]
    ok, blocked = kw.split_edits(out.edits, flags, keywords)
    assert ok == [] and [terms for _e, terms in blocked] == [["Kafka"]]

    # Include it: the same stored edit becomes compilable, with nothing re-run.
    ok, blocked = kw.split_edits(
        out.edits, flags, kw.Keywords(allowed=("Postgres", "Kafka"))
    )
    assert len(ok) == 1 and blocked == []


def test_excluding_a_term_also_blocks_the_edits_that_prompted_the_ruling(latex):
    """The hole `parse_edits` alone cannot close, and the one that motivates Exclude.

    Rule 7 drops an edit carrying a denied term, which covers every proposal made *after*
    the ruling. It cannot cover the ones already stored — and the edit sitting in the
    table when you press Exclude is, by construction, the one that made you press it. A
    `blocking_terms` that asked "has this been ruled on" would call the term settled and
    let that edit compile, turning "never write this again" into "never write this again,
    except here".
    """
    keywords = kw.Keywords(allowed=("Postgres",))
    stored = tailor.parse_edits(_answer(), RESUME, DESCRIPTION, latex, keywords)
    assert len(stored.edits) == 1
    flags = [f.as_dict() for f in stored.flagged]

    after = kw.Keywords(allowed=("Postgres",), denied=("Kafka",))
    ok, blocked = kw.split_edits(stored.edits, flags, after)
    assert ok == [] and [terms for _e, terms in blocked] == [["Kafka"]]

    # And it reads as a decision, not as a question — a banner saying "waiting on you"
    # about a term you excluded last week would never go away.
    undecided, denied = kw.describe_blocked(["Kafka"], after)
    assert undecided == [] and denied == ["Kafka"]
    undecided, denied = kw.describe_blocked(["Kafka"], keywords)
    assert undecided == ["Kafka"] and denied == []


def test_a_flag_is_grounded_at_both_ends(latex):
    """`term` and `evidence` must both occur in the job description, verbatim.

    `inbox`'s quote rule, which is `repair`'s rule that a proposed slug must appear on the
    page it was read from. An ungrounded flag is a technology nobody asked for, being
    proposed for a resume it would then go on.
    """
    out = tailor.parse_edits(
        _answer(edits=[], flagged=[
            {"term": "Rust", "evidence": "Experience with Kafka is required", "why": "x"},
            {"term": "Kafka", "evidence": "we rewrote it all in Rust", "why": "x"},
            {"term": "Kubernetes", "evidence": "we use Postgres and Kubernetes",
             "why": "ok"},
        ]),
        RESUME, DESCRIPTION, latex, kw.Keywords(),
    )
    # Rust is not in the description; Kafka's evidence is not; only Kubernetes survives.
    assert [f.term for f in out.flagged] == ["Kubernetes"]


def test_a_term_already_ruled_on_or_already_on_the_resume_is_never_flagged(latex):
    """A decision you made is not a question, and neither is a word already printed.

    Re-surfacing a `denied` term every night would make excluding one feel like it did
    nothing at all.
    """
    flags = [{"term": "Kafka", "evidence": "Experience with Kafka is required", "why": ""},
             {"term": "Postgres", "evidence": "we use Postgres and Kubernetes", "why": ""}]
    for keywords in (kw.Keywords(allowed=("Kafka",)), kw.Keywords(denied=("Kafka",))):
        out = tailor.parse_edits(
            _answer(edits=[], flagged=flags), RESUME, DESCRIPTION, latex, keywords
        )
        # Kafka is ruled on; Postgres is already in the resume. Nothing is left to ask.
        assert out is None or [f.term for f in out.flagged] == []


def test_the_flag_cap_bounds_how_many_questions_one_posting_can_ask(latex):
    """A page carrying twelve new questions per job is one nobody answers."""
    desc = "We use " + ", ".join(f"Tech{i}" for i in range(10)) + " here."
    flags = [{"term": f"Tech{i}", "evidence": f"Tech{i}", "why": ""} for i in range(10)]
    out = tailor.parse_edits(
        _answer(edits=[], flagged=flags), RESUME, desc, latex, kw.Keywords()
    )
    assert len(out.flagged) == tailor.MAX_FLAGGED


def test_the_allowed_list_reaches_the_prompt_and_the_denied_list_too():
    """`allowed` is the steering half and has to actually be sent.

    A guard that only ever refuses produces a model that proposes the same rejected edit
    every night — every one of those a call spent on an answer that is thrown away.
    """
    from jobtracker.tasks.base import TaskContext

    ctx = TaskContext(today="2026-09-04", resume_text=RESUME, resume_hash="rh",
                      resume_format=get_format("latex"),
                      keywords=kw.Keywords(allowed=("Postgres",), denied=("Kafka",)))
    sent = {}

    class _Client:
        async def complete(self, **kwargs):
            sent.update(kwargs)
            return json.dumps({"edits": [], "flagged": []})

    import asyncio

    from jobtracker.tasks.base import TaskUnit

    unit = TaskUnit(task="tailor", company="Ramp", ats_job_id="7",
                    unit_key="k", title="Backend Engineer",
                    payload={"description": DESCRIPTION})
    asyncio.run(tailor.TailorTask().run(unit, _Client(), ctx))
    assert "- Postgres" in sent["user"]
    assert "RULED OUT" in sent["user"] and "- Kafka" in sent["user"]


def test_ruling_on_a_term_re_asks_every_posting(tmp_path):
    """The lists are half the question, so they belong in the unit key.

    Same mechanism `resume_hash` has: change what you asked and the cached answer is an
    answer to a different question.
    """
    from jobtracker import store
    from jobtracker.tasks.base import TaskContext

    def ctx_for(keywords):
        return TaskContext(today="2026-09-04", resume_text=RESUME, resume_hash="rh",
                           resume_format=get_format("latex"), keywords=keywords)

    before = tailor._unit_key(ctx_for(kw.Keywords()))
    after = tailor._unit_key(ctx_for(kw.Keywords(allowed=("Kafka",))))
    assert before != after

    from jobtracker.models import Decision, Posting, Verdict

    conn = store.connect(":memory:")
    try:
        store.sync_postings(conn, "Ramp", [Posting(
            company="Ramp", ats_job_id="7", title="Backend Engineer", location="NYC",
            url="https://example.com/7", posted_at=None, description=DESCRIPTION,
        )], "2026-09-01")
        store.set_description(conn, "Ramp", "7", DESCRIPTION)
        store.record_verdict(conn, Verdict(company="Ramp", ats_job_id="7",
                                           decision=Decision.MATCH, reason=""),
                             "2026-09-01")
        conn.execute(
            "INSERT INTO rankings (company, ats_job_id, backend_fit, growth, entry_risk,"
            " why, prose_hash, judged_at, score, scored_at) "
            "VALUES ('Ramp','7','strong','strong','low','','p','2026-09-01',9.0,"
            "'2026-09-01')"
        )
        store.record_suggestions(conn, "Ramp", "7", "[]", "rh", "2026-09-04",
                                 keywords_hash=kw.Keywords().hash)
        conn.commit()
        assert store.matches_needing_tailoring(conn, "rh", kw.Keywords().hash) == []
        assert len(store.matches_needing_tailoring(
            conn, "rh", kw.Keywords(allowed=("Kafka",)).hash)) == 1
    finally:
        conn.close()


def test_both_things_that_compile_a_resume_ask_the_same_function():
    """One derivation of "which edits may be compiled", shared by the two compilers.

    `jobtracker tailor build` and `POST /api/tailor-build` are the only two code paths
    that turn stored edits into a PDF, and a second copy of this decision is how the
    button and the terminal come to mean different documents under your name — the rule
    `resume.tailored_stem` already carries for the path itself.

    The render paths call `blocking_terms` instead, which is the per-edit half of the same
    function: they mark rows, they do not decide what is compiled.
    """
    import inspect

    from jobtracker import cli, server

    build = inspect.getsource(cli.cmd_tailor)
    endpoint = inspect.getsource(server.Handler._api_tailor_build)
    for name, source in (("cmd_tailor", build), ("_api_tailor_build", endpoint)):
        assert "split_edits(" in source, name
        # And neither re-implements the scan: no direct term matching in a compiler.
        assert "occurs(" not in source, name


# -- the dashboard's reading ---------------------------------------------------------
def test_a_row_whose_every_edit_is_held_renders_a_state_not_a_build_button(tmp_path,
                                                                           monkeypatch):
    """The rule the `tracked` chip follows.

    Re-tracking a job you already tracked is harmless and the button is still wrong,
    because a live-looking control over something that cannot proceed is the page
    disagreeing with itself. A build here would refuse on click for the same reason.

    Some edits held is a different fact: that is a shorter PDF, still worth building, and
    it renders as a button whose chip title says what is missing.
    """
    from jobtracker import config, dashboard, store

    kwp = tmp_path / "keywords.yaml"
    kwp.write_text(kw.render(kw.Keywords(allowed=("Postgres",))))
    monkeypatch.setattr(config, "KEYWORDS_YAML", kwp)

    def cell(edits):
        conn = store.connect(":memory:")
        try:
            store.record_suggestions(
                conn, "Ramp", "7", json.dumps(edits), "rh", "2026-09-04",
                flagged=json.dumps([{"term": "Kafka", "evidence": "we use Kafka",
                                     "why": ""}]),
                keywords_hash="kh",
            )
            conn.commit()
            suggestions = store.suggestions_by_posting(conn)
        finally:
            conn.close()
        held = dashboard._held_by_posting(suggestions)
        row = {"company": "Ramp", "ats_job_id": "7"}
        return dashboard._track_cell(row, set(), suggestions, set(), held)

    every = cell([{"section": "skills", "current_line": "a",
                   "suggestion": "Kafka and Postgres", "evidence": "e"}])
    assert 'class="theld"' in every and "tailor-build" not in every

    some = cell([{"section": "skills", "current_line": "a",
                  "suggestion": "Kafka and Postgres", "evidence": "e"},
                 {"section": "skills", "current_line": "b",
                  "suggestion": "Postgres only", "evidence": "e"}])
    assert "tailor-build" in some and 'class="theld"' not in some


def test_the_held_reading_costs_one_file_read_for_the_whole_page(tmp_path, monkeypatch):
    """These tables carry thousands of rows, and this reads a config file.

    `_built_resumes` lists the directory once for the same reason. A per-row load would be
    a `stat` per posting wearing different clothes.
    """
    from jobtracker import config, dashboard, store

    kwp = tmp_path / "keywords.yaml"
    kwp.write_text(kw.render(kw.Keywords(allowed=("Postgres",))))
    monkeypatch.setattr(config, "KEYWORDS_YAML", kwp)

    reads = []
    real = kw.load_keywords
    monkeypatch.setattr(kw, "load_keywords",
                        lambda p: (reads.append(p), real(p))[1])

    conn = store.connect(":memory:")
    try:
        for i in range(20):
            store.record_suggestions(
                conn, "Ramp", str(i),
                json.dumps([{"section": "skills", "current_line": "a",
                             "suggestion": "Kafka", "evidence": "e"}]),
                "rh", "2026-09-04",
                flagged=json.dumps([{"term": "Kafka", "evidence": "we use Kafka",
                                     "why": ""}]),
                keywords_hash="kh",
            )
        conn.commit()
        suggestions = store.suggestions_by_posting(conn)
    finally:
        conn.close()

    held = dashboard._held_by_posting(suggestions)
    assert len(held) == 20
    assert len(reads) == 1
