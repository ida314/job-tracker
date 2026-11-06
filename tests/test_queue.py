"""`queue.lane`: the one classification of an uncertain row."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jobtracker import config, queue, store
from jobtracker.criteria import load_criteria
from jobtracker.models import Company
from jobtracker.plugins import get_plugin
from jobtracker.queue import Lane

PACKAGE = Path(queue.__file__).resolve().parent


@pytest.fixture(scope="module")
def criteria():
    return load_criteria(config.CRITERIA_YAML)


def _coverage():
    simplify = get_plugin("simplify")
    yc = get_plugin("ycombinator")
    return {simplify.name: simplify.coverage(), yc.name: yc.coverage()}


COMPANIES = {
    "Acme": Company(name="Acme", ats="greenhouse", slug="acme", tier=1),
    "Target Tech": Company(name="Target Tech", ats="workday", slug="target/wd5/x",
                           tier=4, board_scope="mixed"),
}


def _row(title, company="Acme", origin=None, location="New York, NY",
         first_seen="2026-11-01"):
    return {"company": company, "ats_job_id": "1", "title": title, "origin": origin,
            "location": location, "first_seen": first_seen}


def _lane(criteria, row, reading=None, targets=("entry",)):
    return queue.lane(row, criteria, COMPANIES, _coverage(), reading, targets=targets)


# -- one test per lane -------------------------------------------------------------
def test_feed_lane(criteria):
    row = _row("Simplify — Software Engineer", company="Simplify New-Grad-Positions",
               origin="simplify")
    assert _lane(criteria, row) is Lane.FEED


def test_read_unclear_lane(criteria):
    assert _lane(criteria, _row("Software Engineer"), {"band": "unclear"}) is Lane.READ_UNCLEAR


def test_signal_lane(criteria):
    assert _lane(criteria, _row("Software Engineer")) is Lane.SIGNAL


def test_no_signal_lane(criteria):
    assert _lane(criteria, _row("Barista")) is Lane.NO_SIGNAL


def test_board_noise_lane(criteria):
    assert _lane(criteria, _row("Barista", company="Target Tech")) is Lane.BOARD_NOISE


def test_signal_on_a_mixed_board_is_not_noise(criteria):
    # board_scope folds only the no-signal part of a mixed board.
    assert _lane(criteria, _row("Software Engineer", company="Target Tech")) is Lane.SIGNAL


# -- precedence --------------------------------------------------------------------
def test_feed_outranks_an_unclear_reading(criteria):
    row = _row("Software Engineer", company="Simplify New-Grad-Positions", origin="simplify")
    assert _lane(criteria, row, {"band": "unclear"}) is Lane.FEED


def test_no_signal_outranks_the_feed(criteria):
    row = _row("Account Manager", company="Simplify New-Grad-Positions", origin="simplify")
    assert _lane(criteria, row) is Lane.NO_SIGNAL


def test_feed_lane_requires_target_band(criteria):
    row = _row("Software Engineer", company="Simplify New-Grad-Positions", origin="simplify")
    assert _lane(criteria, row, targets=("mid",)) is Lane.SIGNAL


def test_a_board_curated_to_nothing_is_not_a_feed_lane(criteria):
    row = _row("Software Engineer", company="YC", origin="ycombinator")
    assert _lane(criteria, row) is Lane.SIGNAL


def test_plugin_group_with_no_curation_does_not_crash(criteria):
    # A job board's group is never in `load_companies`, and an origin no longer
    # registered has no coverage. Both must degrade, not raise.
    row = _row("Barista", company="Simplify New-Grad-Positions", origin="simplify")
    assert _lane(criteria, row) is Lane.NO_SIGNAL
    gone = _row("Software Engineer", company="Old Feed", origin="removed-plugin")
    assert _lane(criteria, gone) is Lane.SIGNAL
    assert queue.lane(gone, criteria, {}, {}, None) is Lane.SIGNAL


# -- labels and ordering -----------------------------------------------------------
def test_labels():
    assert queue.label(Lane.FEED, "entry") == "listed on a new-grad feed"
    assert queue.label(Lane.READ_UNCLEAR) == "read — the description does not say"
    assert queue.label(Lane.SIGNAL) == "not read yet"
    assert queue.label(Lane.NO_SIGNAL) == "no field signal in the title"
    assert queue.label(Lane.BOARD_NOISE) == "board lists many non-field roles"
    assert queue.label(Lane.FEED, "mid") == "listed on a mid feed"
    assert queue.label(Lane.FEED)  # never empty
    for ln in Lane:
        assert queue.label(ln.value)  # accepts the stored-string form too


def test_new_grad_is_written_in_one_table():
    source = (PACKAGE / "queue.py").read_text()
    assert source.count('"new-grad"') == 1


def test_sort_key_orders_lane_then_location_then_newest(criteria):
    rows = [
        (_row("Barista", first_seen="2026-11-05"), Lane.NO_SIGNAL),
        (_row("Software Engineer", location="Berlin, Germany"), Lane.SIGNAL),
        (_row("Software Engineer", first_seen="2026-10-01"), Lane.SIGNAL),
        (_row("Software Engineer", first_seen="2026-11-03"), Lane.SIGNAL),
        (_row("Software Engineer", first_seen="garbage"), Lane.SIGNAL),
        (_row("Software Engineer", origin="simplify"), Lane.FEED),
    ]
    ordered = sorted(rows, key=lambda rl: queue.sort_key(rl[0], rl[1], criteria))
    seen = [(r["first_seen"], r["location"], ln) for r, ln in ordered]
    assert seen == [
        ("2026-11-01", "New York, NY", Lane.FEED),
        ("2026-11-03", "New York, NY", Lane.SIGNAL),
        ("2026-10-01", "New York, NY", Lane.SIGNAL),
        ("garbage", "New York, NY", Lane.SIGNAL),  # unreadable sorts last, not "today"
        ("2026-11-01", "Berlin, Germany", Lane.SIGNAL),
        ("2026-11-05", "New York, NY", Lane.NO_SIGNAL),
    ]


# -- the module's two structural rules ---------------------------------------------
def test_lane_is_pure():
    source = (PACKAGE / "queue.py").read_text()
    for banned in ("sqlite3", "open(", "datetime.now"):
        assert banned not in source, banned


def test_lane_is_the_only_derivation():
    # A second table would hold the label as a string literal, so that is what is
    # looked for: prose that happens to say "not read yet" is not a copy of the lane.
    labels = [queue.label(ln) for ln in Lane if ln is not Lane.FEED]
    literals = [q + s + q for s in labels for q in ('"', "'")]
    literals += ['"listed on a', "'listed on a", 'f"listed on a', "f'listed on a"]
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        if path.name == "queue.py" and path.parent == PACKAGE:
            continue
        text = path.read_text()
        offenders += [f"{path.name}: {s}" for s in literals if s in text]
    assert not offenders
    # and the test can see what it is looking for
    queue_source = (PACKAGE / "queue.py").read_text()
    assert all(f'"{s}"' in queue_source for s in labels)


def test_looks_engineering_is_the_only_field_test():
    source = (PACKAGE / "queue.py").read_text()
    assert "looks_engineering(" in source
    assert not re.search(r"first_hit\(|hits\(", source)


# -- the store helper --------------------------------------------------------------
def test_uncertain_with_context(tmp_path, criteria):
    conn = store.connect(tmp_path / "state.db")
    rows = [
        ("Acme", "1", "Software Engineer", None, None, "uncertain", None),
        ("Acme", "2", "Barista", None, None, "uncertain", None),
        ("Simplify New-Grad-Positions", "3", "Simplify — Software Engineer",
         "simplify", "Simplify", "uncertain", None),
        ("Acme", "4", "Backend Engineer", None, None, "match", None),
        ("Acme", "5", "Software Engineer II", None, None, "uncertain", "2026-11-02"),
    ]
    for company, job, title, origin, employer, verdict, closed in rows:
        conn.execute(
            "INSERT INTO postings (company, ats_job_id, title, location, url, first_seen, "
            "last_seen, closed_at, origin, employer) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (company, job, title, "New York, NY", f"https://x/{job}", "2026-11-01",
             "2026-11-01", closed, origin, employer),
        )
        conn.execute(
            "INSERT INTO verdicts (company, ats_job_id, verdict, reason, decided_by, "
            "decided_at) VALUES (?,?,?,?,?,?)",
            (company, job, verdict, "r", "rules", "2026-11-01"),
        )
    store.record_level_reading(conn, "Acme", "1", "unclear", "", "abcd", "2026-11-02")
    conn.commit()

    got = {r["ats_job_id"]: r for r in store.uncertain_with_context(conn)}
    assert set(got) == {"1", "2", "3"}
    assert got["1"]["reading_band"] == "unclear"
    assert got["2"]["reading_band"] is None
    assert got["3"]["origin"] == "simplify" and got["3"]["employer"] == "Simplify"
    assert got["3"]["first_seen"] == "2026-11-01"

    lanes = {
        job: queue.lane(r, criteria, COMPANIES, _coverage(), r) for job, r in got.items()
    }
    assert lanes == {"1": Lane.READ_UNCLEAR, "2": Lane.NO_SIGNAL, "3": Lane.FEED}
