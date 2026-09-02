"""Plugins: the parts of this system you switch on and off in `plugins.yaml`.

A plugin has a **kind**, and there are two.

An **import** plugin is a feed of postings that is not a board in companies.yaml. It is
the original kind and everything below about cursors, pages and `Posting` rows belongs to
it. A **task** plugin is one of the bounded model roles in `tasks/` — it reads no feed,
holds no cursor and produces no postings; the plugin layer supplies only its on/off
switch, while `tasks/` stays the implementation.

Two kinds rather than one abstraction covering both, because the abstraction would be a
lie at every method: `page_url` returns a URL, `page_error` reads a decoded page,
`parse_page` returns `Posting` objects. A model role implements none of those honestly,
and a base class full of methods a whole kind must stub is how a registry stops meaning
anything. What the two genuinely share is a name, a summary, a settings schema and
`unavailable_reason` — so that, and only that, is what `BasePlugin` holds.

The third hand-wired registry in this package, and deliberately the same shape as the
other two — `sources/` for an ATS, `tasks/` for a model pass. Adding one is one module
plus one import line in `__init__.py`, and the module is **pure**: it builds URLs, parses
payloads and does its own cursor arithmetic, while `runner.collect` owns the socket and
the paging loop and the caller owns the connection, the clock and the transaction. That
split is what lets a plugin be tested against a recorded payload with no network.

**`plugins/` may import `tasks/`; `tasks/` must never import `plugins/`.** A task module
is pure and knows nothing about being switchable — which is what lets `survey()` take the
enabled set as an argument instead of reaching for a config file.

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

# What a plugin is. `kind` exists so the callers that are feed-shaped — `cmd_check`'s
# import loop, `plugins purge` — can ask for the kind they can actually drive, rather
# than duck-typing their way into calling `page_url` on a model role.
KIND_IMPORT = "import"
KIND_TASK = "task"
KINDS = (KIND_IMPORT, KIND_TASK)


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


class BasePlugin:
    """What every plugin has, whatever kind it is: a name, a schema, and a switch."""

    name: str = ""
    summary: str = ""
    kind: str = KIND_IMPORT

    def defaults(self) -> dict:
        """This plugin's settings and their default values.

        The schema too, implicitly: `load_settings` rejects any key not in here and takes
        each value's *type* from its default, so a plugin that wants an int declares an
        int. Per-plugin rather than one global dict, because the global one made every
        plugin's config surface the union of every other plugin's — a Discord channel id
        was a valid setting on a model role, and validated as one.
        """
        return {"enabled": False}

    def validate(self, settings: dict) -> None:
        """Raise `InvalidSettings` for a value that parses but cannot be acted on.

        Types are already checked against `defaults()` by the time this runs; this is for
        the semantic half — "a channel id is numeric", "a day count is not negative".
        It lives on the plugin because those rules are the plugin's own business.
        """
        return None

    def unavailable_reason(self, settings: dict) -> Optional[str]:
        """Why this plugin cannot run, or None if it can.

        Missing *configuration* only — a token that is not set, a channel id nobody has
        filled in. Missing *work* is not a reason: a feed with nothing new is working.
        This is the `MAILDIR` distinction, and `cmd_check` logs an enabled-but-unavailable
        plugin at WARNING because that is something to go and fix, not the same state as
        switched off.
        """
        return None


class Plugin(BasePlugin):
    """One external feed. Pure — see the module docstring."""

    kind = KIND_IMPORT
    page_size: int = 0

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

    def describe_cursor(self, cursor: str) -> str:
        """How far this feed has read, phrased for a person. Opaque by default.

        A cursor is the plugin's own token and only the plugin knows how to read it.
        `plugins list` used to decode every plugin's cursor with Discord's snowflake
        decoder, imported directly into the CLI — invisible while Discord was the only
        feed, and a confidently wrong date for the second one.
        """
        return cursor


class TaskPlugin(BasePlugin):
    """The on/off switch for one bounded model role in `tasks/`.

    Deliberately thin. It carries no prompt, no schema and no queue — those stay in the
    task module, which is what "keep the implementation the same as the other model
    roles" means and what lets the remaining roles move here without being rewritten.
    All this adds is a line in plugins.yaml.

    It does **not** answer `unavailable_reason` on the task's behalf. The task's own
    version takes a `TaskContext` — a loaded profile, an answer bank, a resume — and this
    one is handed nothing but settings, so answering here would mean answering a
    different question and reporting it under the same name. Enablement is a decision you
    typed; availability is something a run finds out. `survey()` keeps them apart, and so
    does this.
    """

    kind = KIND_TASK
    # The key in `tasks._REGISTRY`. Normally the same string as `name`, and separate so
    # that renaming the switch never silently detaches it from the role it switches.
    task_name: str = ""

    def __init__(self, name: str, task_name: str = "", summary: str = "",
                 default_enabled: bool = False) -> None:
        self.name = name
        self.task_name = task_name or name
        self.summary = summary
        self._default_enabled = default_enabled

    def defaults(self) -> dict:
        return {"enabled": self._default_enabled}


_REGISTRY: dict = {}


def register(plugin: BasePlugin) -> BasePlugin:
    if not plugin.name:
        raise ValueError("a plugin must have a name")
    if plugin.kind not in KINDS:
        # Refused here rather than discovered by whichever loop picks it up first. A
        # plugin of no kind is one `plugins_of_kind` never returns, which reads exactly
        # like a plugin that is switched off.
        raise ValueError(
            f"plugin {plugin.name!r} must declare a kind: {', '.join(KINDS)}"
        )
    _REGISTRY[plugin.name] = plugin
    return plugin


def get_plugin(name: str) -> Optional[BasePlugin]:
    return _REGISTRY.get(name)


def all_plugins() -> list:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def plugins_of_kind(kind: str) -> list:
    """Every registered plugin of one kind, by name.

    The filter that keeps `cmd_check`'s import loop from being handed a model role. It is
    a separate function rather than an argument to `all_plugins()` so that a caller which
    forgot to filter reads as obviously unfiltered.
    """
    return [p for p in all_plugins() if p.kind == kind]


def plugin_names() -> list:
    return sorted(_REGISTRY)
