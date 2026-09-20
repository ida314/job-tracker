"""Where a saved document goes: the four destinations, and the file that holds them.

The module is pure — `resolve_dir` touches the disk only to ask whether a path is
already a file, and `save` is the one function that writes. Everything else is text.
"""

import pytest

from jobtracker import downloads


def test_nothing_configured_means_downloads(monkeypatch, tmp_path):
    """The default that makes this feature installable: four kinds, one folder, no file.

    Absent is a normal state — this is a file you grow by clicking, so a fresh checkout
    has none."""
    for env in downloads.ENV.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert downloads.load_raw(None) == {k: "~/Downloads" for k in downloads.KINDS}
    assert downloads.load_downloads(tmp_path / "nope.yaml") == {
        k: tmp_path / "Downloads" for k in downloads.KINDS
    }


def test_the_file_outranks_the_environment_which_outranks_the_default(monkeypatch,
                                                                     tmp_path):
    """Three layers, and the order is what the Settings card promises: the thing you
    typed on a page you opened wins, because a setting the page cannot change is one the
    page should not be showing you."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JOBTRACKER_DOWNLOAD_RESUME", "/env/resume")
    monkeypatch.setenv("JOBTRACKER_DOWNLOAD_LETTER", "/env/letter")
    monkeypatch.delenv("JOBTRACKER_DOWNLOAD_RESUME_TEX", raising=False)
    monkeypatch.delenv("JOBTRACKER_DOWNLOAD_LETTER_TEX", raising=False)
    path = tmp_path / "downloads.yaml"
    path.write_text("resume: '/from/the/file'\n")

    raw = downloads.load_raw(path)
    assert raw["resume"] == "/from/the/file"     # the file
    assert raw["letter"] == "/env/letter"        # the environment
    assert raw["resume_tex"] == "~/Downloads"    # the built-in default


def test_a_path_is_stored_as_you_wrote_it(tmp_path, monkeypatch):
    """`~` survives the round trip. Expanding before the write would bake one machine's
    home directory into a curated file, and the expansion is a question about the
    machine reading it rather than about the decision made."""
    monkeypatch.setenv("HOME", str(tmp_path))
    body = downloads.edit("", "letter", "~/work/letters")
    (tmp_path / "d.yaml").write_text(body)
    assert "letter: '~/work/letters'" in body
    assert str(tmp_path) not in body
    assert downloads.load_downloads(tmp_path / "d.yaml")["letter"] == \
        tmp_path / "work" / "letters"


def test_editing_one_destination_keeps_every_other_line(monkeypatch, tmp_path):
    """`keywords.edit`'s rule: this file is mostly the header explaining what the four
    kinds are and where they fall back to, and `yaml.safe_dump` deletes all of it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for env in downloads.ENV.values():
        monkeypatch.delenv(env, raising=False)
    before = downloads.render(downloads.load_raw(None))
    after = downloads.edit(before, "resume_tex", "/srv/tex")

    assert "resume_tex: '/srv/tex'" in after
    kept = [line for line in before.splitlines() if line.startswith("#")]
    assert kept and all(line in after.splitlines() for line in kept)
    assert after.count("resume_tex:") == 1
    assert "resume: '~/Downloads'" in after


def test_a_kind_the_file_does_not_name_is_appended(monkeypatch, tmp_path):
    """An older file, or one somebody trimmed by hand, is still a file whose comments
    are worth keeping."""
    monkeypatch.setenv("HOME", str(tmp_path))
    after = downloads.edit("# mine\nresume: '/a'\n", "letter_tex", "/b")
    assert after == "# mine\nresume: '/a'\nletter_tex: '/b'\n"


def test_an_indented_key_is_not_the_one_being_set():
    """Column 0 only. Nothing in this file nests today, and a matcher that ignored
    indentation is how that would stop being true by accident."""
    after = downloads.edit("other:\n  resume: '/nested'\n", "resume", "/top")
    assert "  resume: '/nested'" in after
    assert after.endswith("resume: '/top'\n")


def test_a_path_with_a_comment_character_survives_the_round_trip(tmp_path, monkeypatch):
    """Quoted unconditionally rather than only when it has to be. A `#` in a directory
    name would otherwise truncate the value to everything before it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "jobs #2"
    body = downloads.edit("", "resume", str(target))
    (tmp_path / "d.yaml").write_text(body)
    assert downloads.load_downloads(tmp_path / "d.yaml")["resume"] == target


@pytest.mark.parametrize("bad", ["", "   ", "relative/dir", "./here"])
def test_a_destination_has_to_be_absolute(bad):
    """A relative path means "wherever `serve` happens to have been started from", which
    is a different directory depending on how it was launched — the one property a
    destination must not have."""
    with pytest.raises(downloads.RefusedPath):
        downloads.resolve_dir(bad)


def test_a_destination_that_is_a_file_is_refused(tmp_path):
    (tmp_path / "f").write_text("x")
    with pytest.raises(downloads.RefusedPath):
        downloads.resolve_dir(str(tmp_path / "f"))


def test_a_destination_that_does_not_exist_yet_is_fine(tmp_path):
    """Checked without touching the disk, so a folder you have named and not used is a
    valid answer. Creating it is `save`'s job, when there is something to put in it."""
    assert downloads.resolve_dir(str(tmp_path / "later")) == tmp_path / "later"
    assert not (tmp_path / "later").exists()


def test_a_malformed_file_is_an_error_not_an_empty_one(tmp_path):
    """`load_keywords`' rule: reading a typo as "no destinations" would quietly send four
    documents somewhere other than where they were told to go."""
    path = tmp_path / "d.yaml"
    path.write_text("resume: [1, 2\n")
    with pytest.raises(ValueError):
        downloads.load_raw(path)

    path.write_text("resume: 5\n")
    with pytest.raises(ValueError, match="must be a directory"):
        downloads.load_raw(path)

    path.write_text("resumee: '/a'\n")
    with pytest.raises(ValueError, match="unknown keys"):
        downloads.load_raw(path)

    path.write_text("resume: 'nope/relative'\n")
    with pytest.raises(ValueError, match="relative"):
        downloads.load_raw(path)


def test_save_creates_the_directory_and_returns_where_it_landed(tmp_path):
    """A destination typed and not yet used is the ordinary case; refusing over it would
    be this feature failing at the only moment it is asked to do anything."""
    target = downloads.save(tmp_path / "new" / "deep", "acme_1.pdf", b"%PDF")
    assert target == tmp_path / "new" / "deep" / "acme_1.pdf"
    assert target.read_bytes() == b"%PDF"


def test_save_replaces_an_earlier_copy_of_the_same_document(tmp_path):
    """Deliberate. The name is minted from the company and the job id, so the only file
    it can land on is an earlier copy of the same document for the same posting — and a
    browser's `file (3).pdf` habit is what makes you attach the wrong one."""
    downloads.save(tmp_path, "acme_1.pdf", b"old")
    downloads.save(tmp_path, "acme_1.pdf", b"new")
    assert (tmp_path / "acme_1.pdf").read_bytes() == b"new"
    assert list(p.name for p in tmp_path.iterdir()) == ["acme_1.pdf"]


def test_the_four_names_are_the_stems_the_builders_already_agree_on():
    """Derived from `tailored_stem` / `letter_stem` rather than composed here, so the
    file on your desk and the file in data/tailored are recognisably the same document.

    Four distinct names, because the kind is what picks the filename as well as the
    folder — two kinds sharing one would overwrite each other in a shared destination."""
    from jobtracker import letter as letter_mod, resume as resume_mod

    stem = resume_mod.tailored_stem("Acme", "1")
    lstem = letter_mod.letter_stem("Acme", "1")
    names = {k: downloads.document_name(k, "Acme", "1") for k in downloads.KINDS}
    assert names == {"resume": f"{stem}.pdf", "letter": f"{lstem}.pdf",
                     "resume_tex": f"{stem}.tex", "letter_tex": f"{lstem}.tex"}
    assert len(set(names.values())) == 4


def test_nothing_from_a_posting_can_reach_a_path_component():
    """`resumes.stored_name` slugs to `[a-z0-9_]`, which is what makes a name safe to
    join onto a directory somebody typed."""
    name = downloads.document_name("resume", "../../etc", "a/b/../c")
    assert "/" not in name and ".." not in name


def test_an_unknown_kind_is_refused_by_both_halves():
    with pytest.raises(downloads.RefusedPath):
        downloads.document_name("passport", "Acme", "1")
    with pytest.raises(downloads.RefusedPath):
        downloads.edit("", "passport", "/tmp")
