"""Reading a Maildir. The only module in this repo that opens your mail.

Path in, bytes out — the same split `fetch.py` has with `sources/`. Everything that
decides what a message *means* lives in `mail.py` and is pure, so the interesting half is
testable without a mailbox at all.

**This never writes.** Not a flag, not a rename, not a directory. Your mail client owns
that store, and a tracker that moved a message between `new/` and `cur/`, or set a Seen
flag, would be a bug you debug in aerc rather than here. The read is `keys()` +
`get_bytes()` and nothing else.
"""

from __future__ import annotations

import logging
import mailbox
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger("jobtracker.maildir")

# What makes a directory a Maildir. `tmp/` exists too, but it holds messages mid-delivery
# and is nobody else's business to read.
MAILDIR_SUBDIRS = ("cur", "new")


@dataclass(frozen=True)
class MailRead:
    """One message off disk, or the failure to read it.

    One type with an `error` field, the same shape `models.FetchResult` uses, because an
    unreadable message has to be *counted*. Skipping it silently would let a quarantined
    file or a truncated spool turn into "no new mail today", which is §7.3 exactly.
    """

    key: str            # the maildir filename — for the log only, never an identity
    folder: str         # 'cur' | 'new'
    raw: bytes = b""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def is_maildir(path: Optional[Path]) -> bool:
    """Does this look like a Maildir? Checked before opening, never after."""
    if path is None:
        return False
    path = Path(path)
    return all((path / sub).is_dir() for sub in MAILDIR_SUBDIRS)


def read_messages(path: Path, limit: Optional[int] = None) -> Iterator[MailRead]:
    """Every message in `cur/` and `new/`, as raw bytes.

    `create=False` is the load-bearing argument. `mailbox.Maildir` defaults it to **True**,
    so a typo'd `$JOBTRACKER_MAILDIR` would silently *create* directories inside — or
    beside — your mail store. It is the most dangerous default in this subsystem.

    `factory=None` returns bytes rather than a `MaildirMessage`, which carries mutable
    subdir and info state we want nowhere near this.
    """
    path = Path(path)
    box = mailbox.Maildir(str(path), factory=None, create=False)
    seen = 0
    for key in box.keys():
        if limit is not None and seen >= limit:
            return
        seen += 1
        folder = "cur" if ":" in key or (path / "cur" / key).exists() else "new"
        try:
            yield MailRead(key=key, folder=folder, raw=box.get_bytes(key))
        except (OSError, KeyError, ValueError) as exc:
            # One quarantined file must not hide the other four thousand. The caller
            # counts these and reports the run degraded.
            log.warning("could not read message %s: %s", key, exc)
            yield MailRead(key=key, folder=folder, error=str(exc))
