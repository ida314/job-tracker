"""Deterministic backend new-grad job-discovery pipeline.

See DESIGN.md for the full architecture. The short version: fetching, parsing,
diffing, and storing are ordinary tested code; the language model is confined to three
bounded roles off the main loop — resolving genuine ambiguity (`resolve`), ranking
against a profile (`rank`), and reading a careers page when a board's slug breaks
(`repair`). In all three it may read, never decide.
"""

import os

__version__ = "0.1.0"


def build_version() -> str:
    """`__version__`, plus the commit it was built from when that is knowable.

    The image build stamps `JOBTRACKER_REVISION` (Dockerfile `ARG GIT_SHA`). Outside a
    container nothing sets it and the bare version is the honest answer — a working-tree
    run has no single commit to name.

    This exists so a deployed run can be identified. Nothing else about a nightly job
    distinguishes "the new image landed" from "the pull silently did not happen": both
    look like a normal 32-second run that exits 0. Reading the revision back out of the
    log or off `service.version` is what turns that into something observable.
    """
    rev = os.environ.get("JOBTRACKER_REVISION", "").strip()
    return f"{__version__}+{rev[:12]}" if rev else __version__
