"""Read job postings out of one Discord channel.

REST only — `GET /channels/{id}/messages` with a bot token. No gateway, no websocket, no
`discord.py`: this is a nightly batch job reading a backlog, and a persistent connection
would be a second thing that can be down for no gain.

Only messages whose author is a bot are imported. In a jobs channel the humans discuss
and the bot posts, so that one flag is a far cleaner cut than trying to tell a posting
from a comment by its text.

Two failure modes here answer 200 with nothing, and both look exactly like a quiet
channel. They are this vendor's `greenhouse/hubspot` and they are named errors, not
zeroes:

  * **No Read Message History.** The docs are explicit that no messages are returned —
    it is not a 403, it is a 200 and `[]`. Caught by `health.evaluate_plugin`, which
    treats an empty *first* read as suspect.
  * **No MESSAGE CONTENT intent.** Every message arrives, correctly authored, with
    `content`, `embeds` and `attachments` blank. Every format then returns None and the
    channel reports zero jobs while being full of them. Caught by `page_error`.

The naming trap worth knowing: MESSAGE CONTENT is listed as a *privileged gateway
intent*, and this code opens no gateway — but it gates the REST payload all the same.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from ... import config
from ...models import Company, Posting
from ..base import Plugin, register
from .formats import FormatContext, formats

log = logging.getLogger("jobtracker.plugins.discord")

API = "https://discord.com/api/v10"
PAGE = 100
# Snowflakes are milliseconds since 2015-01-01T00:00:00Z in their top 42 bits.
DISCORD_EPOCH_MS = 1420070400000
# Discord's reference mandates this shape for a bot's User-Agent.
USER_AGENT = "DiscordBot (https://github.com/ida314/job-tracker, 0.1)"

_TITLE_MAX = 300


def snowflake_at(day: str, days_back: int = 0) -> str:
    """A synthetic message id sorting exactly where midnight UTC on that day does.

    This is why the first read needs no extra API call and no stored state: a Discord id
    *contains* its own timestamp, so "everything since the 1st" is expressible as an id
    without asking Discord what ids existed then. The `<< 22` low bits are left zero,
    which makes this the lowest id in that millisecond — the right choice for a floor.
    """
    anchor = date.fromisoformat(day) - timedelta(days=max(0, days_back))
    ms = int(
        datetime(anchor.year, anchor.month, anchor.day, tzinfo=timezone.utc).timestamp()
        * 1000
    )
    return str(max(0, ms - DISCORD_EPOCH_MS) << 22)


def day_of(snowflake: str) -> Optional[str]:
    """The ISO day a snowflake encodes, so a stored cursor reads as a date."""
    try:
        value = int(str(snowflake).strip())
    except (TypeError, ValueError):
        return None
    ms = (value >> 22) + DISCORD_EPOCH_MS
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def flatten(message: dict) -> str:
    """The message as one block of text: content, then every embed.

    An embed's title-plus-url is rendered back as `## [title](url)` — a markdown headline
    in another costume — so a format written against markdown keeps working unchanged if
    the bot switches to embeds. That is about fifteen lines, and it removes the whole
    class of "the bot changed shape and the feed silently went to zero".
    """
    parts: list = []
    content = str(message.get("content") or "").strip()
    if content:
        parts.append(content)
    for embed in message.get("embeds") or []:
        if not isinstance(embed, dict):
            continue
        title = str(embed.get("title") or "").strip()
        url = str(embed.get("url") or "").strip()
        if title and url:
            parts.append(f"## [{title}]({url})")
        elif title:
            parts.append(f"## {title}")
        elif url:
            parts.append(url)
        author = (embed.get("author") or {})
        if isinstance(author, dict) and author.get("name"):
            parts.append(f"Company: {author['name']}")
        description = str(embed.get("description") or "").strip()
        if description:
            parts.append(description)
        for field in embed.get("fields") or []:
            if isinstance(field, dict) and field.get("name"):
                parts.append(f"{field['name']}: {field.get('value', '')}")
    return "\n".join(parts).strip()


def parse_message(message: dict, ctx: FormatContext):
    """Run the format registry over one message. Returns a ParsedJob or None."""
    for fmt in formats():
        try:
            if not fmt.matches(message, ctx.text):
                continue
            parsed = fmt.parse(message, ctx)
        except Exception:  # noqa: BLE001 — a regex claim one bad message can break
            log.warning(
                "format %s raised on message %s; falling through",
                fmt.name, message.get("id"),
            )
            continue
        if parsed is not None:
            return parsed
    return None


class Discord(Plugin):
    name = "discord"
    summary = "import job postings a bot announces in one Discord channel"
    page_size = PAGE

    # -- availability ----------------------------------------------------------------
    def unavailable_reason(self, settings: dict) -> Optional[str]:
        if not config.DISCORD_TOKEN:
            return "JOBTRACKER_DISCORD_TOKEN is not set"
        if not settings.get("channel_id"):
            return "no channel_id — run `jobtracker plugins set discord channel_id=<id>`"
        return None

    def company(self, settings: dict) -> Company:
        label = settings.get("label") or settings.get("channel_id") or "channel"
        return Company(
            name=f"Discord: #{label}",
            ats="plugin",
            slug="",
            tier=None,
            category="discord-feed",
            check_method="plugin",
            expected_board_name=None,
            notes="Imported by the discord plugin. Not curation — see docs/plugins.md.",
        )

    # -- paging ----------------------------------------------------------------------
    def first_cursor(self, settings: dict, today: str) -> str:
        return snowflake_at(today, int(settings.get("backfill_days") or 0))

    def page_url(self, settings: dict, after: str) -> str:
        return (
            f"{API}/channels/{settings['channel_id']}/messages"
            f"?limit={PAGE}&after={after}"
        )

    def auth_headers(self) -> dict:
        # Header, never a query parameter: `_request` records `url.full` as a span
        # attribute and logs the URL on every retry.
        return {
            "Authorization": f"Bot {config.DISCORD_TOKEN}",
            "User-Agent": USER_AGENT,
        }

    def page_error(self, raw: object) -> Optional[str]:
        """Distinguish "nothing new" from "that was not an answer".

        Discord returns a bare list for a page and a `{"message":…, "code":…}` object for
        an error, so the list check *is* the distinction. Getting this wrong is how a
        broken read advances the cursor past messages nothing will ever see again.
        """
        if isinstance(raw, dict):
            return f"discord error: {raw.get('message') or raw}"
        if not isinstance(raw, list):
            return f"unexpected payload shape: {type(raw).__name__}"
        if raw and all(
            not (m.get("content") or "").strip() and not m.get("embeds")
            for m in raw
            if isinstance(m, dict)
        ):
            # One empty message is ordinary (an attachment-only post). A whole page of
            # them is a configuration fact, and reporting it as zero jobs would be the
            # most expensive silence this plugin could produce.
            return (
                "every message on this page has empty content — the MESSAGE CONTENT "
                "intent is probably off (Developer Portal -> Bot -> Privileged Gateway "
                "Intents); it gates the REST payload even though nothing here uses the "
                "gateway"
            )
        return None

    def page_ids(self, raw: object) -> list:
        if not isinstance(raw, list):
            return []
        return [str(m.get("id")) for m in raw if isinstance(m, dict) and m.get("id")]

    def page_cursor(self, raw: object) -> Optional[str]:
        """The highest id on the page, read off the raw payload.

        Not derived from the postings: a page of forty human messages produces none, and
        a cursor that did not move would re-read those forty every night forever.
        """
        ids = [int(i) for i in self.page_ids(raw) if str(i).isdigit()]
        return str(max(ids)) if ids else None

    # -- parsing ---------------------------------------------------------------------
    def parse_page(self, group: str, raw: object, settings: dict, today: str):
        if not isinstance(raw, list):
            return [], 0, 0

        postings: list = []
        unparsed = skipped = 0
        # Oldest first. Discord answers newest-first even when paging forward with
        # `after`, so the page is reversed to read in the order things were said.
        for message in sorted(
            (m for m in raw if isinstance(m, dict)),
            key=lambda m: int(m["id"]) if str(m.get("id", "")).isdigit() else 0,
        ):
            author = message.get("author") or {}
            if not (isinstance(author, dict) and author.get("bot")):
                # Humans chat here; the bot posts. Skipped, not unparsed — this is a
                # message we understood and declined, and the counts say different things.
                skipped += 1
                continue

            text = flatten(message)
            message_id = str(message.get("id") or "")
            permalink = (
                f"https://discord.com/channels/"
                f"{settings.get('guild_id') or '@me'}/"
                f"{settings.get('channel_id', '')}/{message_id}"
            )
            ctx = FormatContext(
                channel_label=group,
                guild_id=str(settings.get("guild_id") or ""),
                channel_id=str(settings.get("channel_id") or ""),
                permalink=permalink,
                today=today,
                text=text,
            )
            parsed = parse_message(message, ctx)
            if parsed is None or not parsed.role:
                unparsed += 1
                continue

            title = (
                f"{parsed.employer} — {parsed.role}" if parsed.employer else parsed.role
            )
            # Sponsorship needs no special handling: `flatten` already put the bot's
            # own line in the text, so it rides along in the description and is readable
            # by the `level` pass and by you. It deliberately goes no further — a
            # criteria token would be a gate applied before any title is read (the
            # `locations_exclude` mistake, which discarded 390 postings unseen), and in
            # the title "U.S. Citizenship is Required" would collide with `clearance` in
            # `exclude_titles`.

            postings.append(
                Posting(
                    company=group,
                    # The message id: Discord's own stable identifier, monotonic, and
                    # the same thing the cursor is made of. No hashing needed, unlike
                    # the aggregator's synthetic ids.
                    ats_job_id=message_id,
                    title=title[:_TITLE_MAX],
                    # `postings.url` is NOT NULL, and a posting nobody can open is not
                    # worth importing — so a message with no link still gets one back to
                    # itself rather than an empty string.
                    url=parsed.url or permalink,
                    location=parsed.location,
                    posted_at=parsed.posted_at or str(message.get("timestamp") or "") or None,
                    posted_on=parsed.posted_on,
                    description=text,
                )
            )
        return postings, unparsed, skipped


register(Discord())
