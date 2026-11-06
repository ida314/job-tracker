"""A changed description re-asks the answers read off it — for that posting only.

`judge`, `tailor` and `coverletter` each cache an answer per posting, and each was keyed
only on configuration (the profile prose, the resume, the keyword lists, the template).
A description is not write-once forever — an ATS fetch can replace a feed's blurb with
the real prose — so an answer read off the blurb outlived the text it was about. Each
table now records `description_hash` of the text the model read, and each pending query
compares it to the description stored now.

The other half is the upgrade: rows written before the column existed are stamped from
the current description on connect, so shipping this re-asks nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging

from jobtracker import store
from jobtracker.models import Decision, Posting, Verdict
from jobtracker.tasks import TaskContext, get_task
from jobtracker.tasks.runner import run_task

TODAY = "2026-09-01"
BLURB = "Backend engineer, new grad. Apply via the link."
PROSE = (
    "You will build distributed systems in Go and Kafka. New graduates welcome; "
    "you will be mentored by senior engineers on the storage team."
)


class _Judging:
    """A client that answers `judge` with a well-formed judgment, and counts calls."""

    def __init__(self):
        self.calls = 0

    async def complete(self, *_a, **_k):
        self.calls += 1
        return json.dumps({
            "backend_fit": "strong", "growth": "moderate", "entry_risk": "low",
            "why": "backend work",
        })


class _Profile:
    """The one attribute `judge` reads off a profile in pending/apply."""

    prose = "backend"
    prose_hash = "prose-1"


class _Template:
    hash = "template-1"


def _ctx():
    return TaskContext(
        today=TODAY, profile=_Profile(), resume_hash="resume-1",
        letter_template=_Template(),
    )


def _seed(conn, descriptions: dict):
    """Open matches with the given descriptions, each ranked and scored.

    One `sync_postings` call: it closes anything absent from the list it is handed.
    """
    postings = [
        Posting("Acme", jid, "Backend Engineer", f"https://x/{jid}")
        for jid in descriptions
    ]
    store.sync_postings(conn, "Acme", postings, TODAY)
    for jid, text in descriptions.items():
        store.set_description(conn, "Acme", jid, text, "ats")
        store.record_verdict(conn, Verdict("Acme", jid, Decision.MATCH, "r", "rules"), TODAY)
    conn.commit()


def _answer_everything(conn, jids):
    """What a night of `work` leaves behind: a judgment, a proposal and a letter each."""

    class _J:
        backend_fit, growth, entry_risk, why = "strong", "strong", "low", ""

    for jid in jids:
        store.record_judgment(conn, "Acme", jid, _J(), "prose-1", TODAY)
        store.set_score(conn, "Acme", jid, 80.0, TODAY)
        store.record_suggestions(
            conn, "Acme", jid, "[]", "resume-1", TODAY, keywords_hash=_keywords_hash(),
        )
        store.record_letter(conn, "Acme", jid, "[]", _letter_key(), TODAY)
    conn.commit()


def _keywords_hash():
    from jobtracker.tasks import tailor

    return tailor._keywords(_ctx()).hash


def _letter_key():
    from jobtracker.tasks import coverletter

    return coverletter._unit_key(_ctx())


def _pending(conn, task):
    return {u.ats_job_id: u for u in get_task(task).pending(conn, _ctx())}


def test_the_hash_is_one_function_in_python_and_in_sql():
    """The writers hash in Python and the pending queries hash in SQL; if the two ever
    disagreed, every posting would read as changed on every night."""
    conn = store.connect(":memory:")
    assert store.description_hash(None) == ""
    assert store.description_hash("") == ""
    assert len(store.description_hash(PROSE)) == 16
    got = conn.execute("SELECT description_hash(?)", (PROSE,)).fetchone()[0]
    assert got == store.description_hash(PROSE)
    assert conn.execute("SELECT description_hash(NULL)").fetchone()[0] == ""
    conn.close()


def test_description_upgrade_rejudges():
    """Judged on a feed's blurb, then the real prose arrives: that posting is asked
    again, as a new unit, and answering it drains the queue."""
    conn = store.connect(":memory:")
    _seed(conn, {"1": BLURB})
    client = _Judging()

    asyncio.run(run_task(conn, get_task("judge"), client, _ctx()))
    assert client.calls == 1
    assert _pending(conn, "judge") == {}
    row = conn.execute("SELECT description_hash FROM rankings").fetchone()
    assert row["description_hash"] == store.description_hash(BLURB)

    store.set_description(conn, "Acme", "1", PROSE, "ats")
    conn.commit()
    units = _pending(conn, "judge")
    assert list(units) == ["1"]
    assert units["1"].unit_key == f"prose-1:{store.description_hash(PROSE)}"
    assert units["1"].payload["description"] == PROSE

    asyncio.run(run_task(conn, get_task("judge"), client, _ctx()))
    assert client.calls == 2
    assert _pending(conn, "judge") == {}
    conn.close()


def test_upgrade_does_not_mass_rejudge_existing_rankings(tmp_path, caplog):
    """A database from before the column: every answer is stamped from the current
    description on connect, so nothing re-asks — and the count is said in the log."""
    db = tmp_path / "state.db"
    conn = store.connect(db)
    _seed(conn, {"1": PROSE, "2": BLURB})
    _answer_everything(conn, ["1", "2"])
    # Roll the schema back to before this change existed.
    for table in ("rankings", "resume_suggestions", "cover_letters"):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN description_hash")
    conn.commit()
    conn.close()

    with caplog.at_level(logging.INFO, logger="jobtracker.store"):
        conn = store.connect(db)
    assert any("stamped 6 existing" in r.getMessage() for r in caplog.records)
    for task in ("judge", "tailor", "coverletter"):
        assert _pending(conn, task) == {}, task
    conn.close()

    # Self-draining: the second connect has nothing to stamp and says nothing.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="jobtracker.store"):
        conn = store.connect(db)
    assert not any("stamped" in r.getMessage() for r in caplog.records)

    # And the stamp is a real one: a later change still re-asks.
    store.set_description(conn, "Acme", "2", PROSE + " Updated.", "ats")
    conn.commit()
    for task in ("judge", "tailor", "coverletter"):
        assert list(_pending(conn, task)) == ["2"], task
    conn.close()


def test_tailor_unit_pending_again_after_description_change():
    """A proposal grounded in a description that has since moved is re-asked, with the
    unit key carrying the new hash, and storing the answer drains it again."""
    conn = store.connect(":memory:")
    _seed(conn, {"1": BLURB})
    _answer_everything(conn, ["1"])
    assert _pending(conn, "tailor") == {}

    store.set_description(conn, "Acme", "1", PROSE, "ats")
    conn.commit()
    units = _pending(conn, "tailor")
    assert list(units) == ["1"]
    assert units["1"].unit_key.endswith(f":{store.description_hash(PROSE)}")

    store.record_suggestions(
        conn, "Acme", "1", "[]", "resume-1", TODAY, keywords_hash=_keywords_hash(),
        description_hash=store.description_hash(PROSE),
    )
    conn.commit()
    assert _pending(conn, "tailor") == {}
    conn.close()


def test_other_postings_not_rekeyed():
    """The description is the one per-posting part of each key. Changing one posting's
    description moves that posting's unit and leaves every other unit — and its retry
    count — exactly where it was."""
    conn = store.connect(":memory:")
    _seed(conn, {"1": BLURB, "2": PROSE})
    # Scored but unanswered, so every posting is pending in all three tasks.
    for jid in ("1", "2"):
        conn.execute(
            "INSERT INTO rankings (company, ats_job_id, backend_fit, growth, entry_risk,"
            " why, prose_hash, judged_at, score, scored_at, description_hash) "
            "VALUES ('Acme', ?, 'strong', 'strong', 'low', '', 'old-prose', ?, 50, ?, '')",
            (jid, TODAY, TODAY),
        )
    conn.commit()

    before = {t: _pending(conn, t) for t in ("judge", "tailor", "coverletter")}
    store.set_description(conn, "Acme", "1", PROSE + " And more.", "ats")
    conn.commit()
    after = {t: _pending(conn, t) for t in ("judge", "tailor", "coverletter")}

    for task in before:
        assert set(before[task]) == set(after[task]) == {"1", "2"}, task
        assert after[task]["2"].unit_key == before[task]["2"].unit_key, task
        assert after[task]["1"].unit_key != before[task]["1"].unit_key, task

    # And with answers on file, only the changed posting comes back.
    _answer_everything(conn, ["1", "2"])
    store.set_description(conn, "Acme", "1", PROSE + " Once more.", "ats")
    conn.commit()
    for task in before:
        assert list(_pending(conn, task)) == ["1"], task
    conn.close()


def test_a_unit_set_aside_on_the_blurb_is_retried_on_the_prose():
    """Why the hash is in `unit_key` and not only in the pending query: three failures
    against a feed's two-line blurb say nothing about the real description, and the
    set-aside ledger is keyed on `unit_key`."""
    from jobtracker.tasks.base import MAX_ATTEMPTS

    conn = store.connect(":memory:")
    _seed(conn, {"1": BLURB})
    (unit,) = get_task("judge").pending(conn, _ctx())
    for _ in range(MAX_ATTEMPTS):
        store.record_attempt(
            conn, "judge", unit.company, unit.ats_job_id, unit.unit_key, "no_answer", TODAY
        )
    conn.commit()
    assert unit.ident in store.blocked_units(conn, "judge", MAX_ATTEMPTS)

    store.set_description(conn, "Acme", "1", PROSE, "ats")
    conn.commit()
    (fresh,) = get_task("judge").pending(conn, _ctx())
    assert fresh.ident not in store.blocked_units(conn, "judge", MAX_ATTEMPTS)
    conn.close()
