"""Deterministic backend new-grad job-discovery pipeline.

See DESIGN.md for the full architecture. The short version: fetching, parsing,
diffing, and storing are ordinary tested code; the language model is confined to five
bounded roles off the main loop (DESIGN.md §8) — resolving genuine ambiguity
(`resolve`), reading a careers page when a board's slug breaks (`repair`), ranking
against a profile (`rank`), matching a form's questions to answers you already wrote
(`prefill`), and reading an employer's reply for the stage it reports (`inbox`). In all
five it may read and propose, never decide.
"""

import os

# Keep in step with `version` in pyproject.toml — test_version_matches_pyproject holds
# them together, because two numbers that can disagree eventually do, and the whole
# point of this string is that you can trust it.
#
# 0.2.0 (2026-08-16) — applications: the outer loop got a page, a history and manual
# entry. Minor rather than patch because it adds a surface and a table, and because a
# version that never moves cannot answer "is this the build with the new thing in it".
#
# 0.3.0 (2026-08-16) — the mailbox became a source of proposals (`mail` + the `inbox`
# task, the last bounded model role), a posting can carry its own resume and be
# re-planned from the page, the postings tables group by company, Today opens past its
# three, and the answer gaps sort by how many employers ask. Three new tables.
__version__ = "0.3.0"


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
