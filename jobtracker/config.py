"""Paths and loaders. The one place filesystem locations are resolved.

DB path comes from $JOBTRACKER_DB so the container can point it at a mounted volume
(/data/state.db) while local dev uses ./data/state.db. Curated inputs (companies.yaml,
criteria.yaml) live next to the package root. They are never written by a *scheduled*
run — that separation is DESIGN.md §2.3 — but they are not read-only: `serve` edits
criteria.yaml from /tuning and appends to companies.yaml from /companies, both through
`safewrite`, and both on a click somebody made.
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
# host's display (noVNC, xpra, …) puts it back in front of you. Empty means the window is
# local, or that you have no viewer, and the link is simply not rendered. The app neither
# starts, probes nor knows anything about the viewer — it is a URL, `_safe_url`-checked
# like every other third-party href these pages render.
#
# This was deleted on 2026-08-22 and is back, narrower, since 2026-08-29. Deleting it was
# right about the main flow and wrong about the last resort: `/apply` mirrors the fields
# the discovery pass could read, and the honest answer for everything else — a captcha, a
# widget that will not take a value however it is written, a section that only renders
# once something is clicked — used to be "open the window", with nothing anywhere that
# opened it. A video stream is a bad way to type fifteen fields and the only way to reach
# a form that will not be typed into at all. So it is on `/apply` alone, beside the
# preview it is the fallback for, and it is not on the dashboard: nothing there has a
# window open yet.
BROWSER_VIEW_URL = os.environ.get("JOBTRACKER_BROWSER_VIEW_URL", "")

# Resumes tailored to one posting each. The answer bank's `resume:` is the default and
# lives beside answers.yaml; these are the exceptions, uploaded from the browser and
# stored under a name this repo minted. Gitignored with the rest of ./data.
RESUMES_DIR = Path(os.environ.get("JOBTRACKER_RESUMES", ROOT / "data" / "resumes"))

# Your resume's *source*, as LaTeX. Curated and personal, so it sits beside answers.yaml
# rather than under ./data — this is a file you wrote and the pipeline only ever reads.
#
# LaTeX rather than the PDF because `tailor` has to read the resume as text, and there is
# no text extractor in this repo. A .tex file is already text: no PDF parser, no dependency
# decision, and — the part that matters more — the model quotes lines back and a PDF
# extractor's idea of a line is a column-mangling accident. It also makes the tailored
# output a diff you can read. Absent is a normal state: `tailor` reports itself unavailable.
RESUME_TEX = Path(os.environ.get("JOBTRACKER_RESUME_TEX", ROOT / "resume.tex"))

# Where a tailored resume is assembled. Generated state, so it goes under ./data with the
# uploads — nothing here is authored by hand and deleting it costs a rebuild, not a file.
TAILORED_DIR = Path(os.environ.get("JOBTRACKER_TAILORED", ROOT / "data" / "tailored"))

# Your job-search mailbox, read only. Empty means not configured, which is a normal
# state: `mail` says so and the `inbox` task reports itself unavailable rather than idle.
# Never written, never marked read, never moved — your mail client owns that directory.
# See docs/mail.md.
_MAILDIR = os.environ.get("JOBTRACKER_MAILDIR", "").strip()
MAILDIR = Path(_MAILDIR) if _MAILDIR else None

# Import plugins: which feeds are switched on, and how each is pointed at its source.
# Curation, like companies.yaml — every writer is a command you typed. Absent means no
# plugin is configured, which is a normal state and never an error. See docs/plugins.md.
PLUGINS_YAML = Path(os.environ.get("JOBTRACKER_PLUGINS", ROOT / "plugins.yaml"))

# The Discord bot token, and this repo's first credential. Five rules come with it, and
# every one of them is a place it could leak from:
#
#   * Env only, never plugins.yaml. Gitignored is not the same protection as never on
#     disk; a config file gets `cat`ed into a terminal and pasted into an issue.
#   * Never a build ARG — `docker history` on a published image reads those back, which
#     is already recorded here about the sir-client install.
#   * Never in a log line's `extra={}`: the JSON formatter promotes those to top-level
#     keys, so it would land in structured logs.
#   * Never a span attribute, and therefore **never a query parameter** — `_request`
#     records `url.full` on every request and logs the URL on every retry. Header only.
#   * Never echoed by `plugins list`, not even a prefix.
#
# Empty means not configured — the MAILDIR rule — and the plugin reports itself
# unavailable rather than idle, which is a different state from "nothing to do".
DISCORD_TOKEN = os.environ.get("JOBTRACKER_DISCORD_TOKEN", "").strip() or None

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
