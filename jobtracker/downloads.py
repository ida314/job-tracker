"""Where a built document goes when you save it.

Four destinations, one per kind of document this repo compiles for a posting: the
tailored resume and the cover letter, and the LaTeX each of those two is compiled
from. Every one defaults to `~/Downloads` and every one is set independently, because
the PDF you attach to a form and the `.tex` you keep under version control do not
belong in the same folder.

**This exists because a web page cannot direct a browser download.** The `↓` and `✉`
used to be `<a download>` links, so the file landed wherever the browser had been told
to put things and this repo had no say in it; the `tex` buttons went to the clipboard,
which is not a place at all. `serve` runs on your machine as you, so the honest way to
put a document at a path you chose is for the server to write it there and say where it
went. That is what `server._api_download` does and this module is the half that decides
*where*.

**Curation, not observation.** `downloads.yaml` is written by the Settings page on a
click you made — the same standing `keywords.yaml` and `plugins.yaml` have, and
DESIGN.md §2.3 is intact: no scheduled run touches it. The writer is line-oriented text
surgery for `keywords.edit`'s reason — the file is mostly the comments explaining what
the four kinds are, and `yaml.safe_dump` deletes all of them.

**Precedence is file, then environment, then `~/Downloads`.** The file wins because it
is the thing you typed on a page you opened, and a setting the page cannot change is
one the page should not be showing you. The environment variables are underneath it so
that a deployment where `~/Downloads` is meaningless — a container, a service account —
has a default worth having without anyone having to click four times.

**A path is stored as you wrote it**, `~` and all. Expanding before the write would bake
`/home/dylan` into a curated file, and the expansion is a question about the machine
reading it, not about the decision you made.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("jobtracker.downloads")

# Ordered, and the order is the one the Settings card renders in: the two documents
# first, then the two sources. A tuple rather than a set — this is a list somebody reads
# down, not a membership test that happens to be iterable.
KINDS = ("resume", "letter", "resume_tex", "letter_tex")

LABELS = {
    "resume": "Tailored resume (PDF)",
    "letter": "Cover letter (PDF)",
    "resume_tex": "Tailored resume (LaTeX source)",
    "letter_tex": "Cover letter (LaTeX source)",
}

# Read at the point of use rather than at import, `config.prefill_off`'s reason: it is
# what lets a test set one of these with `monkeypatch.setenv` and never depend on which
# module imported which first.
ENV = {
    "resume": "JOBTRACKER_DOWNLOAD_RESUME",
    "letter": "JOBTRACKER_DOWNLOAD_LETTER",
    "resume_tex": "JOBTRACKER_DOWNLOAD_RESUME_TEX",
    "letter_tex": "JOBTRACKER_DOWNLOAD_LETTER_TEX",
}

DEFAULT = "~/Downloads"


class RefusedPath(ValueError):
    """The directory was not one this can write to, so nothing was written."""


def default_raw(kind: str) -> str:
    """What `kind` falls back to with nothing in the file: the environment, or
    `~/Downloads`.

    Returned unexpanded, like everything else here, so the Settings card can show you
    the same string the file would hold.
    """
    return (os.environ.get(ENV.get(kind, ""), "") or "").strip() or DEFAULT


def resolve_dir(raw: str) -> Path:
    """Expand and check one directory, or raise `RefusedPath` naming what is wrong.

    Absolute after expansion, because a relative path means "wherever `serve` happens to
    have been started from", which is a different directory depending on how you
    launched it — the one property a destination must not have.

    `normpath` rather than `resolve`: it collapses `..` without following symlinks and
    without touching the disk, so a directory that does not exist yet is still a valid
    answer. Creating it is `save`'s job, at the moment there is something to put in it.
    """
    text = str(raw or "").strip()
    if not text:
        raise RefusedPath("a destination cannot be empty")
    if "\x00" in text:
        raise RefusedPath("a destination cannot contain a null byte")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise RefusedPath(
            f"{text!r} is relative — a destination has to be an absolute path, or start "
            f"with ~"
        )
    path = Path(os.path.normpath(path))
    if path.exists() and not path.is_dir():
        raise RefusedPath(f"{path} is a file, not a directory")
    return path


def load_raw(path: Optional[str | Path] = None, fill: bool = True) -> dict:
    """The destinations as written, `{kind: text}`.

    A missing file is an empty one — this is a file you grow by clicking, so a fresh
    checkout has none, and that is a normal state rather than an error. A *malformed* one
    is an error, `load_keywords`' rule: reading a typo as "no destinations" would quietly
    send four documents somewhere other than where you said.

    `fill` is what separates the two questions this answers. Filled — the default, and
    what every reader that is about to write a file wants — every kind is present, backed
    by the environment and then `~/Downloads`. Unfilled it holds **only what the file
    names**, which is the question the Settings card asks: a fallback rendered into a
    text box reads as a decision somebody made, and `expected_board_name` is the rule
    that must not happen.
    """
    out = {kind: default_raw(kind) for kind in KINDS} if fill else {}
    path = Path(path) if path is not None else None
    if path is None or not path.exists():
        return out

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: not valid YAML — {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: expected a top-level mapping, got {type(data).__name__}"
        )
    unknown = sorted(set(data) - set(KINDS))
    if unknown:
        raise ValueError(f"{path}: unknown keys: {unknown} (known: {list(KINDS)})")

    for kind in KINDS:
        if kind not in data or data[kind] is None:
            continue
        value = data[kind]
        # `isinstance(True, int)` is True, and a bare `~` in YAML is None — already
        # skipped above. Anything else that is not a string is a typo worth naming.
        if isinstance(value, bool) or not isinstance(value, str):
            raise ValueError(
                f"{path}: '{kind}' must be a directory, got {type(value).__name__}"
            )
        try:
            resolve_dir(value)
        except RefusedPath as exc:
            raise ValueError(f"{path}: '{kind}': {exc}") from exc
        out[kind] = value.strip()
    return out


def load_downloads(path: Optional[str | Path] = None) -> dict:
    """`load_raw` with every value expanded — `{kind: Path}`.

    The loader `safewrite.write_text` validates a candidate with, so the check before a
    write and the read at use are provably the same code.
    """
    return {kind: resolve_dir(raw) for kind, raw in load_raw(path).items()}


_HEADER = """\
# Where a saved document goes. See docs/downloads.md.
#
# Curation: human-authored, and written by the Settings page on a click you made.
#
# Four kinds, set independently — the PDF you attach to an employer's form and the
# LaTeX you keep under version control do not belong in the same folder:
#
#   resume      the tailored resume, compiled       (the actions cell's ↓)
#   letter      the cover letter, compiled          (the actions cell's ✉)
#   resume_tex  the LaTeX the tailored resume is compiled from   (tex)
#   letter_tex  the LaTeX the cover letter is compiled from      (tex✉)
#
# Written as you typed it, `~` and all; it is expanded when a file is saved. A path has
# to be absolute (or start with ~) and must not name an existing file. The directory is
# created the first time something is written into it.
#
# Anything absent here falls back to $JOBTRACKER_DOWNLOAD_RESUME and friends, and then
# to ~/Downloads.

"""


def render(raw: dict) -> str:
    """The whole file, from scratch. Only used to create one — see `edit`."""
    out = [_HEADER]
    for kind in KINDS:
        out.append(f"{kind}: {_scalar(raw.get(kind) or DEFAULT)}\n")
    return "".join(out)


def _scalar(value: str) -> str:
    """One path as a YAML scalar, always single-quoted.

    Quoted unconditionally rather than only when it has to be: a path can hold a `#`, a
    `:` or a leading space, each of which changes what the line means, and a rule with
    three exceptions is one that will be applied with two.
    """
    return "'" + str(value).replace("'", "''") + "'"


def edit(text: str, kind: str, raw: str) -> str:
    """Set one destination in `text`, touching no other line.

    Text surgery rather than a YAML round trip, `keywords.edit`'s rule: this file is
    mostly the header explaining what the four kinds are and where they fall back to,
    and `yaml.safe_dump` deletes every line of it.

    A kind the file does not name yet is appended rather than rewritten in place — an
    older file, or one somebody trimmed by hand, is still a file whose comments are
    worth keeping.
    """
    if kind not in KINDS:
        raise RefusedPath(f"unknown document {kind!r}")
    resolve_dir(raw)
    value = str(raw).strip()

    if not text.strip():
        return render({**{k: default_raw(k) for k in KINDS}, kind: value})

    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if _key_of(line) != kind:
            continue
        newline = "\n" if line.endswith("\n") else ""
        lines[i] = f"{kind}: {_scalar(value)}{newline}"
        return "".join(lines)

    body = "".join(lines)
    if not body.endswith("\n"):
        body += "\n"
    return body + f"{kind}: {_scalar(value)}\n"


def _key_of(line: str) -> Optional[str]:
    """The top-level key a line sets, or None.

    Column 0 only. An indented `resume:` belongs to something else — there is nothing
    nested in this file today, and a matcher that ignores indentation is how that stops
    being true by accident.
    """
    if not line or line[0].isspace() or line.lstrip().startswith("#"):
        return None
    key, sep, _rest = line.partition(":")
    if not sep:
        return None
    return key.strip() or None


def document_name(kind: str, company: str, ats_job_id: str) -> str:
    """What the saved file is called — the same basename it has on this machine.

    The two PDFs keep the name `tailor build` and `coverletter build` gave them, and the
    two sources take that stem with a `.tex`. Derived from `tailored_stem` /
    `letter_stem` rather than composed here, so the file on your desk and the file in
    `data/tailored` are recognisably the same document — and because those are the single
    derivations four other callers already agree on.

    Both stems run the company and the job id through `resumes.stored_name`, which slugs
    to `[a-z0-9_]`. That is what makes this safe to join onto a directory you typed:
    nothing from a posting can contribute a separator or a `..`.
    """
    from . import letter as letter_mod, resume as resume_mod

    if kind == "resume":
        return resume_mod.tailored_path(company, ats_job_id).name
    if kind == "letter":
        return letter_mod.letter_path(company, ats_job_id).name
    if kind == "resume_tex":
        return resume_mod.tailored_stem(company, ats_job_id) + ".tex"
    if kind == "letter_tex":
        return letter_mod.letter_stem(company, ats_job_id) + ".tex"
    raise RefusedPath(f"unknown document {kind!r}")


def save(directory: Path, name: str, data: bytes) -> Path:
    """Write one document into `directory` and return where it landed.

    The directory is created if it is not there: a destination you typed and have not
    used yet is the ordinary case, and refusing over it would be this feature failing at
    the only moment it is asked to do anything.

    Overwriting is deliberate. `name` is minted by `resumes.stored_name` from the company
    and the job id, so the only file it can ever land on is an earlier copy of the same
    document for the same posting — and a browser's `file (3).pdf` habit is exactly the
    thing that makes you attach the wrong one.
    """
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RefusedPath(f"could not create {directory}: {exc}") from exc
    target = directory / name
    try:
        target.write_bytes(data)
    except OSError as exc:
        raise RefusedPath(f"could not write {target}: {exc}") from exc
    log.info("saved %s", target)
    return target
