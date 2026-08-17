"""Resume files: validating an upload, naming it, and finding the right one.

Two resumes exist for any application. The answer bank's `resume:` is the default and
lives beside answers.yaml; a posting may override it with its own. This module owns both
the bytes-on-disk half and the "which file does this posting actually attach" question,
so that the CLI and the web page cannot answer it differently — an application goes out
under your name, and the two paths disagreeing about which PDF went with it is not a
thing you could discover afterwards.

Extracted from `server._api_resume`, which had all of it inline, for the same reason
`safewrite.py` was: a second caller appeared, and validation that lives in one endpoint
is validation the other endpoint does not have.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import re
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Optional

from . import config, store

log = logging.getLogger("jobtracker.resumes")

# The resume upload is base64 inside a JSON body — so the wire cost is ~4/3 of the file.
# 6 MB of body is comfortably over any real resume and still bounded.
MAX_UPLOAD = 6 * 1024 * 1024

# What a resume may be. An allowlist rather than a blocklist, and checked before anything
# reaches disk: these files are written into a directory the pipeline reads on every run.
RESUME_TYPES = {
    ".pdf": b"%PDF-",
    # DOCX is a zip. `PK\x03\x04` is the local file header every one starts with.
    ".docx": b"PK\x03\x04",
}


class RefusedUpload(Exception):
    """Why the upload was not stored, phrased for the person who tried it."""


def validate_upload(filename: str, content_b64: str) -> tuple[bytes, str]:
    """(bytes, suffix) for an upload that may be written, or raise RefusedUpload.

    Order is load-bearing. The suffix is checked *first*, so `resume.exe` is told it is
    not a resume rather than that its base64 could not be decoded — the second message is
    true of neither the file nor the problem. Nothing here touches the disk: a refusal
    must leave no partial file behind for the next run to find.
    """
    filename = str(filename or "").strip()
    b64 = str(content_b64 or "")
    if not filename or not b64:
        raise RefusedUpload("no file")

    # The extension decides the name we write, so it can never come from the client's
    # string — a filename is attacker-shaped input even when the attacker is you with a
    # badly named file. Only the suffix is read, and only from an allowlist.
    suffix = Path(filename).suffix.lower()
    if suffix not in RESUME_TYPES:
        raise RefusedUpload(
            f"{suffix or 'that'} is not a resume; expected "
            f"{' or '.join(sorted(RESUME_TYPES))}"
        )
    try:
        blob = base64.b64decode(b64, validate=True)
    except (ValueError, binascii.Error):
        raise RefusedUpload("could not decode the upload") from None
    if not blob:
        raise RefusedUpload("the file is empty")
    if len(blob) > MAX_UPLOAD:
        raise RefusedUpload(
            f"{len(blob) // 1024} KB is over the {MAX_UPLOAD // (1024 * 1024)} MB limit"
        )
    if not blob.startswith(RESUME_TYPES[suffix]):
        # Content, not just the name. A .docx that is really something else would be
        # attached to a job application and read by a person.
        raise RefusedUpload(f"that is not really a {suffix} file")
    return blob, suffix


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_") or "x"


def stored_name(company: str, ats_job_id: str, suffix: str) -> str:
    """The filename a posting's resume is written under.

    `<company>_<job>_<digest8><suffix>`, all of it minted here. The slug reduces to
    `[a-z0-9_]`, so no separator, dot or `..` survives from a company name — and the
    digest of the exact pair is what stops two postings whose names slug alike from
    silently overwriting each other's resume.
    """
    digest = hashlib.sha1(f"{company}\x00{ats_job_id}".encode()).hexdigest()[:8]
    return f"{_slug(company)}_{_slug(ats_job_id)}_{digest}{suffix}"


def path_for(filename: str) -> Path:
    return config.RESUMES_DIR / filename


def write_atomic(path: Path, blob: bytes) -> None:
    """Write via `.part` + rename, so a reader never sees a half-written resume."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(blob)
    os.replace(tmp, path)


def override_for(
    conn: sqlite3.Connection, company: str, ats_job_id: str
) -> Optional[Path]:
    """This posting's own resume, or None.

    A row whose file has gone missing reads as None and logs. Raising here would surface
    with a browser already sitting on an open application form, which docs/prefill.md
    names as the worst possible moment to discover a missing resume.
    """
    row = store.get_posting_resume(conn, company, ats_job_id)
    if row is None:
        return None
    path = path_for(row["filename"])
    if not path.is_file():
        log.warning(
            "posting resume for %s %s is recorded but missing at %s — using the bank's",
            company, ats_job_id, path,
        )
        return None
    return path


def effective(conn: sqlite3.Connection, answers, company: str, ats_job_id: str):
    """(answers, override) — the answer bank as it applies to this one posting.

    `Answers` is a frozen dataclass, so this is a copy: `ctx.answers` is shared across
    every unit of a run and must not be mutated. The copy's `.hash` differs from the
    bank's, and that hash must never be stored — `prefill_plans.answers_hash` records the
    bank's, and `resume_key` records the override. Two questions, two columns.
    """
    override = override_for(conn, company, ats_job_id)
    if override is None or answers is None:
        return answers, None
    return replace(answers, resume=override), override
