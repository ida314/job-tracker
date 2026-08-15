"""Replace a curated YAML file without ever leaving a broken one on disk.

Two files are now edited by the machine on the user's behalf — `criteria.yaml` through
the tuning tab, `answers.yaml` through the Settings tab — and both are files a later run
*loads and depends on*. Writing them in place and validating afterwards would leave the
next run to discover the breakage, which for `criteria.yaml` means an entire nightly
sweep matching against nothing.

So: write a candidate beside the original, parse **the candidate**, keep a `.bak`, and
only then swap it in atomically. If the parse fails, the candidate is removed and the
original was never touched.

This was extracted from `server._api_rule`, which had it inline and correct; the answer
writer needed exactly the same four steps and copying them would have meant two places
to get it right.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Callable

import yaml

log = logging.getLogger("jobtracker.safewrite")


class RefusedWrite(Exception):
    """The candidate did not parse, so nothing was written."""


def write_yaml(path: Path, data: dict, validate: Callable[[Path], object]) -> None:
    """Serialize `data` to `path`, but only if `validate` accepts the result.

    `validate` is handed the *candidate path*, not the parsed data, so it can be the
    same strict loader the application uses at startup — the check and the real read are
    then provably the same code.
    """
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    write_text(path, body, validate)


def write_text(path: Path, body: str, validate: Callable[[Path], object]) -> None:
    """As `write_yaml`, for callers that render the text themselves.

    `answers.yaml` uses this: it carries hand-written comments and commented-out stubs
    for unanswered questions, and a round trip through `yaml.safe_dump` would silently
    delete every one of them.
    """
    path = Path(path)
    candidate = path.with_suffix(path.suffix + ".candidate")
    candidate.write_text(body)
    try:
        validate(candidate)
    except Exception as exc:  # noqa: BLE001 — any loader failure means refuse
        candidate.unlink(missing_ok=True)
        raise RefusedWrite(str(exc)) from exc

    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    candidate.replace(path)
    log.debug("rewrote %s", path)
