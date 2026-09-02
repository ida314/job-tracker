"""The import-plugin framework: the switch, the cursor, and containment.

What each section is protecting:

  * **The switch.** Disabling must stop reading and change nothing else. An absent
    plugins.yaml, a disabled plugin and a missing credential all mean "contributes
    nothing" — but enabled-and-unable is a different state from switched off, and
    conflating them is how a feed quietly stops importing.
  * **The cursor.** Every rule here is about not losing messages: a failed read must not
    advance it, an unrecognized payload must not read as an empty page, and a page of
    pure conversation must still move it or the walk stalls forever.
  * **Containment.** A plugin's group is not curation, its failures never reach the
    slug-repair agent, and its credential never reaches a log line or a span.

Stubs are hand-written, per house style. No network, no Playwright, no router.
"""

import logging

import pytest

from jobtracker import store
from jobtracker.models import Posting
from jobtracker.plugins import base, runner
from jobtracker.plugins import settings as plugin_settings


# -- stubs ---------------------------------------------------------------------------
class _Feed(base.Plugin):
    """A minimal plugin: pages of dicts with an `id`, every one a posting."""

    name = "feed"
    summary = "a test feed"
    page_size = 2

    def __init__(self, pages):
        self.pages = pages

    def company(self, settings):
        from jobtracker.models import Company

        return Company(name="Feed: #x", ats="plugin", slug="", check_method="plugin")

    def unavailable_reason(self, settings):
        return settings.get("_unavailable")

    def first_cursor(self, settings, today):
        return "0"

    def page_url(self, settings, after):
        return f"https://feed.example/items?after={after}"

    def page_error(self, raw):
        return None if isinstance(raw, list) else "unexpected payload shape"

    def page_ids(self, raw):
        return [str(i["id"]) for i in raw] if isinstance(raw, list) else []

    def page_cursor(self, raw):
        ids = [int(i["id"]) for i in raw] if isinstance(raw, list) else []
        return str(max(ids)) if ids else None

    def parse_page(self, group, raw, settings, today):
        posts = [
            Posting(group, str(i["id"]), i.get("title", "SWE"), i.get("url", "https://x/1"))
            for i in raw
            if i.get("title") != "__chat__"
        ]
        skipped = sum(1 for i in raw if i.get("title") == "__chat__")
        return posts, 0, skipped


class _Fetcher:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def fetch_json(self, url, headers=None):
        self.calls.append((url, headers))
        return self.responses.pop(0) if self.responses else (200, [], None)


def _item(i, **kw):
    return {"id": i, **kw}


# -- the switch ----------------------------------------------------------------------
def test_an_absent_plugins_file_is_a_normal_state_not_an_error(tmp_path):
    assert plugin_settings.load_settings(tmp_path / "nope.yaml") == {}


def test_a_plugin_is_off_until_you_say_otherwise():
    """Installing an import plugin never starts reading anything.

    Asserted over every registered import plugin rather than over one shared dict,
    because the schema is per-plugin now and a dict nobody consults would keep passing
    this test while a plugin quietly defaulted itself on. Task plugins are deliberately
    excluded: the three roles that predate the switch default to on, so that adding the
    switch changed nothing for anyone who does not use it.
    """
    from jobtracker.plugins import KIND_IMPORT, plugins_of_kind

    for plugin in plugins_of_kind(KIND_IMPORT):
        assert plugin_settings.defaults_for(plugin.name)["enabled"] is False, plugin.name


def test_enabling_writes_through_safewrite_and_a_refused_candidate_writes_nothing(tmp_path):
    from jobtracker import safewrite

    path = tmp_path / "plugins.yaml"
    plugin_settings.set_options(path, "discord", {"enabled": True, "channel_id": "123"})
    before = path.read_text()
    with pytest.raises((plugin_settings.InvalidSettings, safewrite.RefusedWrite)):
        plugin_settings.set_options(path, "discord", {"channel_id": "not-a-number"})
    assert path.read_text() == before


@pytest.mark.parametrize(
    "key,value", [("backfill_days", "soon"), ("backfill_days", "-3"), ("enabled", "maybe")],
)
def test_a_bad_value_is_refused_at_the_cli_not_at_one_am(key, value):
    """`-3` is the one that matters here.

    Its refusal is a *semantic* rule, and semantic rules moved onto the plugin when the
    schema went per-plugin. So `coerce` has to run `validate` as well as convert, or the
    refusal degrades from "refused while you are standing there" to a RefusedWrite three
    layers down in safewrite — which is the failure this test is named after.
    """
    with pytest.raises(plugin_settings.InvalidSettings):
        plugin_settings.coerce("discord", key, value)


def test_setting_an_unknown_key_is_refused():
    with pytest.raises(plugin_settings.InvalidSettings):
        plugin_settings.coerce("discord", "colour", "blue")


def test_a_channel_name_is_refused_because_discord_wants_the_numeric_id(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text("discord:\n  enabled: true\n  channel_id: new-grad-jobs\n")
    with pytest.raises(plugin_settings.InvalidSettings):
        plugin_settings.load_settings(path)


def test_an_unknown_plugin_name_in_the_file_is_refused(tmp_path):
    """Silently ignoring it would let a typo read as "that feed imported nothing"."""
    path = tmp_path / "plugins.yaml"
    path.write_text("dsicord:\n  enabled: true\n")
    with pytest.raises(plugin_settings.InvalidSettings):
        plugin_settings.load_settings(path)


def test_settings_for_fills_in_defaults_for_a_plugin_nobody_configured(tmp_path):
    got = plugin_settings.settings_for("discord", tmp_path / "none.yaml")
    assert got["enabled"] is False and got["expire_after_days"] == 90


# -- the cursor ----------------------------------------------------------------------
def _collect(pages, responses=None):
    plugin = _Feed(pages)
    fetcher = _Fetcher(responses or [(200, p, None) for p in pages] + [(200, [], None)])
    return plugin, fetcher, runner.collect(plugin, fetcher, {}, {}, "2026-08-31")


def test_a_failed_read_does_not_advance_the_cursor():
    """A failure means we do not know what arrived. Stamping the cursor anyway would
    skip that window permanently, with no error left behind to find it by."""
    plugin = _Feed([])
    fetcher = _Fetcher([(None, None, "Timeout")])
    result = runner.collect(plugin, fetcher, {}, {"cursor": "5"}, "2026-08-31")
    assert result.ok is False and result.error
    assert result.cursor == {}


def test_a_two_hundred_that_is_not_a_list_is_a_failure_not_an_empty_read():
    """Zero new items is the normal answer for an incremental feed, so it is exactly the
    reading that must not be reachable by accident."""
    plugin = _Feed([])
    fetcher = _Fetcher([(200, {"message": "Missing Access", "code": 50001}, None)])
    result = runner.collect(plugin, fetcher, {}, {}, "2026-08-31")
    assert result.ok is False
    assert "unexpected payload shape" in result.error
    assert result.cursor == {}


def test_the_cursor_advances_past_messages_that_were_filtered_out():
    """A page of pure conversation produces no postings. A cursor derived from postings
    would therefore not move, and the next run would re-read the same page forever."""
    page = [_item(1, title="__chat__"), _item(2, title="__chat__")]
    plugin = _Feed([page])
    fetcher = _Fetcher([(200, page, None), (200, [], None)])
    result = runner.collect(plugin, fetcher, {}, {}, "2026-08-31")
    assert result.postings == []
    assert result.skipped == 2
    assert result.cursor == {"cursor": "2"}


def test_the_cursor_is_persisted_even_when_it_did_not_move():
    """`first_cursor` is derived from `today`, so a quiet channel would otherwise
    recompute a floor that slides forward a day every night — and anything older than the
    window would be skipped without ever having been read."""
    plugin = _Feed([])
    fetcher = _Fetcher([(200, [], None)])
    result = runner.collect(plugin, fetcher, {}, {}, "2026-08-31")
    assert result.ok and result.cursor == {"cursor": "0"}


def test_the_driver_stops_on_a_short_page():
    plugin = _Feed([])
    fetcher = _Fetcher([(200, [_item(1)], None)])  # page_size is 2, so this is short
    result = runner.collect(plugin, fetcher, {}, {}, "2026-08-31")
    assert result.ok and len(fetcher.calls) == 1


def test_a_page_that_adds_no_new_id_ends_the_walk():
    """A vendor that wraps to the beginning instead of ending does not error and does not
    return a short page — the Nvidia lesson from the Workday adapter, carried over."""
    page = [_item(1), _item(2)]
    plugin = _Feed([])
    fetcher = _Fetcher([(200, page, None)] * 10)
    result = runner.collect(plugin, fetcher, {}, {}, "2026-08-31")
    assert result.ok
    assert len(fetcher.calls) == 2  # the repeat stopped it, not the cap


def test_the_page_cap_is_a_ceiling_not_a_filter(caplog):
    plugin = _Feed([])
    fetcher = _Fetcher([(200, [_item(i), _item(i + 100)], None) for i in range(1, 60)])
    with caplog.at_level(logging.WARNING):
        result = runner.collect(plugin, fetcher, {}, {}, "2026-08-31")
    assert result.ok
    assert len(fetcher.calls) == runner.MAX_PAGES
    assert "cap" in caplog.text


def test_the_first_read_is_flagged_so_an_empty_one_can_be_questioned():
    plugin = _Feed([])
    fetcher = _Fetcher([(200, [], None)])
    assert runner.collect(plugin, fetcher, {}, {}, "2026-08-31").first_read is True
    fetcher = _Fetcher([(200, [], None)])
    assert runner.collect(plugin, fetcher, {}, {"cursor": "9"}, "2026-08-31").first_read is False


# -- containment ---------------------------------------------------------------------
def test_a_plugins_group_is_never_a_curated_company():
    """It is a name to key `postings` on, not an entry anybody authored. Nothing may put
    it into companies.yaml, and `curation` does not know the check_method."""
    from jobtracker import curation
    from jobtracker.plugins import get_plugin

    company = get_plugin("discord").company({"label": "jobs"})
    assert company.check_method not in curation.CHECK_METHODS
    assert company.ats not in curation.ATS_VALUES


def test_a_plugin_group_has_no_source_adapter_so_nothing_tries_to_fetch_it_as_a_board():
    from jobtracker.plugins import get_plugin
    from jobtracker.sources import get_source

    assert get_source(get_plugin("discord").company({}).ats) is None


def test_adding_a_plugin_is_one_module_plus_one_import_line():
    """The same two steps as adding an ATS or a task. If this file grows a discovery
    mechanism, that is a new abstraction and it should be argued for."""
    import pathlib

    text = pathlib.Path("jobtracker/plugins/__init__.py").read_text()
    assert "from . import discord" in text
    assert "importlib" not in text and "entry_points" not in text


def test_the_registry_refuses_a_nameless_plugin():
    with pytest.raises(ValueError):
        base.register(base.Plugin())


# -- the credential ------------------------------------------------------------------
def test_the_token_travels_in_a_header_and_never_in_the_url():
    """`fetch._request` records `url.full` as a span attribute and logs the URL on every
    retry, so a token in a query parameter lands in traces and logs the same day."""
    from jobtracker import config
    from jobtracker.plugins import discord as dmod

    original = dmod.config.DISCORD_TOKEN
    dmod.config.DISCORD_TOKEN = "s3cret-token"
    try:
        plugin = dmod.Discord()
        url = plugin.page_url({"channel_id": "123"}, "0")
        assert "s3cret-token" not in url
        assert plugin.auth_headers()["Authorization"] == "Bot s3cret-token"
    finally:
        dmod.config.DISCORD_TOKEN = original
        assert config.DISCORD_TOKEN == original


def test_the_token_never_reaches_a_log_line(caplog):
    from jobtracker.plugins import discord as dmod

    original = dmod.config.DISCORD_TOKEN
    dmod.config.DISCORD_TOKEN = "s3cret-token"
    try:
        plugin = dmod.Discord()
        fetcher = _Fetcher([(200, [], None)])
        with caplog.at_level(logging.DEBUG):
            runner.collect(plugin, fetcher, {"channel_id": "1"}, {}, "2026-08-31")
        assert "s3cret-token" not in caplog.text
    finally:
        dmod.config.DISCORD_TOKEN = original


def test_a_plugin_with_no_token_reports_itself_unavailable():
    from jobtracker.plugins import discord as dmod

    original = dmod.config.DISCORD_TOKEN
    dmod.config.DISCORD_TOKEN = None
    try:
        assert "JOBTRACKER_DISCORD_TOKEN" in dmod.Discord().unavailable_reason({})
    finally:
        dmod.config.DISCORD_TOKEN = original


def test_a_configured_token_but_no_channel_is_still_unavailable():
    from jobtracker.plugins import discord as dmod

    original = dmod.config.DISCORD_TOKEN
    dmod.config.DISCORD_TOKEN = "t"
    try:
        assert "channel_id" in dmod.Discord().unavailable_reason({})
        assert dmod.Discord().unavailable_reason({"channel_id": "1"}) is None
    finally:
        dmod.config.DISCORD_TOKEN = original


# -- append-only ingestion ------------------------------------------------------------
def test_an_incremental_poll_never_closes_yesterdays_postings():
    """The concrete reason a plugin is not a fifth Source. `sync_postings` closes every
    posting absent from a fetch, and a poll returns only what is new — so one quiet
    evening would close every posting the feed had ever imported."""
    conn = store.connect(":memory:")
    store.append_postings(conn, "Feed", [Posting("Feed", "1", "SWE", "https://x/1")], "2026-08-01")
    store.append_postings(conn, "Feed", [], "2026-08-02")  # a quiet night
    row = conn.execute("SELECT closed_at FROM postings").fetchone()
    assert row["closed_at"] is None


def test_re_reading_the_same_messages_imports_nothing_twice():
    """An interrupted run leaves the cursor unwritten, so tomorrow re-reads the window."""
    conn = store.connect(":memory:")
    post = Posting("Feed", "1", "SWE", "https://x/1", description="body")
    first, _ = store.append_postings(conn, "Feed", [post], "2026-08-01")
    again, _ = store.append_postings(conn, "Feed", [post], "2026-08-02")
    assert len(first) == 1 and again == []
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1


def test_a_posting_arrives_with_its_description_already_stored():
    """Nothing else would ever write it: `_cache_descriptions` builds its work list from
    the board loop, and a plugin company is not in it. A NULL description drops the row
    out of `level`'s queue AND out of `matches_needing_judgment`, so it is never judged,
    never scored, and `rank.available` filters it out — in the table, absent from the
    product."""
    conn = store.connect(":memory:")
    store.append_postings(
        conn, "Feed", [Posting("Feed", "1", "SWE", "https://x/1", description="the text")],
        "2026-08-01",
    )
    assert store.get_description(conn, "Feed", "1") == "the text"


def test_a_re_append_can_never_blank_a_stored_description():
    conn = store.connect(":memory:")
    store.append_postings(
        conn, "Feed", [Posting("Feed", "1", "SWE", "https://x/1", description="body")], "2026-08-01"
    )
    store.append_postings(
        conn, "Feed", [Posting("Feed", "1", "SWE", "https://x/1", description="")], "2026-08-02"
    )
    assert store.get_description(conn, "Feed", "1") == "body"


def test_a_duplicate_of_something_already_tracked_is_never_imported():
    conn = store.connect(":memory:")
    url = "https://jobs.lever.co/acme/xyz"
    store.sync_postings(conn, "Acme", [Posting("Acme", "xyz", "SWE", url)], "2026-08-01",
                        identity=("lever", "acme"))
    inserted, suppressed = store.append_postings(
        conn, "Feed", [Posting("Feed", "9", "Acme — SWE", url)], "2026-08-02"
    )
    assert inserted == [] and len(suppressed) == 1
    assert conn.execute("SELECT COUNT(*) FROM postings WHERE company='Feed'").fetchone()[0] == 0


def test_a_feed_posting_closes_by_age_and_never_by_absence():
    conn = store.connect(":memory:")
    store.append_postings(
        conn, "Feed",
        [Posting("Feed", "old", "SWE", "https://x/1", posted_on="2026-01-01"),
         Posting("Feed", "new", "SWE", "https://x/2", posted_on="2026-08-01")],
        "2026-08-31",
    )
    assert store.close_stale_postings(conn, "Feed", "2026-06-01", "2026-08-31") == 1
    rows = {r["ats_job_id"]: r for r in conn.execute("SELECT * FROM postings")}
    assert rows["old"]["closed_reason"] == "aged_out"
    assert rows["new"]["closed_at"] is None


def test_expiry_leaves_a_posting_closed_for_a_better_known_reason_alone():
    conn = store.connect(":memory:")
    store.append_postings(
        conn, "Feed", [Posting("Feed", "1", "SWE", "https://x/1", posted_on="2026-01-01")],
        "2026-08-31",
    )
    conn.execute("UPDATE postings SET closed_at='2026-02-01', closed_reason='duplicate'")
    assert store.close_stale_postings(conn, "Feed", "2026-06-01", "2026-08-31") == 0


# -- state and purge -----------------------------------------------------------------
def test_plugin_state_is_scoped_per_plugin():
    conn = store.connect(":memory:")
    store.set_plugin_state(conn, "a", {"cursor": "1"}, "2026-08-01")
    store.set_plugin_state(conn, "b", {"cursor": "2"}, "2026-08-01")
    assert store.get_plugin_state(conn, "a") == {"cursor": "1"}
    assert store.get_plugin_state(conn, "b") == {"cursor": "2"}


def test_purge_removes_postings_and_everything_derived_from_them():
    conn = store.connect(":memory:")
    store.append_postings(conn, "Feed", [Posting("Feed", "1", "SWE", "https://x/1")], "2026-08-01")
    store.set_plugin_state(conn, "feed", {"cursor": "5"}, "2026-08-01")
    removed = store.purge_company(conn, "Feed", plugin="feed")
    assert removed.get("postings") == 1
    assert store.get_plugin_state(conn, "feed") == {}


def test_purge_keeps_your_judgments_and_your_applications():
    """The tuning corpus and the fact that you applied both outlive the posting row.
    `decisions.title` is denormalized precisely so the corpus does not shrink."""

    conn = store.connect(":memory:")
    store.append_postings(conn, "Feed", [Posting("Feed", "1", "SWE", "https://x/1")], "2026-08-01")
    store.record_decision(conn, "Feed", "1", "SWE", "match", "2026-08-02", note="mine")
    store.set_override(conn, "Feed", "1", "match", "2026-08-02", reason="mine")
    store.record_application(conn, "Feed", "1", "SWE", "applied", "2026-08-03")
    store.purge_company(conn, "Feed", plugin="feed")
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM overrides").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1


def test_purge_names_the_rows_you_applied_to_before_removing_anything():
    conn = store.connect(":memory:")
    store.append_postings(conn, "Feed", [Posting("Feed", "1", "SWE", "https://x/1")], "2026-08-01")
    store.record_application(conn, "Feed", "1", "SWE", "applied", "2026-08-03")
    blockers = store.purge_blockers(conn, "Feed")
    assert [b["ats_job_id"] for b in blockers] == ["1"]


# -- kinds ---------------------------------------------------------------------------
def test_a_plugin_must_declare_a_kind_a_loop_actually_owns():
    """A plugin of no kind is one `plugins_of_kind` never returns.

    Which reads exactly like a plugin that is switched off — so it is refused at
    registration, in front of whoever wrote it, rather than discovered as an absence at
    01:00 by a loop that never picked it up.
    """
    class _Kindless(base.BasePlugin):
        name = "kindless"
        kind = "neither"

    with pytest.raises(ValueError):
        base.register(_Kindless())


def test_only_import_plugins_reach_the_feed_loop():
    """`runner.collect`'s first act is an HTTP request for `plugin.page_url(...)`.

    Handed a model role it would ask a thing with no URL for one. The kind filter is what
    stops that, and `cmd_check` must use it rather than `all_plugins()`.
    """
    from jobtracker import plugins as plugins_mod

    imports = [p.name for p in plugins_mod.plugins_of_kind(plugins_mod.KIND_IMPORT)]
    tasks_ = [p.name for p in plugins_mod.plugins_of_kind(plugins_mod.KIND_TASK)]
    assert "discord" in imports and "discord" not in tasks_
    assert "judge" in tasks_ and "judge" not in imports


def test_a_task_plugin_is_never_asked_for_a_company_a_cursor_or_a_page():
    """It does not implement the feed protocol — it does not have it.

    The failure is then an AttributeError at the boundary, not a NotImplementedError
    three layers into a paging loop that has already opened a socket.
    """
    from jobtracker import plugins as plugins_mod

    for plugin in plugins_mod.plugins_of_kind(plugins_mod.KIND_TASK):
        for method in ("company", "page_url", "page_cursor", "parse_page"):
            assert not hasattr(plugin, method), f"{plugin.name}.{method}"


def test_every_task_plugin_switches_a_task_that_exists():
    """A switch pointing at nothing is a control that silently does nothing."""
    from jobtracker import plugins as plugins_mod
    from jobtracker.tasks import get_task

    for plugin in plugins_mod.plugins_of_kind(plugins_mod.KIND_TASK):
        assert get_task(plugin.task_name) is not None, plugin.name


# -- the schema ----------------------------------------------------------------------
def test_a_plugin_only_accepts_the_settings_it_declares(tmp_path):
    """The flat DEFAULTS let any plugin be pointed at a Discord channel.

    `channel_id` was a valid setting on every plugin and was `.isdigit()`-validated as
    one, so the config surface of each was the union of all of them.
    """
    path = tmp_path / "plugins.yaml"
    path.write_text('judge:\n  enabled: true\n  channel_id: "123"\n')
    with pytest.raises(plugin_settings.InvalidSettings):
        plugin_settings.load_settings(path)


def test_an_existing_discord_file_loads_unchanged_after_the_split(tmp_path):
    """The migration property, asserted key by key.

    A plugins.yaml written before plugins had kinds holds exactly these six keys with
    exactly these defaults. `set_options` trims values equal to their default, so a
    changed default would silently re-interpret a value already on disk.
    """
    path = tmp_path / "plugins.yaml"
    path.write_text(
        "discord:\n"
        "  enabled: true\n"
        '  channel_id: "123456789012345678"\n'
        '  guild_id: "987654321098765432"\n'
        "  label: new-grad-jobs\n"
        "  backfill_days: 14\n"
        "  expire_after_days: 90\n"
    )
    got = plugin_settings.load_settings(path)["discord"]
    assert got == {
        "enabled": True,
        "channel_id": "123456789012345678",
        "guild_id": "987654321098765432",
        "label": "new-grad-jobs",
        "backfill_days": 14,
        "expire_after_days": 90,
    }


def test_the_day_counts_are_discords_rule_and_not_every_plugins(tmp_path):
    """`expire_after_days` is not a setting a model role has, so its rule is not one
    a model role is held to. That was the whole defect in one global dict."""
    assert "expire_after_days" not in plugin_settings.defaults_for("judge")
    assert "expire_after_days" in plugin_settings.defaults_for("discord")


def test_priority_is_not_a_setting_plugins_yaml_accepts(tmp_path):
    """Priority is the pipeline's dependency chain, not a preference.

    In yaml it would put half the queue's ordering in a file, make `all_tasks()` a
    function of what is on disk, and make the ordering test a statement about one
    machine. It falls out of the unknown-key rejection for free; this says so on purpose.
    """
    path = tmp_path / "plugins.yaml"
    path.write_text("judge:\n  enabled: true\n  priority: 5\n")
    with pytest.raises(plugin_settings.InvalidSettings):
        plugin_settings.load_settings(path)


def test_a_cursor_is_described_by_the_plugin_that_minted_it():
    """`plugins list` decoded every plugin's cursor with Discord's snowflake decoder,
    imported straight into the CLI. Invisible with one feed; a confidently wrong date
    for the second."""
    assert base.Plugin().describe_cursor("whatever-this-is") == "whatever-this-is"
