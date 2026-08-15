"""answers.yaml: strict loading, the hash contract, and the machine-managed tail.

This file holds text that gets typed into real job applications, so the loader is strict
in the same way `profile.py` and `criteria.py` are — a silently-defaulted answer is the
failure mode that matters here, not a crash.

Two mechanisms have tests because they are easy to break invisibly:

* `Answers.hash` covers the answers and nothing else, so regenerating the gap block or
  editing a comment must not invalidate a single prefill plan.
* Adding an answer is text surgery, not a YAML round trip, because a round trip would
  silently delete every comment in the file — including the stubs being worked through.
"""

import pytest

from jobtracker.answers import (
    GAP_MARKER,
    insert_answer,
    load_answers,
    normalize_label,
    render_gap_block,
    rewrite_gaps,
    slugify,
)

MINIMAL = """\
identity:
  first_name: Dylan
  last_name: D
  email: d@example.edu

answers:
  work_authorization: "Yes"
"""


def _write(tmp_path, body=MINIMAL, resume=True):
    path = tmp_path / "answers.yaml"
    path.write_text(body)
    if resume:
        (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4")
    return path


# -- loading -----------------------------------------------------------------------
def test_a_minimal_bank_loads(tmp_path):
    a = load_answers(_write(tmp_path))
    assert a.get("first_name") == "Dylan"
    assert a.get("work_authorization") == "Yes"
    assert a.get("nothing_like_this") is None
    assert a.resume is None                    # not declared, and that is fine


@pytest.mark.parametrize("body, message", [
    ("identity:\n  first_name: D\n", "missing"),                     # no last_name/email
    ("identity:\n  first_name: D\n  last_name: D\n  email: e\nnope: 1\n", "unknown key"),
    ("identity:\n  frist_name: D\n  last_name: D\n  email: e\n", "unknown identity"),
    ("- a\n- b\n", "mapping"),
])
def test_the_loader_refuses_rather_than_defaulting(tmp_path, body, message):
    path = tmp_path / "answers.yaml"
    path.write_text(body)
    with pytest.raises(ValueError, match=message):
        load_answers(path)


def test_an_empty_answer_is_refused_with_a_reason(tmp_path):
    """Blank and missing are indistinguishable once it reaches a form field."""
    path = _write(tmp_path, MINIMAL + '  why_us: ""\n')
    with pytest.raises(ValueError, match="real application"):
        load_answers(path)


def test_a_missing_resume_is_caught_at_load_not_at_fill_time(tmp_path):
    """The worst moment to discover this is with a browser on an open application."""
    path = _write(tmp_path, MINIMAL + "\nresume: ./nope.pdf\n", resume=False)
    with pytest.raises(ValueError, match="is not a file"):
        load_answers(path)


def test_a_resume_path_is_relative_to_the_file(tmp_path):
    a = load_answers(_write(tmp_path, MINIMAL + "\nresume: ./resume.pdf\n"))
    assert a.resume is not None and a.resume.name == "resume.pdf"


# -- aliases ------------------------------------------------------------------------
def test_an_alias_recognizes_the_same_question_asked_differently(tmp_path):
    path = _write(tmp_path, """\
identity:
  first_name: D
  last_name: D
  email: e@x.edu
answers:
  work_authorization:
    value: "Yes"
    aliases:
      - "Are you legally authorized to work in the United States?"
""")
    a = load_answers(path)
    key = a.by_alias[normalize_label("Are you legally authorized to work in the United States?")]
    assert a.get(key) == "Yes"
    # The key itself always works as its own alias, so a plain scalar answer is usable.
    assert a.by_alias[normalize_label("work authorization")] == "work_authorization"


def test_normalization_ignores_punctuation_and_required_markers():
    """A trailing asterisk marking a field required is not a different question."""
    assert normalize_label("Email Address *") == normalize_label("email address")
    assert normalize_label("Who is your  employer?") == "who is your employer"
    assert slugify("How did you hear about this role?") == "how_did_you_hear_about_this_role"
    # Capped at eight words so a paragraph-length question still yields a usable key.
    assert slugify("a b c d e f g h i j k") == "a_b_c_d_e_f_g_h"


# -- the hash contract --------------------------------------------------------------
def test_the_hash_covers_answers_and_not_comments(tmp_path):
    path = _write(tmp_path)
    before = load_answers(path).hash
    path.write_text("# a comment nobody asked for\n" + path.read_text())
    assert load_answers(path).hash == before


def test_adding_an_answer_moves_the_hash(tmp_path):
    """That is what re-queues the prefill plans which needed it, and nothing else."""
    path = _write(tmp_path)
    before = load_answers(path).hash
    path.write_text(insert_answer(path.read_text(), "current_employer", "NYU"))
    assert load_answers(path).hash != before


# -- the machine-managed tail -------------------------------------------------------
def _gap(**over):
    base = {"question_key": "current_employer", "ask": "Who is your current employer?",
            "type": "text", "options": None, "seen_on": "Stripe,Ramp",
            "first_seen": "2026-08-13", "resolved_at": None}
    base.update(over)
    return base


def test_the_gap_block_is_commented_out(tmp_path):
    """Live keys with empty values would load as answers and be typed into a form."""
    path = _write(tmp_path)
    rewrite_gaps(path, [_gap()])
    body = path.read_text()
    assert "# current_employer:" in body
    assert "Who is your current employer?" in body
    assert "Stripe,Ramp" in body
    # Still valid, and still holds exactly the one answer that was there before.
    assert load_answers(path).answerable == ["email", "first_name", "last_name",
                                             "work_authorization"]


def test_regenerating_the_tail_never_touches_what_is_above_it(tmp_path):
    path = _write(tmp_path)
    before = load_answers(path).hash

    rewrite_gaps(path, [_gap()])
    rewrite_gaps(path, [_gap(question_key="why_us", ask="Why us?")])
    body = path.read_text()

    assert body.count(GAP_MARKER) == 1          # replaced, not appended to
    assert "current_employer" not in body       # answered gaps disappear
    assert "why_us" in body
    assert "work_authorization" in body         # the user's own content survives
    assert load_answers(path).hash == before    # and no plan is invalidated


def test_options_are_carried_into_the_stub(tmp_path):
    path = _write(tmp_path)
    rewrite_gaps(path, [_gap(type="select", options="United States | Canada")])
    assert "United States | Canada" in path.read_text()


def test_an_empty_gap_list_says_so(tmp_path):
    path = _write(tmp_path)
    rewrite_gaps(path, [])
    assert "nothing outstanding" in path.read_text()
    assert render_gap_block([]).startswith(GAP_MARKER)


# -- inserting an answer ------------------------------------------------------------
def test_insert_answer_preserves_comments(tmp_path):
    """A YAML round trip would delete every one of them, stubs included."""
    body = "# keep me\n" + MINIMAL + "\n" + render_gap_block([_gap()])
    out = insert_answer(body, "current_employer", "NYU",
                        ["Who is your current employer?"])
    assert "# keep me" in out
    assert "# current_employer:" in out         # the stub is still there for now
    assert "  current_employer:" in out         # and the real answer was added
    assert 'aliases: ["Who is your current employer?"]' in out


def test_insert_answer_creates_the_block_when_there_is_none(tmp_path):
    body = "identity:\n  first_name: D\n  last_name: D\n  email: e@x.edu\n"
    path = tmp_path / "answers.yaml"
    path.write_text(insert_answer(body, "why_us", "Because."))
    assert load_answers(path).get("why_us") == "Because."


def test_a_value_with_yaml_metacharacters_survives(tmp_path):
    """Answers are prose from a human: colons, hashes and quotes all show up."""
    hostile = 'Note: "scale" — 40% #1, a: b'
    path = _write(tmp_path)
    path.write_text(insert_answer(path.read_text(), "why_us", hostile))
    assert load_answers(path).get("why_us") == hostile


def test_a_file_upload_stub_does_not_pretend_to_take_text(tmp_path):
    """You cannot answer "Attach" with a sentence.

    Observed on Cloudflare's live form: the cover-letter input is labelled "Attach", and
    a generic `value: ""` stub would send the user off to type an answer for a file
    upload. The two keys that take a path are named instead.
    """
    path = _write(tmp_path)
    rewrite_gaps(path, [_gap(question_key="attach", ask="Attach", type="file")])
    body = path.read_text()
    assert "a FILE upload" in body
    assert "cover_letter" in body
    assert 'value: ""' not in body.split(GAP_MARKER)[1]
