"""Anything else with a link in it.

The honest floor: pull out the first URL, take the first line as the role, date it by
when the message was posted. It never guesses an employer, because a guessed employer
becomes a company name in a tracker whose whole discipline is that identity is verified
rather than inferred — the `ashby/cedar` rule, one layer out.
"""

from __future__ import annotations

import re
from typing import Optional

from ....sources.base import iso_day
from .base import Format, FormatContext, ParsedJob, register

_URL = re.compile(r"https?://[^\s<>()\[\]{}\"']+")
# A markdown link is unwrapped to its label rather than having its URL cut out from
# under it: `[Backend Engineer](<https://...>)` must read as "Backend Engineer", not as
# the punctuation left over once the URL is gone.
_MD_LINK = re.compile(r"\[([^\]]+)\]\(\s*<?[^)]*>?\s*\)")
_DECORATION = re.compile(r"[`*_#>]+")
MAX_ROLE = 200


class Generic(Format):
    name = "generic"
    fallback = True

    def matches(self, message: dict, text: str) -> bool:
        return True

    def parse(self, message: dict, ctx: FormatContext) -> Optional[ParsedJob]:
        url = ""
        found = _URL.search(ctx.text)
        if found:
            url = found.group(0).rstrip(".,;:!?").strip("<>")

        role = ""
        for line in ctx.text.splitlines():
            # Strip the URL out before taking the line as a title, or a bare pasted link
            # becomes its own job title.
            unwrapped = _MD_LINK.sub(r"\1", line)
            candidate = re.sub(
                r"\s+", " ", _DECORATION.sub("", _URL.sub("", unwrapped))
            ).strip()
            candidate = candidate.strip("[]()<> -|")
            if candidate:
                role = candidate[:MAX_ROLE]
                break
        if not role:
            return None

        stamp = str(message.get("timestamp") or "")
        return ParsedJob(
            role=role,
            employer="",  # never guessed — see the module docstring
            url=url,
            posted_at=stamp or None,
            posted_on=iso_day(stamp),
            format=self.name,
        )


register(Generic())
