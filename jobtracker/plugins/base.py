"""Import plugins: a feed of postings that is not a board in companies.yaml.

The third hand-wired registry in this package, and deliberately the same shape as the
other two — `sources/` for an ATS, `tasks/` for a model pass. Adding one is one module
plus one import line in `__init__.py`, and the module is **pure**: it builds URLs, parses
payloads and does its own cursor arithmetic, while `runner.collect` owns the socket and
the paging loop and the caller owns the connection, the clock and the transaction. That
split is what lets a plugin be tested against a recorded payload with no network.

Why this is not a fifth `Source`. A `Source` describes a *board*, and a board is a
complete statement of what a company has open — which is why `sync_postings` closes every
posting absent from a fetch. A plugin reads an incremental feed, where the normal answer
is "nothing new since last time". Routed through a Source, one quiet evening would close
every posting the feed had ever imported. `store.append_postings` exists for that reason,
and so does this package.

What a plugin is switched on by is `plugins.yaml` — curation, like companies.yaml, with
every writer a command you typed. Where its reader got to is `plugin_state` in state.db —
observation. Keeping those apart is DESIGN.md 3.3, and it is what lets you delete
state.db and keep your configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import Company, Posting


@dataclass
class PluginFetch:
    """The outcome of one plugin's read. What `FetchResult` is for a board.

    `read` and `imported` are separate on purpose. "Read 40 messages, imported 3" is a
    healthy night, "read 0" is also a healthy night, and an error is neither — three
    states that a single posting count would flatten into one ambiguous zero. That is the
    same distinction `health.evaluate_plugin` is built around.

    `cursor` empty means **do not advance**. A failed read must leave it alone: we do not
    know what arrived, and stamping it anyway would skip those messages permanently with
    no error left behind (DESIGN.md 7.3, applied to a cursor).
    """

    plugin: str
    ok: bool = False
    postings: list[Posting] = field(default_factory=list)
    read: int = 0
    imported: int = 0
    unparsed: int = 0
    skipped: int = 0
    cursor: dict = field(default_factory=dict)
    error: Optional[str] = None
    first_read: bool = False


class Plugin:
    """One external feed. Pure — see the module docstring."""

    name: str = ""
    summary: str = ""
    page_size: int = 0

    def unavailable_reason(self, settings: dict) -> Optional[str]:
        """Why this plugin cannot run, or None if it can.

        Missing *configuration* only — a token that is not set, a channel id nobody has
        filled in. Missing *work* is not a reason: a feed with nothing new is working.
        This is the `MAILDIR` distinction, and `cmd_check` logs an enabled-but-unavailable
        plugin at WARNING because that is something to go and fix, not the same state as
        switched off.
        """
        return None

    def company(self, settings: dict) -> Company:
        """The synthetic posting group this feed writes under.

        It exists because `postings` is keyed by `(company, ats_job_id)` and every join
        downstream goes through that name. It is **not curation**: it is never written to
        companies.yaml and never joined onto a `load_companies` result. Every consumer
        resolves companies with `.get` and degrades to tier `-`, and that absence is
        load-bearing in one place — `repair.detect` skips companies it cannot find, which
        is what stops a failing feed sending the slug-repair agent to scrape Discord.
        """
        raise NotImplementedError

    def first_cursor(self, settings: dict, today: str) -> str:
        """Where to start reading when there is no stored cursor."""
        raise NotImplementedError

    def page_url(self, settings: dict, after: str) -> str:
        raise NotImplementedError

    def auth_headers(self) -> dict:
        return {}

    def page_error(self, raw: object) -> Optional[str]:
        """Why this payload is not a usable page of results, or None if it is.

        The same job `Source.jobs_page_error` does, and it carries more weight here. On a
        board, zero rows is suspicious. On an incremental feed, zero rows is the *normal*
        reading — so it is the one answer that must never be reachable by accident. A 200
        carrying a shape we do not understand has to stay distinguishable from a quiet
        night, or a broken read advances the cursor past messages nothing will ever look
        at again.
        """
        return None

    def page_cursor(self, raw: object) -> Optional[str]:
        """The cursor to ask for next, read off the RAW page.

        Deliberately not derived from `parse_page`'s output. A page of forty human chat
        messages yields zero postings, and a cursor built from postings would therefore
        not move — so the next run would re-read the same forty messages, and the run
        after that too, forever.
        """
        return None

    def page_ids(self, raw: object) -> list:
        """Every item id on the raw page, whether or not it parsed into a posting.

        Separate from `parse_page` for the same reason `page_cursor` is: the loop's
        "did this page tell us anything new?" stopping rule has to count *items seen*,
        not postings produced, or a page of pure conversation reads as an empty page and
        the walk stops one page into a backlog.
        """
        return []

    def parse_page(self, group: str, raw: object, settings: dict, today: str):
        """`(postings, unparsed, skipped)` for one page. Pure, and never raises."""
        raise NotImplementedError


_REGISTRY: dict = {}


def register(plugin: Plugin) -> Plugin:
    if not plugin.name:
        raise ValueError("a plugin must have a name")
    _REGISTRY[plugin.name] = plugin
    return plugin


def get_plugin(name: str) -> Optional[Plugin]:
    return _REGISTRY.get(name)


def all_plugins() -> list:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def plugin_names() -> list:
    return sorted(_REGISTRY)
