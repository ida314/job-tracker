"""End-to-end wiring of the run loop against an in-memory DB (no network).

Mirrors exactly what cmd_check does per company, then renders the report — so the
health -> store -> match -> report glue is covered without hitting live boards.
"""

from jobtracker import config, report, store
from jobtracker.criteria import load_criteria
from jobtracker.health import evaluate
from jobtracker.match import match
from jobtracker.models import Company, FetchResult, Posting


def _run(conn, company, result, criteria, today):
    prior = store.get_health(conn, company.name)
    ever = store.ever_nonempty(conn, company.name)
    health = evaluate(company, result, prior, today, ever)
    store.upsert_health(conn, health, today)
    if health.status.value == "ok":
        store.sync_postings(conn, company.name, result.postings, today)
        for p in result.postings:
            store.record_verdict(conn, match(p, criteria), today)
    conn.commit()


def test_full_loop_report():
    conn = store.connect(":memory:")
    criteria = load_criteria(config.CRITERIA_YAML)
    companies = [
        Company("Acme", "greenhouse", "acme", tier=1, check_method="api"),
        Company("DeadCo", "greenhouse", "dead", tier=2, check_method="api"),
        Company("HandCo", "workday", "", tier=3, check_method="manual",
                notes="check the workday tenant"),
    ]

    acme = FetchResult("Acme", "greenhouse", "acme", ok=True, status_code=200,
                       observed_board_name="Acme",
                       postings=[
                           Posting("Acme", "1", "Software Engineer, New Grad", "u1", "NYC"),
                           Posting("Acme", "2", "Senior Staff Engineer", "u2", "NYC"),
                           Posting("Acme", "3", "Platform Engineer", "u3", "NYC"),
                       ])
    dead = FetchResult("DeadCo", "greenhouse", "dead", ok=False, status_code=404,
                       error="HTTP 404")

    _run(conn, companies[0], acme, criteria, "2026-07-20")
    _run(conn, companies[1], dead, criteria, "2026-07-20")

    text = report.build_report(conn, companies, "2026-07-20", since="2026-07-20")

    assert "## New matches (1)" in text
    assert "Software Engineer, New Grad" in text
    assert "Senior Staff Engineer" not in text  # rejected
    assert "## Uncertain — needs a human (1)" in text
    assert "Platform Engineer" in text
    assert "`fetch_failed`" in text and "DeadCo" in text
    assert "HandCo" in text  # manual, surfaced

    counts = store.counts_by_verdict(conn)
    assert counts == {"match": 1, "reject": 1, "uncertain": 1}


# -- description caching -----------------------------------------------------------
# `check` caches descriptions so every downstream pass is offline with respect to the
# ATSes. These pin the three rules that make that affordable and safe.
class _FakeFetcher:
    """Records what was requested; answers with a description and a posted date."""

    def __init__(self, fail=()):
        self.requested = []
        self.fail = set(fail)

    def fetch_job_detail(self, company, ats_job_id):
        self.requested.append(ats_job_id)
        if ats_job_id in self.fail:
            return None, None
        return f"description for {ats_job_id}", "2023-11-01T00:00:00-04:00"


def _cache(conn, fetcher, wanted, budget=100, today="2026-08-02"):
    from jobtracker.cli import _cache_descriptions

    _cache_descriptions(conn, fetcher, wanted, budget, today)


def _gh(jid, description=""):
    company = Company(name="Acme", ats="greenhouse", slug="acme")
    return company, Posting("Acme", jid, "Software Engineer", "u", description=description)


def test_bulk_descriptions_are_free_and_cost_no_request():
    """Ashby and Lever ship descriptionPlain in the list call — never refetch those."""
    conn = store.connect(":memory:")
    company, posting = _gh("1", description="already here")
    store.sync_postings(conn, "Acme", [posting], "2026-08-02")
    f = _FakeFetcher()
    _cache(conn, f, [(company, posting)])
    assert f.requested == []
    assert store.get_description(conn, "Acme", "1") == "already here"


def test_greenhouse_description_is_fetched_once_ever():
    conn = store.connect(":memory:")
    company, posting = _gh("1")
    store.sync_postings(conn, "Acme", [posting], "2026-08-02")

    f = _FakeFetcher()
    _cache(conn, f, [(company, posting)])
    assert f.requested == ["1"]
    assert store.get_description(conn, "Acme", "1") == "description for 1"
    # The detail payload is also where first_published lives — take it while there.
    assert conn.execute("SELECT posted_on FROM postings").fetchone()[0] == "2023-11-01"

    # Second run: already stored, so no second request.
    f2 = _FakeFetcher()
    _cache(conn, f2, [(company, posting)])
    assert f2.requested == []


def test_budget_caps_requests_and_defers_the_rest():
    """A bad night must not turn a 30-second job into a 40-minute one."""
    conn = store.connect(":memory:")
    wanted = []
    for jid in "12345":
        company, posting = _gh(jid)
        store.sync_postings(conn, "Acme", [posting], "2026-08-02")
        wanted.append((company, posting))

    f = _FakeFetcher()
    _cache(conn, f, wanted, budget=2)
    assert len(f.requested) == 2
    stored = conn.execute(
        "SELECT COUNT(*) FROM postings WHERE description IS NOT NULL"
    ).fetchone()[0]
    assert stored == 2  # the other three are simply picked up tomorrow


def test_a_failed_description_leaves_the_row_retryable_and_raises_nothing():
    """A 500 on one job detail is not a broken board.

    It must not raise, must not mark anything unhealthy, and must leave description
    NULL — the sentinel meaning "never fetched" — so tomorrow tries again.
    """
    conn = store.connect(":memory:")
    company, posting = _gh("1")
    store.sync_postings(conn, "Acme", [posting], "2026-08-02")
    _cache(conn, _FakeFetcher(fail={"1"}), [(company, posting)])
    assert store.get_description(conn, "Acme", "1") is None


def test_a_source_with_no_detail_endpoint_is_not_retried_forever():
    """The aggregator feeds have no per-posting page, so a fetch cannot exist.

    Leaving description NULL meant every run re-attempted them and counted each as a
    failure: 249 aggregator postings a night, with no request ever made. Recording ''
    — the documented "fetched and genuinely empty" sentinel — retires them.
    """
    conn = store.connect(":memory:")
    company = Company(name="Simplify", ats="aggregator", slug="",
                      check_method="aggregator")
    posting = Posting("Simplify", "abc", "NVIDIA — Backend Engineer, New Grad", "u")
    store.sync_postings(conn, "Simplify", [posting], "2026-08-02")

    f = _FakeFetcher()
    _cache(conn, f, [(company, posting)])
    assert f.requested == []                                  # nothing was attempted
    assert store.get_description(conn, "Simplify", "abc") == ""   # and not NULL

    # A second run must not reconsider it at all.
    f2 = _FakeFetcher()
    _cache(conn, f2, [(company, posting)])
    assert f2.requested == []


# -- import plugins: the switch, end to end ------------------------------------------
class _StubFetcher:
    """One page of Discord-shaped messages, then nothing."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    def fetch_json(self, url, headers=None):
        self.calls += 1
        return (200, self.pages.pop(0), None) if self.pages else (200, [], None)


def _discord_message(mid, content, bot=True):
    return {
        "id": str(mid),
        "timestamp": "2026-08-30T12:00:00+00:00",
        "author": {"bot": bot},
        "content": content,
        "embeds": [],
    }


_SAMPLE = (
    "## [Software Developer Associate @ Artera]"
    "(<https://jobs.lever.co/artera-2/eae88c70>)\n"
    "### Locations:  Seattle, WA\nPosted on: July 31, 2026"
)


def _plugin_run(conn, fetcher, active, today="2026-08-31"):
    from jobtracker import cli

    stats = {"companies": 0, "ok": 0, "failed": 0, "new_postings": 0, "matches": 0}
    degraded: list = []
    cli._run_plugins(
        conn, fetcher, active, load_criteria(config.CRITERIA_YAML),
        store.load_overrides(conn), today, stats, degraded,
    )
    conn.commit()
    return stats, degraded


def _enabled_discord():
    from jobtracker.plugins import get_plugin

    settings = {
        "enabled": True, "channel_id": "111", "guild_id": "222",
        "label": "jobs", "backfill_days": 14, "expire_after_days": 90,
    }
    return [(get_plugin("discord"), settings)]


def test_a_discord_posting_travels_all_the_way_to_a_verdict_and_a_description():
    conn = store.connect(":memory:")
    fetcher = _StubFetcher([[_discord_message(1300000000000000001, _SAMPLE)]])
    stats, degraded = _plugin_run(conn, fetcher, _enabled_discord())

    assert stats["new_postings"] == 1 and degraded == []
    row = conn.execute(
        "SELECT p.title, p.posted_on, p.description, v.verdict FROM postings p "
        "JOIN verdicts v ON v.company=p.company AND v.ats_job_id=p.ats_job_id"
    ).fetchone()
    assert row["title"] == "Artera — Software Developer Associate"
    assert row["posted_on"] == "2026-07-31"  # the bot's date, not the message's
    assert row["verdict"] == "match"
    # Without a description the posting is never judged, never scored, and rank.available
    # filters it out — in the table, absent from the product.
    assert row["description"]


def test_a_quiet_night_closes_nothing_a_plugin_imported():
    conn = store.connect(":memory:")
    _plugin_run(conn, _StubFetcher([[_discord_message(1300000000000000001, _SAMPLE)]]),
                _enabled_discord())
    _plugin_run(conn, _StubFetcher([]), _enabled_discord(), today="2026-09-01")
    assert conn.execute(
        "SELECT COUNT(*) FROM postings WHERE closed_at IS NULL"
    ).fetchone()[0] == 1


def test_disabling_a_plugin_leaves_its_postings_exactly_where_they_were():
    """The whole point of the switch: it stops reading and changes nothing else."""
    conn = store.connect(":memory:")
    _plugin_run(conn, _StubFetcher([[_discord_message(1300000000000000001, _SAMPLE)]]),
                _enabled_discord())
    before = [dict(r) for r in conn.execute("SELECT * FROM postings ORDER BY ats_job_id")]
    verdicts_before = [dict(r) for r in conn.execute("SELECT * FROM verdicts")]

    # Disabled: `_active_plugins` yields nothing, so the loop never runs.
    fetcher = _StubFetcher([[_discord_message(1300000000000000002, _SAMPLE)]])
    _plugin_run(conn, fetcher, [], today="2026-09-01")

    assert fetcher.calls == 0
    assert [dict(r) for r in conn.execute("SELECT * FROM postings ORDER BY ats_job_id")] == before
    assert [dict(r) for r in conn.execute("SELECT * FROM verdicts")] == verdicts_before


def test_re_enabling_resumes_from_the_stored_cursor_and_imports_nothing_twice():
    conn = store.connect(":memory:")
    page = [_discord_message(1300000000000000001, _SAMPLE)]
    _plugin_run(conn, _StubFetcher([page]), _enabled_discord())
    cursor = store.get_plugin_state(conn, "discord")
    assert cursor == {"cursor": "1300000000000000001"}

    _plugin_run(conn, _StubFetcher([page]), _enabled_discord(), today="2026-09-01")
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1


def test_a_failed_read_leaves_the_cursor_and_degrades_the_run():
    class _Broken:
        def fetch_json(self, url, headers=None):
            return (401, None, "HTTP 401")

    conn = store.connect(":memory:")
    stats, degraded = _plugin_run(conn, _Broken(), _enabled_discord())
    assert degraded and stats["new_postings"] == 0
    assert store.get_plugin_state(conn, "discord") == {}
    assert conn.execute(
        "SELECT last_status FROM board_health WHERE company LIKE 'Discord%'"
    ).fetchone()[0] == "fetch_failed"


def test_an_announcement_of_a_job_already_tracked_is_never_imported():
    conn = store.connect(":memory:")
    store.sync_postings(
        conn, "Artera",
        [Posting("Artera", "eae88c70", "Software Developer Associate",
                 "https://jobs.lever.co/artera-2/eae88c70")],
        "2026-08-01", identity=("lever", "artera-2"),
    )
    _plugin_run(conn, _StubFetcher([[_discord_message(1300000000000000001, _SAMPLE)]]),
                _enabled_discord())
    assert conn.execute(
        "SELECT COUNT(*) FROM postings WHERE company LIKE 'Discord%'"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT closed_at FROM postings").fetchone()[0] is None


def test_with_no_plugins_configured_the_run_reads_no_feed_at_all(tmp_path):
    from jobtracker import cli

    assert cli._active_plugins(tmp_path / "absent.yaml") == []


def test_a_malformed_plugins_file_stops_the_feed_rather_than_reading_as_no_plugins(tmp_path):
    """Silently reading it as "nothing configured" would turn a typo into a feed that
    stopped importing with no error anywhere."""
    from jobtracker import cli

    path = tmp_path / "plugins.yaml"
    path.write_text("discord:\n  enabled: yes-please\n")
    assert cli._active_plugins(path) == []


def test_an_enabled_plugin_with_no_token_is_not_read(tmp_path):
    from jobtracker import cli
    from jobtracker.plugins import discord as dmod

    path = tmp_path / "plugins.yaml"
    path.write_text("discord:\n  enabled: true\n  channel_id: '111'\n")
    original = dmod.config.DISCORD_TOKEN
    dmod.config.DISCORD_TOKEN = None
    try:
        assert cli._active_plugins(path) == []
    finally:
        dmod.config.DISCORD_TOKEN = original
