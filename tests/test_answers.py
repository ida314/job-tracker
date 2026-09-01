"""answers.yaml: strict loading, the hash contract, and the machine-managed tail.

This file holds text that gets typed into real job applications, so the loader is strict
in the same way `profile.py` and `criteria.py` are — a silently-defaulted answer is the
failure mode that matters here, not a crash.

Two mechanisms have tests because they are easy to break invisibly:

* `Answers.hash` covers the answers *and their aliases* and nothing else, so
  regenerating the gap block or editing a comment must not invalidate a single prefill
  plan — while attaching a question to an answer must invalidate exactly the plans that
  asked it. Aliases joined the hash on 2026-08-25, when attaching became the way a
  question gets answered rather than a shortcut past a model that would have caught it.
* Adding an answer is text surgery, not a YAML round trip, because a round trip would
  silently delete every comment in the file — including the stubs being worked through.
"""

import pytest
import yaml

from jobtracker.answers import (
    GAP_MARKER,
    STARTER,
    insert_answer,
    load_answers,
    normalize_label,
    render_gap_block,
    rewrite_gaps,
    set_resume,
    set_resume_name,
    slugify,
    upsert_identity,
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


def test_attaching_a_question_moves_the_hash(tmp_path):
    """An alias changes no value, and it still has to invalidate the plans.

    This is the one that would have made the whole change a silent dead end. Attaching
    "Are you legally authorized to work in the US?" to an answer you already wrote is
    now the main way a question gets answered — it is what the model pass used to do —
    and it edits only the alias list. With aliases out of the hash,
    `matches_needing_prefill`'s `answers_hash != ?` never fires, the plan is never
    rebuilt, and the field you had just explained stays a gap forever while the page
    says it saved.
    """
    path = _write(tmp_path)
    path.write_text(insert_answer(path.read_text(), "work_authorization", "Yes"))
    before = load_answers(path).hash

    path.write_text(insert_answer(path.read_text(), "work_authorization", "Yes",
                                  ["Are you legally authorized to work in the US?"]))
    after = load_answers(path)
    assert after.hash != before
    assert "are you legally authorized to work in the us" in after.by_alias


def test_the_hash_does_not_move_for_an_alias_already_written(tmp_path):
    """`insert_answer` is additive, so re-saving the same answer is a no-op — and a
    no-op must not re-plan every posting. Otherwise the nightly run rebuilds the whole
    corpus whenever a page saves a value that had not changed."""
    path = _write(tmp_path)
    path.write_text(insert_answer(path.read_text(), "work_authorization", "Yes",
                                  ["Authorized to work?"]))
    before = load_answers(path).hash
    path.write_text(insert_answer(path.read_text(), "work_authorization", "Yes",
                                  ["Authorized to work?"]))
    assert load_answers(path).hash == before


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


# -- the identity and resume writers -------------------------------------------------
#
# Both are text surgery for the reason `insert_answer` is: `answers.yaml` carries
# hand-written comments and the commented-out stubs the user is working through, and a
# YAML round trip would delete every one of them.

DOCUMENTED = """\
# What you would type into an application form.

identity:
  first_name: Dylan
  last_name: D
  # preferred_name: Ada
  email: d@example.edu
  # phone: "+1 555 010 0000"

# A path, relative to this file.
resume: ./resume.pdf

answers:
  work_authorization: "Yes"

# ===== unanswered questions · regenerated by `jobtracker prefill` =====
#
# (nothing outstanding)
"""


def test_upsert_identity_replaces_a_value_that_is_already_set(tmp_path):
    path = _write(tmp_path, DOCUMENTED)
    path.write_text(upsert_identity(path.read_text(), {"email": "new@nyu.edu"}))
    assert load_answers(path).get("email") == "new@nyu.edu"
    assert path.read_text().count("email:") == 1


def test_upsert_identity_fills_a_commented_stub_in_place(tmp_path):
    """The example ships the optional fields commented out as documentation.

    Answering one has to replace that line where it stands. Appending a second entry
    instead would leave the file carrying `# phone: "+1 555 010 0000"` directly above a
    real phone number — two lines that disagree, one of which is not what will be typed.
    """
    path = _write(tmp_path, DOCUMENTED)
    path.write_text(upsert_identity(path.read_text(), {"phone": "+1 917 555 0123"}))
    body = path.read_text()
    assert load_answers(path).get("phone") == "+1 917 555 0123"
    assert "# phone:" not in body
    # And it kept its position: still below email, still above the resume comment.
    assert body.index("email:") < body.index("phone:") < body.index("resume:")


def test_upsert_identity_adds_a_field_that_was_never_mentioned(tmp_path):
    path = _write(tmp_path, DOCUMENTED)
    path.write_text(upsert_identity(path.read_text(), {"github": "https://github.com/x"}))
    assert load_answers(path).get("github") == "https://github.com/x"


def test_upsert_identity_preserves_every_comment_and_the_gap_block(tmp_path):
    path = _write(tmp_path, DOCUMENTED)
    path.write_text(upsert_identity(path.read_text(), {"location": "New York, NY"}))
    body = path.read_text()
    assert "# What you would type into an application form." in body
    assert "# A path, relative to this file." in body
    assert GAP_MARKER in body
    assert "(nothing outstanding)" in body


def test_clearing_an_identity_field_removes_it_rather_than_blanking_it(tmp_path):
    """An empty value would load as an answer and be typed as the empty string.

    A blank in a submitted form is indistinguishable from a field we had no answer for,
    which is exactly the ambiguity this file's strictness exists to remove.
    """
    path = _write(tmp_path, DOCUMENTED)
    path.write_text(upsert_identity(path.read_text(), {"email": "d@example.edu",
                                                       "last_name": "D",
                                                       "first_name": "Dylan",
                                                       "preferred_name": ""}))
    assert "preferred_name" not in load_answers(path).identity


def test_upsert_identity_refuses_a_key_that_is_not_a_real_field(tmp_path):
    """`IDENTITY_KEYS` is closed on purpose: a typo'd key is never filled, and a form
    field left blank looks exactly like one we had no answer for."""
    with pytest.raises(ValueError, match="unknown identity field"):
        upsert_identity(DOCUMENTED, {"emial": "d@example.edu"})


def test_the_starter_becomes_a_loadable_bank_once_you_name_yourself(tmp_path):
    """The bootstrap: saving your identity is what creates the file.

    STARTER deliberately carries no placeholder identity — `answers.example.yaml` ships
    Ada Lovelace's name and email as documentation, and a bank that loads with a
    stranger's identity in it would type that identity into a real application.
    """
    assert "Ada" not in STARTER and "example.edu" not in STARTER
    path = tmp_path / "answers.yaml"
    path.write_text(upsert_identity(STARTER, {"first_name": "Dylan", "last_name": "D",
                                              "email": "d@nyu.edu"}))
    bank = load_answers(path)
    assert bank.get("first_name") == "Dylan"
    assert bank.get("email") == "d@nyu.edu"
    assert GAP_MARKER in path.read_text()


def test_set_resume_points_at_a_file_beside_the_bank(tmp_path):
    path = _write(tmp_path, DOCUMENTED)
    (tmp_path / "resume.docx").write_bytes(b"PK\x03\x04")
    path.write_text(set_resume(path.read_text(), "resume.docx"))
    assert load_answers(path).resume == (tmp_path / "resume.docx")
    assert path.read_text().count("resume:") == 1


def test_set_resume_writes_the_key_when_the_file_never_had_one(tmp_path):
    path = _write(tmp_path, MINIMAL)
    path.write_text(set_resume(path.read_text(), "resume.pdf"))
    assert load_answers(path).resume == (tmp_path / "resume.pdf")


def test_a_first_save_comes_out_in_canonical_order(tmp_path):
    """Not in whatever order the form's fields arrived, and not reversed.

    The order is also what keeps a new key from landing above a trailing comment that
    introduces the *next* section — a freshly created `identity:` is followed by exactly
    that in STARTER.
    """
    path = tmp_path / "answers.yaml"
    path.write_text(upsert_identity(STARTER, {"email": "d@nyu.edu", "school": "NYU",
                                              "last_name": "D", "first_name": "Dylan"}))
    # The keys, not the prose — the header comment mentions these names too.
    body = path.read_text()
    assert (body.index("  first_name:") < body.index("  last_name:")
            < body.index("  email:") < body.index("  school:"))
    assert body.index("  school:") < body.index("# A path, relative to this file.")
    assert load_answers(path).get("school") == "NYU"


# -- changing an answer you already wrote ----------------------------------------------
def test_answering_a_question_twice_updates_it_rather_than_shadowing_it():
    """This used to insert unconditionally, and the failure was completely silent.

    A key already in the file got a *second* YAML mapping key with the same name.
    `yaml.safe_load` accepts that and keeps the last occurrence — the old one, since the
    new entry went in at the top — so `load_answers` validated it, `safewrite` accepted
    it, the page said saved, and the value you typed was discarded. `/apply` now offers
    this on every field rather than only on unanswered ones, so "save" usually means
    "change what I told you last time".
    """
    text = (
        "identity:\n  first_name: A\n  last_name: B\n  email: a@b.c\n\n"
        "answers:\n"
        "  work_authorization:\n"
        "    value: \"Yes\"\n"
        "    aliases:\n"
        "      - \"Are you authorized to work in the US?\"\n"
        "  # a comment of my own\n"
        "  current_employer: \"NYU\"\n"
    )
    out = insert_answer(text, "work_authorization", "No")

    assert yaml.safe_load(out)["answers"]["work_authorization"]["value"] == "No"
    assert out.count("  work_authorization:") == 1
    assert "# a comment of my own" in out


def test_editing_an_answer_keeps_the_aliases_that_made_it_match():
    """An alias is the exact wording one employer used. Editing the value is not a
    statement about any of them, and dropping them would un-match every form the answer
    already fills."""
    text = (
        "identity:\n  first_name: A\n  last_name: B\n  email: a@b.c\n\n"
        "answers:\n"
        "  sponsorship:\n"
        "    value: \"No\"\n"
        "    aliases: [\"Will you require sponsorship?\"]\n"
    )
    out = insert_answer(text, "sponsorship", "Yes", ["Do you need a visa?"])
    parsed = yaml.safe_load(out)["answers"]["sponsorship"]

    assert parsed["value"] == "Yes"
    assert parsed["aliases"] == ["Will you require sponsorship?", "Do you need a visa?"]
    # Saving the same thing again does not grow the list.
    again = yaml.safe_load(insert_answer(out, "sponsorship", "Yes",
                                         ["Do you need a visa?"]))
    assert len(again["answers"]["sponsorship"]["aliases"]) == 2


def test_a_scalar_answer_becomes_the_long_form_when_it_gains_an_alias():
    """A plain string is a legal answer and has nowhere to put an alias. Expanding it is
    the only way an edit can also record the question it was an answer to."""
    text = ("identity:\n  first_name: A\n  last_name: B\n  email: a@b.c\n\n"
            "answers:\n  current_employer: \"NYU\"\n")
    parsed = yaml.safe_load(
        insert_answer(text, "current_employer", "Anthropic", ["Current employer?"])
    )["answers"]["current_employer"]

    assert parsed == {"value": "Anthropic", "aliases": ["Current employer?"]}


# -- the name a resume goes out under --------------------------------------------------
def test_the_resume_can_be_given_the_name_an_employer_sees(tmp_path):
    """Disk names are minted for collision safety and read like it. This is the one a
    person at the other end opens, and until now it was the same string."""
    text = ("identity:\n  first_name: A\n  last_name: B\n  email: a@b.c\n"
            "resume: ./resume.pdf\n")
    out = set_resume_name(text, "Dylan_Dodds_Resume.pdf")

    assert yaml.safe_load(out)["resume_name"] == "Dylan_Dodds_Resume.pdf"
    # Beside the key it qualifies, and replaced in place on a second save.
    assert out.count("resume_name:") == 1
    assert set_resume_name(out, "Other.pdf").count("resume_name:") == 1
    # Clearing removes the key. A blank would load as an answer and send a file called
    # ".pdf" — the same reading `upsert_identity` refuses for an identity field.
    assert "resume_name" not in yaml.safe_load(set_resume_name(out, ""))


def test_the_sent_name_is_not_part_of_the_answers_hash(tmp_path):
    """It changes which name an upload carries, not which answer goes in any field, so a
    plan built before it was set is still a correct plan. Folding it in would re-plan
    every posting for a cosmetic rename."""
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4 x")
    path = tmp_path / "answers.yaml"
    base = ("identity:\n  first_name: A\n  last_name: B\n  email: a@b.c\n"
            "resume: ./resume.pdf\n")
    path.write_text(base)
    before = load_answers(path).hash
    path.write_text(set_resume_name(base, "Ada_Lovelace_CV.pdf"))
    after = load_answers(path)

    assert after.resume_name == "Ada_Lovelace_CV.pdf"
    assert after.hash == before
