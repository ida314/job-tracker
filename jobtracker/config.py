"""Paths and loaders. The one place filesystem locations are resolved.

DB path comes from $JOBTRACKER_DB so the container can point it at a mounted volume
(/data/state.db) while local dev uses ./data/state.db. Curated inputs (companies.yaml,
criteria.yaml) live next to the package root and are read-only at runtime.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .models import Company

# Repo root = parent of the jobtracker/ package directory.
ROOT = Path(__file__).resolve().parent.parent

COMPANIES_YAML = Path(os.environ.get("JOBTRACKER_COMPANIES", ROOT / "companies.yaml"))
CRITERIA_YAML = Path(os.environ.get("JOBTRACKER_CRITERIA", ROOT / "criteria.yaml"))
PROFILE_YAML = Path(os.environ.get("JOBTRACKER_PROFILE", ROOT / "profile.yaml"))

# Curated like the three above, but gitignored: it holds your name, email, phone, and
# whatever else an application form asks for. `answers.example.yaml` is the tracked
# file that documents its shape. Absent is a normal state — prefill reports itself
# unavailable and nothing else notices.
ANSWERS_YAML = Path(os.environ.get("JOBTRACKER_ANSWERS", ROOT / "answers.yaml"))

# Where the browser keeps its profile between runs. Persistent so that candidate-account
# logins survive, which is the only thing that could ever make the `manual` Workday
# companies tractable. Gitignored, and it is not used for prefill state — see
# docs/prefill.md on why a cookie cannot carry that.
BROWSER_PROFILE = Path(
    os.environ.get("JOBTRACKER_BROWSER_PROFILE", ROOT / "data" / "browser")
)

# Where to watch the browser the button opens, when it opens somewhere you cannot see.
# Playwright drives a browser on the machine running `serve`, so on a headless host the
# window exists and has no screen; pointing this at a remote-desktop viewer for that
# host's display puts it back in front of you. Empty means the window is local and needs
# no link. The app neither starts nor knows anything about the viewer — it is a URL.
BROWSER_VIEW_URL = os.environ.get("JOBTRACKER_BROWSER_VIEW_URL", "")

# Resumes tailored to one posting each. The answer bank's `resume:` is the default and
# lives beside answers.yaml; these are the exceptions, uploaded from the browser and
# stored under a name this repo minted. Gitignored with the rest of ./data.
RESUMES_DIR = Path(os.environ.get("JOBTRACKER_RESUMES", ROOT / "data" / "resumes"))

# Your job-search mailbox, read only. Empty means not configured, which is a normal
# state: `mail` says so and the `inbox` task reports itself unavailable rather than idle.
# Never written, never marked read, never moved — your mail client owns that directory.
# See docs/mail.md.
_MAILDIR = os.environ.get("JOBTRACKER_MAILDIR", "").strip()
MAILDIR = Path(_MAILDIR) if _MAILDIR else None

_DEFAULT_DB = ROOT / "data" / "state.db"
DB_PATH = Path(os.environ.get("JOBTRACKER_DB", _DEFAULT_DB))


def load_companies(path: str | Path | None = None) -> list[Company]:
    """Parse companies.yaml into Company objects. Fails loudly on a malformed entry."""
    path = Path(path) if path is not None else COMPANIES_YAML
    if not path.exists():
        raise FileNotFoundError(
            f"companies file not found: {path} — run `jobtracker migrate` first"
        )

    with path.open() as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a top-level list of companies")

    companies: list[Company] = []
    seen: set[str] = set()
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: entry {i} is not a mapping")
        name = entry.get("name")
        ats = entry.get("ats")
        if not name or not ats:
            raise ValueError(f"{path}: entry {i} missing required name/ats")
        if name in seen:
            raise ValueError(f"{path}: duplicate company name {name!r}")
        seen.add(name)
        companies.append(
            Company(
                name=str(name),
                ats=str(ats),
                slug=str(entry.get("slug") or ""),
                tier=entry.get("tier"),
                category=str(entry.get("category") or ""),
                check_method=str(entry.get("check_method") or "manual"),
                expected_board_name=(
                    str(entry["expected_board_name"])
                    if entry.get("expected_board_name")
                    else None
                ),
                careers_page=str(entry.get("careers_page") or ""),
                board_url=str(entry.get("board_url") or ""),
                notes=str(entry.get("notes") or ""),
            )
        )
    return companies


def ensure_data_dir() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
