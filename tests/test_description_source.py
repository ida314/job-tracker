"""`postings.description_source`: what kind of text a stored description is.

`description` holds two different things — prose (an ATS detail payload, a Discord
message, a YC listing's own words) and feed metadata Simplify assembles from its JSON
fields. The column is what tells them apart, so every writer has to set it and every row
that predates it has to be labelled on connect.
"""

import ast
import json
import logging
import pathlib

import pytest

from jobtracker import config, store
from jobtracker.criteria import load_criteria
from jobtracker.models import Company, Posting
from jobtracker.plugins import (
    KIND_IMPORT,
    KIND_TASK,
    description_source,
    description_sources,
    get_plugin,
    plugins_of_kind,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
TODAY = "2026-09-15"


def _sources(conn):
    return {
        (r["company"], r["ats_job_id"]): r["description_source"]
        for r in conn.execute("SELECT company, ats_job_id, description_source FROM postings")
    }


class _StubFetcher:
    """Serves one recorded payload for every page a board asks for."""

    def __init__(self, json_payload=None, text_payload=None):
        self.json_payload = json_payload
        self.text_payload = text_payload

    def fetch_json(self, url, headers=None):
        return 200, self.json_payload, None

    def fetch_text(self, url, headers=None):
        return 200, self.text_payload, None


def _plugin_run(conn, fetcher, name, **settings):
    from jobtracker import cli

    plugin = get_plugin(name)
    stats = {"companies": 0, "ok": 0, "failed": 0, "new_postings": 0, "matches": 0}
    degraded: list = []
    active = [(plugin, {**plugin.defaults(), "enabled": True, **settings})]
    cli._run_plugins(
        conn, fetcher, active, load_criteria(config.CRITERIA_YAML),
        store.load_overrides(conn), TODAY, stats, degraded,
    )
    conn.commit()
    assert degraded == [] and stats["failed"] == 0
    return plugin.company(active[0][1]).name


# -- the migration -------------------------------------------------------------------
def test_description_source_migrates_an_existing_db(tmp_path, caplog):
    """Built the way the dedupe-index test builds one: a real database, the column
    dropped back off, then a reconnect. A column declared in `_SCHEMA` would pass every
    freshly-built database in this suite and fail the one that matters."""
    db = tmp_path / "old.db"
    conn = store.connect(db)
    store.sync_postings(
        conn, "Acme", [Posting("Acme", "1", "SWE", "https://x/1"),
                       Posting("Acme", "2", "SWE", "https://x/2")], TODAY,
    )
    store.set_description(conn, "Acme", "1", "We build payments.", source="ats")
    group = "Simplify New-Grad-Positions"
    store.append_postings(
        conn, group, [Posting(group, "s", "X — SWE", "https://y/s",
                              description="Category: Software")],
        TODAY, origin="simplify", description_source="feed",
    )
    store.append_postings(
        conn, "Discord: #jobs",
        [Posting("Discord: #jobs", "d", "Y — SWE", "https://z/d",
                 description="## [SWE @ Y]")],
        TODAY, origin="discord", description_source="prose",
    )
    store.append_postings(
        conn, "Mystery", [Posting("Mystery", "m", "SWE", "https://m/m", description="?")],
        TODAY, origin="someboard", description_source="prose",
    )
    conn.commit()
    conn.execute("ALTER TABLE postings DROP COLUMN description_source")
    conn.commit()
    conn.close()

    with caplog.at_level(logging.INFO, logger="jobtracker.store"):
        reopened = store.connect(db, description_sources=description_sources())

    cols = {r["name"] for r in reopened.execute("PRAGMA table_info(postings)")}
    assert "description_source" in cols
    assert _sources(reopened) == {
        ("Acme", "1"): "ats",
        ("Acme", "2"): None,  # no description, so nothing to label
        (group, "s"): "feed",
        ("Discord: #jobs", "d"): "prose",
        ("Mystery", "m"): None,  # an origin nobody vouched for is not guessed
    }
    assert any("labelled the source of 3 stored description(s)" in r.getMessage()
               for r in caplog.records)

    # Self-draining: the second connect has nothing left to say.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="jobtracker.store"):
        store.connect(db, description_sources=description_sources()).close()
    assert not any("labelled the source" in r.getMessage() for r in caplog.records)


def test_a_legacy_feed_row_is_labelled_by_its_board_not_as_a_curated_one(tmp_path):
    """The legacy-origin migration has to run first. Before it, a Simplify row written
    under the old aggregator entry has `origin IS NULL` and would be labelled `ats`."""
    db = tmp_path / "old.db"
    conn = store.connect(db)
    group = "Simplify New-Grad-Positions"
    store.append_postings(
        conn, group, [Posting(group, "s", "X — SWE", "https://y/s",
                              description="Category: Software")], TODAY,
    )
    conn.execute("UPDATE postings SET origin=NULL")
    conn.commit()
    conn.execute("ALTER TABLE postings DROP COLUMN description_source")
    conn.commit()
    conn.close()

    reopened = store.connect(db, description_sources=description_sources())
    assert _sources(reopened) == {(group, "s"): "feed"}


# -- each writer sets it -------------------------------------------------------------
def test_simplify_rows_are_feed():
    conn = store.connect(":memory:")
    listings = json.loads((FIXTURES / "simplify_listings.json").read_text())
    group = _plugin_run(conn, _StubFetcher(json_payload=listings), "simplify")

    got = _sources(conn)
    assert got and all(company == group for company, _ in got)
    assert set(got.values()) == {"feed"}


def test_discord_and_yc_rows_are_prose():
    conn = store.connect(":memory:")
    yc = _plugin_run(
        conn, _StubFetcher(text_payload=(FIXTURES / "yc_jobs_page.html").read_text()),
        "ycombinator",
    )
    message = {
        "id": "1300000000000000001", "timestamp": "2026-09-14T12:00:00+00:00",
        "author": {"bot": True}, "embeds": [],
        "content": "## [Software Developer Associate @ Artera]"
                   "(<https://jobs.lever.co/artera-2/eae88c70>)\n"
                   "### Locations:  Seattle, WA\nPosted on: September 14, 2026",
    }
    discord = _plugin_run(
        conn, _StubFetcher(json_payload=[message]), "discord",
        channel_id="111", guild_id="222", label="jobs",
    )

    by_group: dict = {}
    for (company, _), source in _sources(conn).items():
        by_group.setdefault(company, set()).add(source)
    assert by_group == {yc: {"prose"}, discord: {"prose"}}


def test_board_rows_are_ats():
    """Every branch of `_cache_descriptions` — free from bulk, fetched, and the
    no-detail-endpoint `''` — is the board path, and all of it is `ats`."""
    from jobtracker.cli import _cache_descriptions

    class _Fetcher:
        def fetch_job_detail(self, company, ats_job_id):
            return f"description for {ats_job_id}", None

    conn = store.connect(":memory:")
    gh = Company(name="Acme", ats="greenhouse", slug="acme")
    agg = Company(name="Agg", ats="aggregator", slug="")
    bulk = Posting("Acme", "1", "SWE", "u1", description="from the bulk payload")
    fetched = Posting("Acme", "2", "SWE", "u2")
    nodetail = Posting("Agg", "3", "SWE", "u3")
    store.sync_postings(conn, "Acme", [bulk, fetched], TODAY)
    store.sync_postings(conn, "Agg", [nodetail], TODAY)
    # sync_postings writes no description, so nothing is labelled before the cache runs
    assert set(_sources(conn).values()) == {None}

    _cache_descriptions(conn, _Fetcher(),
                        [(gh, bulk), (gh, fetched), (agg, nodetail)], 100, TODAY)
    assert _sources(conn) == {("Acme", "1"): "ats", ("Acme", "2"): "ats",
                              ("Agg", "3"): "ats"}


def test_a_feed_row_with_no_description_carries_no_source():
    """NULL text has no kind. `''` is still a statement by that board — "fetched and
    genuinely empty" — so it is labelled like any other text it wrote."""
    conn = store.connect(":memory:")
    store.append_postings(
        conn, "Feed",
        [Posting("Feed", "1", "SWE", "https://x/1", description=None),
         Posting("Feed", "2", "SWE", "https://x/2", description="")],
        TODAY, origin="discord", description_source="prose",
    )
    assert _sources(conn) == {("Feed", "1"): None, ("Feed", "2"): "prose"}


def test_a_reread_labels_an_unlabelled_row_without_touching_its_description():
    conn = store.connect(":memory:")
    post = Posting("Feed", "1", "SWE", "https://x/1", description="first text")
    store.append_postings(conn, "Feed", [post], TODAY, origin="discord")
    assert _sources(conn) == {("Feed", "1"): None}

    later = Posting("Feed", "1", "SWE", "https://x/1", description="different text")
    store.append_postings(conn, "Feed", [later], TODAY, origin="discord",
                          description_source="prose")
    assert _sources(conn) == {("Feed", "1"): "prose"}
    assert store.get_description(conn, "Feed", "1") == "first text"  # still write-once


# -- the contract ----------------------------------------------------------------------
def test_set_description_requires_a_source():
    conn = store.connect(":memory:")
    store.sync_postings(conn, "Acme", [Posting("Acme", "1", "SWE", "u")], TODAY)
    with pytest.raises(TypeError):
        store.set_description(conn, "Acme", "1", "text")  # no default to fall back on
    with pytest.raises(ValueError):
        store.set_description(conn, "Acme", "1", "text", source=None)
    with pytest.raises(ValueError):
        store.set_description(conn, "Acme", "1", "text", source="model")
    assert store.get_description(conn, "Acme", "1") is None


def test_an_unknown_source_is_refused_everywhere_it_can_be_written():
    conn = store.connect(":memory:")
    with pytest.raises(ValueError):
        store.append_postings(conn, "Feed", [], TODAY, description_source="html")
    with pytest.raises(ValueError):
        store.connect(":memory:", description_sources={"simplify": "metadata"})


def test_coverage_is_what_each_board_says_it_publishes():
    assert get_plugin("simplify").coverage() == {"bands": ["entry"], "prose": False}
    assert get_plugin("ycombinator").coverage() == {"bands": [], "prose": True}
    assert get_plugin("discord").coverage() == {"bands": [], "prose": True}
    assert description_sources() == {
        p.name: description_source(p) for p in plugins_of_kind(KIND_IMPORT)
    }
    assert description_sources()["simplify"] == "feed"


def test_a_task_plugin_has_no_coverage_to_ask():
    """`coverage()` lives on the import plugin class only, so nothing can ask a model
    role what listings it publishes — and `description_sources` never tries."""
    tasks = plugins_of_kind(KIND_TASK)
    assert tasks
    assert not any(hasattr(p, "coverage") for p in tasks)
    assert not set(description_sources()) & {p.name for p in tasks}


def test_store_does_not_import_plugins():
    """`plugins` imports `tasks`, and `tasks` imports `store`. The mapping is handed in
    by the caller instead, and this keeps it that way."""
    src = (pathlib.Path(store.__file__)).read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [a.name for a in node.names]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        else:
            continue
        assert not any("plugins" in n for n in names), ast.unparse(node)


def test_the_cli_and_the_server_hand_the_mapping_in():
    """Every connect in the two entry points goes through the wrapper that passes
    `description_sources()`; a bare `store.connect` would leave job-board rows unlabelled.
    """
    from jobtracker import cli, server

    for module in (cli, server):
        src = pathlib.Path(module.__file__).read_text()
        bare = [line for line in src.splitlines()
                if "store.connect(" in line and "description_sources=" not in line
                and not line.strip().startswith(('"""', "`", "#"))]
        assert bare == [], (module.__name__, bare)
