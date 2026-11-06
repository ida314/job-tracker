"""`check` upgrades a feed's metadata stub to the linked posting's own prose.

Simplify writes `Category: … / Locations: …` as a description (`description_source=
'feed'`). Where its link points straight at a Greenhouse, Lever or Ashby posting, `check`
fetches that posting's public JSON once, through the same adapters and limiter as the
board sweep, and overwrites the stub — the one exception to write-once, `feed` → `ats`.

The fetcher is faked everywhere here; nothing opens a socket.
"""

import argparse
import json
import logging

from jobtracker import cli, config, store
from jobtracker.criteria import load_criteria
from jobtracker.dedupe import dedupe_key, upgrade_target
from jobtracker.models import FetchResult, Posting

TODAY = "2026-09-15"
GROUP = "Simplify New-Grad-Positions"
STUB = "Category: Software\nLocations: New York, NY"

LEVER_ID = "e0670581-cfb1-4400-be91-8813416f95ca"
ASHBY_A = "3f2c1d9e-1111-4a2b-9c3d-0123456789ab"
ASHBY_B = "3f2c1d9e-2222-4a2b-9c3d-0123456789ab"
ASHBY_C = "3f2c1d9e-3333-4a2b-9c3d-0123456789ab"


class _FakeFetcher:
    """Answers the upgrade pass's two calls from tables, and records every request."""

    def __init__(self, details=None, boards=None, raise_on=()):
        self.details = details or {}  # (slug, job_id) -> text | None
        self.boards = boards or {}    # (ats, slug) -> [(job_id, description)] | None
        self.raise_on = set(raise_on)
        self.detail_calls: list = []
        self.board_calls: list = []

    def fetch_job_detail(self, company, ats_job_id):
        self.detail_calls.append((company.ats, company.slug, ats_job_id))
        if "detail" in self.raise_on:
            raise RuntimeError("boom")
        return self.details.get((company.slug, ats_job_id)), None

    def fetch_company(self, company):
        self.board_calls.append((company.ats, company.slug))
        if "board" in self.raise_on:
            raise RuntimeError("boom")
        jobs = self.boards.get((company.ats, company.slug))
        res = FetchResult(company=company.name, ats=company.ats, slug=company.slug)
        if jobs is None:
            res.error = "HTTP 404"
            res.status_code = 404
            return res
        res.ok = True
        res.status_code = 200
        res.postings = [
            Posting(company.name, jid, "SWE", f"https://x/{jid}", description=text)
            for jid, text in jobs
        ]
        return res


def _feed_rows(conn, rows, source="feed"):
    """Feed postings with a verdict each, the way `_run_plugins` leaves them."""
    from jobtracker.match import match
    from jobtracker.tuning import apply_override

    criteria = load_criteria(config.CRITERIA_YAML)
    postings = [
        Posting(GROUP, jid, title, url, location="New York, NY", description=STUB)
        for jid, title, url in rows
    ]
    store.append_postings(conn, GROUP, postings, TODAY, origin="simplify",
                          description_source=source)
    overrides = store.load_overrides(conn)
    for p in postings:
        store.record_verdict(conn, apply_override(match(p, criteria), overrides), TODAY)
    conn.commit()


def _row(conn, jid):
    return conn.execute(
        "SELECT description, description_source FROM postings WHERE company=? "
        "AND ats_job_id=?", (GROUP, jid),
    ).fetchone()


# -- resolving the link ------------------------------------------------------------
def test_upgrade_resolves_lever_slug_from_url_case_preserved():
    url = f"https://jobs.lever.co/Onehouse/{LEVER_ID}/apply"
    assert upgrade_target(url) == ("lever", "Onehouse", LEVER_ID)
    # The key lowercases the slug, which is exactly why the upgrade never parses it:
    # `lever/onehouse` 404s.
    assert dedupe_key(url) == f"lever:onehouse:{LEVER_ID}"

    # And the slug that reaches the adapter is the one the feed wrote.
    conn = store.connect(":memory:")
    _feed_rows(conn, [("s1", "Onehouse — Software Engineer, New Grad", url)])
    fetcher = _FakeFetcher(boards={("lever", "Onehouse"): [(LEVER_ID, "We build lakes.")]})
    cli._upgrade_descriptions(conn, fetcher, budget=10)
    assert fetcher.board_calls == [("lever", "Onehouse")]
    assert tuple(_row(conn, "s1")) == ("We build lakes.", "ats")


def test_upgrade_target_shapes():
    gh = "https://job-boards.greenhouse.io/Stripe/jobs/7184616?gh_src=x"
    assert upgrade_target(gh) == ("greenhouse", "Stripe", "7184616")
    assert upgrade_target("https://boards.greenhouse.io/acme/jobs/42") == (
        "greenhouse", "acme", "42")
    assert upgrade_target(
        "https://boards.greenhouse.io/embed/job_app?for=Acme&token=99"
    ) == ("greenhouse", "Acme", "99")
    assert upgrade_target(f"https://jobs.ashbyhq.com/Ramp/{ASHBY_A}/application") == (
        "ashby", "Ramp", ASHBY_A)

    for url in (
        "https://careers.withwaymo.com/jobs?gh_jid=7488508",   # careers site
        "https://grnh.se/7pt3sv7q2us",                          # shortener
        "https://job-boards.eu.greenhouse.io/agency/jobs/4754242101",  # EU API host
        f"https://jobs.eu.lever.co/acme/{LEVER_ID}",            # EU API host
        "https://boards.greenhouse.io/acme/jobs/not-a-number",  # not a Greenhouse id
        "https://jobs.lever.co/acme/apply",                     # not a Lever id
        "https://boards.greenhouse.io/embed/job_app?for=..&token=1",  # path traversal
        "https://boards.greenhouse.io/embed/job_app?token=1",   # no board
        "https://simplify.jobs/p/abc",
        "javascript:alert(1)",
        "",
    ):
        assert upgrade_target(url) is None, url


# -- the pass ------------------------------------------------------------------------
def test_upgrade_overwrites_only_feed_text():
    conn = store.connect(":memory:")
    gh = "https://boards.greenhouse.io/acme/jobs/{}"
    _feed_rows(conn, [("f", "Acme — Software Engineer, New Grad", gh.format(1))])
    _feed_rows(conn, [("a", "Acme — Software Engineer", gh.format(2))], source="feed")
    _feed_rows(conn, [("p", "Acme — Backend Engineer", gh.format(3))], source="prose")
    # `a` was upgraded on an earlier night.
    store.set_description(conn, GROUP, "a", "Earlier prose.", source="ats")
    conn.commit()

    fetcher = _FakeFetcher(details={("acme", "1"): "Real prose.",
                                    ("acme", "2"): "Newer.", ("acme", "3"): "Newer."})
    cli._upgrade_descriptions(conn, fetcher, budget=10)
    assert fetcher.detail_calls == [("greenhouse", "acme", "1")]  # never `a` or `p`
    assert tuple(_row(conn, "f")) == ("Real prose.", "ats")
    assert tuple(_row(conn, "a")) == ("Earlier prose.", "ats")
    assert tuple(_row(conn, "p")) == (STUB, "prose")

    # The upgraded row is `ats` now, so the next night asks nothing at all.
    again = _FakeFetcher(details={("acme", "1"): "Changed."})
    cli._upgrade_descriptions(conn, again, budget=10)
    assert again.detail_calls == []
    assert tuple(_row(conn, "f")) == ("Real prose.", "ats")


def test_upgrade_leaves_the_row_untouched_on_failure_404_or_empty(caplog):
    conn = store.connect(":memory:")
    _feed_rows(conn, [
        ("gh_fail", "Acme — Software Engineer", "https://boards.greenhouse.io/acme/jobs/1"),
        ("lv_gone", "Beta — Software Engineer", f"https://jobs.lever.co/beta/{LEVER_ID}"),
        ("ab_empty", "Gamma — Software Engineer",
         f"https://jobs.ashbyhq.com/gamma/{ASHBY_A}"),
        ("ab_404", "Delta — Software Engineer", f"https://jobs.ashbyhq.com/delta/{ASHBY_A}"),
        ("nowhere", "Eps — Software Engineer", "https://eps.example.com/jobs/1"),
        ("rejected", "Zeta — Account Executive", "https://boards.greenhouse.io/z/jobs/9"),
    ])
    fetcher = _FakeFetcher(
        details={("acme", "1"): None, ("z", "9"): "never asked"},
        boards={("lever", "beta"): [], ("ashby", "gamma"): [(ASHBY_A, "  ")]},
    )
    with caplog.at_level(logging.INFO, logger="jobtracker"):
        cli._upgrade_descriptions(conn, fetcher, budget=10)

    for jid in ("gh_fail", "lv_gone", "ab_empty", "ab_404", "nowhere", "rejected"):
        assert tuple(_row(conn, jid)) == (STUB, "feed"), jid
    # A reject is not in scope, and a link that resolves nowhere is never fetched.
    assert fetcher.detail_calls == [("greenhouse", "acme", "1")]
    assert sorted(fetcher.board_calls) == [
        ("ashby", "delta"), ("ashby", "gamma"), ("lever", "beta")]
    summary = [r.getMessage() for r in caplog.records if "upgraded" in r.getMessage()]
    assert summary == [
        "description upgrade: 0 upgraded, 2 failed or empty, 1 no longer on their "
        "board, 1 had no prose, 0 deferred to tomorrow, 1 link nowhere we can read "
        "(4 request(s))"
    ]
    assert any("provenance" in r.getMessage() and "not verified identity"
               in r.getMessage() for r in caplog.records)


def test_upgrade_respects_its_budget():
    conn = store.connect(":memory:")
    _feed_rows(conn, [
        (f"g{i}", "Acme — Software Engineer", f"https://boards.greenhouse.io/acme/jobs/{i}")
        for i in range(5)
    ] + [
        ("l1", "Beta — Software Engineer", f"https://jobs.lever.co/beta/{LEVER_ID}"),
    ])
    fetcher = _FakeFetcher(
        details={("acme", str(i)): f"prose {i}" for i in range(5)},
        boards={("lever", "beta"): [(LEVER_ID, "lever prose")]},
    )
    cli._upgrade_descriptions(conn, fetcher, budget=3)
    assert len(fetcher.detail_calls) + len(fetcher.board_calls) == 3
    upgraded = conn.execute(
        "SELECT COUNT(*) FROM postings WHERE description_source='ats'"
    ).fetchone()[0]
    assert upgraded == 3

    # The remainder is tomorrow's.
    tomorrow = _FakeFetcher(details=fetcher.details, boards=fetcher.boards)
    cli._upgrade_descriptions(conn, tomorrow, budget=100)
    assert len(tomorrow.detail_calls) + len(tomorrow.board_calls) == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM postings WHERE description_source='feed'"
    ).fetchone()[0] == 0

    # A zero budget fetches nothing.
    conn2 = store.connect(":memory:")
    _feed_rows(conn2, [("g", "Acme — Software Engineer",
                        "https://boards.greenhouse.io/acme/jobs/1")])
    none = _FakeFetcher(details={("acme", "1"): "x"})
    cli._upgrade_descriptions(conn2, none, budget=0)
    assert none.detail_calls == []


def test_ashby_board_fetched_once_per_slug():
    conn = store.connect(":memory:")
    _feed_rows(conn, [
        ("a", "Ramp — Software Engineer", f"https://jobs.ashbyhq.com/ramp/{ASHBY_A}"),
        ("b", "Ramp — Backend Engineer",
         f"https://jobs.ashbyhq.com/ramp/{ASHBY_B.upper()}/application"),
        ("c", "Ramp — Software Engineer, New Grad", f"https://jobs.ashbyhq.com/ramp/{ASHBY_C}"),
        ("o", "Other — Software Engineer", f"https://jobs.ashbyhq.com/other/{ASHBY_A}"),
    ])
    fetcher = _FakeFetcher(boards={
        ("ashby", "ramp"): [(ASHBY_A, "A prose"), (ASHBY_B, "B prose"), (ASHBY_C, "C")],
        ("ashby", "other"): [(ASHBY_A, "other prose")],
    })
    # Two requests is the whole cost: one per board, however many links point into it.
    cli._upgrade_descriptions(conn, fetcher, budget=2)
    assert sorted(fetcher.board_calls) == [("ashby", "other"), ("ashby", "ramp")]
    assert _row(conn, "a")[0] == "A prose"
    assert _row(conn, "b")[0] == "B prose"  # id matched case-insensitively
    assert _row(conn, "c")[0] == "C"
    assert _row(conn, "o")[0] == "other prose"


# -- inside `check` --------------------------------------------------------------------
class _CheckFetcher(_FakeFetcher):
    """Everything `cmd_check` asks of a Fetcher: one healthy board, one feed listing,
    and an upgrade pass where every request fails or raises."""

    instances: list = []

    def __init__(self, listing, board_postings, **kwargs):
        super().__init__(**kwargs)
        self.listing = listing
        self.board_postings = board_postings
        self.closed = False

    def fetch_all(self, companies):
        out = []
        for c in companies:
            res = FetchResult(company=c.name, ats=c.ats, slug=c.slug, ok=True,
                              status_code=200, observed_board_name=c.name)
            res.postings = [replace_company(p, c.name) for p in self.board_postings]
            out.append(res)
        return out

    def fetch_json(self, url, headers=None):
        return 200, self.listing, None

    def fetch_text(self, url, headers=None):
        return 200, json.dumps(self.listing), None

    def close(self):
        self.closed = True


def replace_company(p, name):
    from dataclasses import replace

    return replace(p, company=name)


def _check(tmp_path, monkeypatch, fetcher, max_upgrades=100):
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "- name: Acme\n  ats: greenhouse\n  slug: acme\n  tier: 1\n"
        "  check_method: api\n  expected_board_name: Acme\n"
    )
    plugins = tmp_path / "plugins.yaml"
    plugins.write_text("simplify:\n  enabled: true\n")
    monkeypatch.setattr(cli, "Fetcher", lambda *a, **k: fetcher)
    args = argparse.Namespace(
        companies=str(companies), criteria=str(config.CRITERIA_YAML),
        db=str(tmp_path / "state.db"), since=TODAY, output=str(tmp_path / "out.md"),
        max_descriptions=400, max_upgrades=max_upgrades, plugins=str(plugins),
    )
    return cli.cmd_check(args)


def _listing():
    def entry(i, url, title="Software Engineer, New Grad"):
        return {"id": f"s{i}", "title": title, "company_name": f"Co{i}", "url": url,
                "active": True, "is_visible": True, "locations": ["New York, NY"],
                "category": "Software", "date_posted": 1773268640}
    return [
        entry(1, "https://boards.greenhouse.io/coone/jobs/11"),
        entry(2, f"https://jobs.lever.co/CoTwo/{LEVER_ID}"),
        entry(3, f"https://jobs.ashbyhq.com/cothree/{ASHBY_A}"),
        entry(4, "https://cofour.example.com/careers/4"),
    ]


def test_upgrade_never_enters_wanted(tmp_path, monkeypatch):
    seen_wanted: list = []
    real_cache = cli._cache_descriptions

    def spy(conn, fetcher, wanted, budget, today):
        seen_wanted.extend(wanted)
        return real_cache(conn, fetcher, wanted, budget, today)

    monkeypatch.setattr(cli, "_cache_descriptions", spy)
    board = [Posting("Acme", "100", "Software Engineer, New Grad",
                     "https://boards.greenhouse.io/acme/jobs/100",
                     location="New York, NY")]
    fetcher = _CheckFetcher(
        _listing(), board,
        details={("acme", "100"): "Acme board prose.", ("coone", "11"): "GH prose."},
        boards={("lever", "CoTwo"): [(LEVER_ID, "Lever prose.")],
                ("ashby", "cothree"): [(ASHBY_A, "Ashby prose.")]},
    )
    assert _check(tmp_path, monkeypatch, fetcher) == cli.EXIT_OK

    # `wanted` is the board pass's list and nothing else.
    assert {(c.name, p.ats_job_id) for c, p in seen_wanted} == {("Acme", "100")}
    conn = store.connect(tmp_path / "state.db")
    got = {r["ats_job_id"]: (r["description"], r["description_source"])
           for r in conn.execute("SELECT * FROM postings WHERE company=?", (GROUP,))}
    assert got["s1"] == ("GH prose.", "ats")
    assert got["s2"] == ("Lever prose.", "ats")
    assert got["s3"] == ("Ashby prose.", "ats")
    assert got["s4"][1] == "feed" and got["s4"][0].startswith("Category")
    assert fetcher.closed


def test_upgrade_failure_is_invisible_to_health(tmp_path, monkeypatch):
    board = [Posting("Acme", "100", "Software Engineer, New Grad",
                     "https://boards.greenhouse.io/acme/jobs/100",
                     location="New York, NY",
                     description="Acme board prose.")]
    # Every upgrade request raises; the board and the feed are healthy.
    fetcher = _CheckFetcher(_listing(), board, raise_on={"detail", "board"})
    assert _check(tmp_path, monkeypatch, fetcher) == cli.EXIT_OK

    conn = store.connect(tmp_path / "state.db")
    statuses = {r["company"]: r["last_status"]
                for r in conn.execute("SELECT company, last_status FROM board_health")}
    assert statuses == {"Acme": "ok", GROUP: "ok"}
    # Nothing keyed on the upgraded boards' slugs was ever written as a board.
    assert not conn.execute(
        "SELECT 1 FROM board_health WHERE company LIKE '%:%'"
    ).fetchone()
    rows = list(conn.execute(
        "SELECT description_source FROM postings WHERE company=?", (GROUP,)))
    assert {r[0] for r in rows} == {"feed"}
