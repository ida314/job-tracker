"""What you actually sent, frozen at the moment the application was recorded.

`applications` says you applied and `application_events` says what happened next. Neither
says *what went out* — which resume, which cover letter, what you typed into the four
short questions the form asked. This module answers that, and it is the only thing in the
repo that copies a document rather than deriving one.

**It stores the bytes.** A filename would be cheaper and would quietly become a lie:
`tailor build` rewrites a posting's PDF at a deterministic path (`resume.tailored_path`),
so a stored name goes on resolving forever while coming to mean a document you never sent.
Copying is the only form of "this is what I sent" that survives the next rebuild.

Two questions live here, and keeping them in one module is the point — the page that shows
you which resume is in effect and the freeze that records it must not be able to disagree,
which is the rule `resumes.py` was extracted for:

    effective_resume / effective_letter   which document would go out right now
    freeze                                copy those, and write the row

Everything is keyed on `(company, ats_job_id)`, so a manual application — no `postings`
row at all — records exactly as well as a tracked one.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Optional

from . import config, letter as letter_mod, questions, resumes, store

log = logging.getLogger("jobtracker.submissions")

# What `resume_kind` / `letter_kind` may say. The file itself stops saying, once it is a
# copy under a name this module minted, so the row has to.
RESUME_KINDS = ("override", "default")
LETTER_KINDS = ("override", "generated", "default")


def effective_resume(conn: sqlite3.Connection, answers, company: str, ats_job_id: str):
    """`(path, kind)` for the resume that would go out, or `(None, "")`.

    Precedence: this posting's own upload, then the answer bank's `resume:`.

    The **tailored** PDF is deliberately not a term. `tailor build --attach` is what makes
    it this posting's resume, and attaching writes it into `posting_resumes` — so it
    reaches an application through the first branch, after you read the diff, and never
    because a build happened to succeed last night. CLAUDE.md: "Nothing anywhere accepts
    an edit on a click."

    `resumes.override_for` reads a row whose file has gone missing as "no override" and
    logs, rather than raising; that behaviour is load-bearing here too, because this runs
    on the click that records an application.
    """
    override = resumes.override_for(conn, company, ats_job_id)
    if override is not None:
        return override, "override"
    default = getattr(answers, "resume", None)
    if default:
        path = Path(default)
        if path.is_file():
            return path, "default"
        log.warning("answer bank names a resume at %s and it is not there", path)
    return None, ""


def effective_letter(conn: sqlite3.Connection, answers, company: str, ats_job_id: str):
    """`(path, kind)` for the cover letter that would go out, or `(None, "")`.

    Precedence: this posting's own upload, then the letter `coverletter build` compiled
    **if it is still current**, then the answer bank's generic `cover_letter:`.

    Currency matters here in a way it does not for the resume: `cover_letters.written_at`
    moves when the model rewrites the paragraphs, and `letter.is_current` is the existing
    single answer to "is the PDF on disk still the one that row describes". A stale PDF is
    not what you sent — it is what you sent *last time the template changed*.
    """
    row = store.get_posting_letter(conn, company, ats_job_id)
    if row is not None:
        path = resumes.path_for(row["filename"])
        if path.is_file():
            return path, "override"
        log.warning(
            "letter for %s %s is recorded but missing at %s — falling back",
            company, ats_job_id, path,
        )

    built = letter_mod.letter_path(company, ats_job_id)
    if built.is_file():
        written = store.get_letter(conn, company, ats_job_id)
        written_at = written["written_at"] if written is not None else ""
        if letter_mod.is_current(letter_mod.built_day(built), written_at):
            return built, "generated"

    default = getattr(answers, "cover_letter", None)
    if default:
        path = Path(default)
        if path.is_file():
            return path, "default"
    return None, ""


def archive_dir(company: str, ats_job_id: str) -> Path:
    """Where this application's frozen documents live.

    Named from `resumes.stored_name`, the same digest every other per-posting file uses,
    so there is one naming family and not two. A directory rather than a name prefix
    because the two documents inside it are then just `resume.pdf` and `letter.pdf` — a
    folder you can open and understand with no reference to this code.
    """
    stem = resumes.stored_name(company, ats_job_id, "").rstrip(".") or "application"
    return config.SUBMISSIONS_DIR / stem


def _archive(src: Path, dest_dir: Path, name: str) -> Optional[str]:
    """Copy one document into the archive, returning its filename or None.

    Failure is a log and a None, never an exception: this runs inside the click that
    records an application, and a disk error must not lose the application itself. A
    submission that records no resume is a gap you can see; a refused "I applied" is work
    you have to redo.
    """
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / f"{name}{src.suffix.lower()}"
        # Through a temporary + replace, `resumes.write_atomic`'s rule: a reader must
        # never see a half-copied document, least of all this one.
        tmp = out.with_suffix(out.suffix + ".part")
        shutil.copyfile(src, tmp)
        tmp.replace(out)
        return out.name
    except OSError as exc:
        log.warning("could not archive %s: %s", src, exc)
        return None


def freeze(
    conn: sqlite3.Connection,
    company: str,
    ats_job_id: str,
    now: str,
    answers=None,
    *,
    replace: bool = False,
) -> Optional[dict]:
    """Record what went out. Returns the summary written, or None if one already stood.

    **Write-once unless `replace`.** A submission is a fact about a moment; a second
    `applied` finds the row already there and leaves it and its files alone. The row is
    claimed *before* anything is copied, so two writers racing cannot both decide they are
    the first and then overwrite each other's bytes.

    `replace=True` has exactly one caller — the posting page's explicit "Update what I
    submitted" — which exists because `+ tracker` on a table row records an application
    before you have opened the page at all.

    Only saved, non-empty answers are recorded (`questions.answered`). A template default
    rendered on the page and never typed is not something you submitted.
    """
    stored = store.posting_answers(conn, company, ats_job_id)
    payload = questions.answered(stored)

    resume_path, resume_kind = effective_resume(conn, answers, company, ats_job_id)
    letter_path, letter_kind = effective_letter(conn, answers, company, ats_job_id)

    # Claim the row first, with the names the copies WILL have. If the claim loses, we
    # have copied nothing and touched nothing.
    dest = archive_dir(company, ats_job_id)
    resume_name = f"resume{resume_path.suffix.lower()}" if resume_path else None
    letter_name = f"letter{letter_path.suffix.lower()}" if letter_path else None
    won = store.freeze_submission(
        conn, company, ats_job_id, now,
        resume=resume_name, resume_kind=resume_kind,
        letter=letter_name, letter_kind=letter_kind,
        answers=json.dumps(payload), replace=replace,
    )
    if not won:
        return None

    written_resume = _archive(resume_path, dest, "resume") if resume_path else None
    written_letter = _archive(letter_path, dest, "letter") if letter_path else None

    # A copy that failed must not leave the row claiming a file that is not there — the
    # `posting_resumes` rule ("recorded but missing" is the state to avoid) read forward.
    if written_resume != resume_name or written_letter != letter_name:
        store.freeze_submission(
            conn, company, ats_job_id, now,
            resume=written_resume, resume_kind=resume_kind if written_resume else "",
            letter=written_letter, letter_kind=letter_kind if written_letter else "",
            answers=json.dumps(payload), replace=True,
        )

    log.info(
        "submission %s/%s: resume=%s letter=%s answers=%d",
        company, ats_job_id, written_resume or "-", written_letter or "-", len(payload),
    )
    return {
        "resume": written_resume, "resume_kind": resume_kind if written_resume else "",
        "letter": written_letter, "letter_kind": letter_kind if written_letter else "",
        "answers": len(payload),
    }


def archived_path(row, kind: str) -> Optional[Path]:
    """Where one frozen document sits, from a submission row, or None.

    The single derivation the download route and the page both use — a second copy of this
    expression is how a link and a label come to mean different files.
    """
    if row is None or kind not in ("resume", "letter"):
        return None
    try:
        name = row[kind]
    except (IndexError, KeyError):
        return None
    if not name:
        return None
    return archive_dir(row["company"], row["ats_job_id"]) / name
