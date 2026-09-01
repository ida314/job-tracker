"""How one bot's message becomes one posting.

A jobs channel carries one bot's house style, and the next channel carries a different
one. So parsing is a small registry of its own, the same shape as `sources/` and
`tasks/`: a format is one file plus one import line, and it never touches the network.

A format may return None for a message it cannot read. That is not a failure — it is how
a message falls through to the next format, and finally to `generic`, which reads
anything with a link in it. Formats are written not to raise, and the dispatcher catches
anyway: "never raises" is a promise one malformed date can break inside `strptime`, and
the cost of believing it would be a poll that dies half way through a channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ParsedJob:
    """What a format managed to read. Everything but `role` is optional.

    Two date fields, mirroring `Posting` and for the same reason: `posted_at` is the
    vendor's text exactly as written and is never compared, `posted_on` is a plain ISO
    day and is the only one anything may sort on.
    """

    role: str
    employer: str = ""
    url: str = ""
    location: str = ""
    sponsorship: str = ""
    posted_at: Optional[str] = None
    posted_on: Optional[str] = None
    format: str = ""


@dataclass(frozen=True)
class FormatContext:
    """Everything a format may look at besides the message itself.

    `today` is a parameter rather than a clock read, because a format is pure — the same
    discipline `Source.normalize_posted_at(raw, today)` follows, and for the same reason:
    a function that reads the clock cannot be tested against a recorded payload.
    """

    channel_label: str
    guild_id: str
    channel_id: str
    permalink: str
    today: str
    text: str


class Format:
    name: str = ""
    # The fallback sorts last regardless of import order. Relying on the order of lines
    # in `__init__.py` to keep `generic` at the end is exactly the kind of silent,
    # action-at-a-distance breakage this codebase is built to avoid, and declaring it
    # costs one attribute.
    fallback: bool = False

    def matches(self, message: dict, text: str) -> bool:
        """A cheap shape test. Let `parse` return None for anything deeper."""
        return False

    def parse(self, message: dict, ctx: FormatContext) -> Optional[ParsedJob]:
        raise NotImplementedError


_FORMATS: list = []


def register(fmt: Format) -> Format:
    _FORMATS.append(fmt)
    return fmt


def formats() -> list:
    """Registered order, with any fallback last."""
    return sorted(_FORMATS, key=lambda f: (f.fallback,))
