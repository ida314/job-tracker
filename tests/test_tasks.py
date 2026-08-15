"""The task queue: what gets picked, what gets committed, and what stops being retried.

Three properties are load-bearing and each has a test here:

* **Selection is the pipeline order.** level before judge before prefill, because each
  produces what the next consumes.
* **Containment.** Every unit commits by itself, so an interrupted run keeps everything
  that already landed. The passes this replaced lost a whole batch to one ^C.
* **Failure stays absence.** A unit that answers nothing, or raises, writes nothing —
  and after three consecutive failures it stops consuming budget.

No network anywhere. Clients are hand-written stubs, following the convention in
test_rank.py rather than pulling in unittest.mock.
"""

import asyncio
import json

import pytest

from jobtracker import config, store
from jobtracker.criteria import load_criteria
from jobtracker.models import Decision, Posting, Verdict
from jobtracker.profile import load_profile
from jobtracker.tasks import (
    MAX_ATTEMPTS,
    TaskContext,
    all_tasks,
    get_task,
    run_task,
    select,
    survey,
    task_names,
)
from jobtracker.tasks.base import Task, TaskUnit

TODAY = "2026-08-13"


@pytest.fixture(scope="module")
def criteria():
    return load_criteria(config.CRITERIA_YAML)


@pytest.fixture(scope="module")
def profile():
    return load_profile(config.PROFILE_YAML)


def _ctx(criteria=None, profile=None, **over):
    return TaskContext(today=TODAY, criteria=criteria, profile=profile, **over)


def _seed(conn, specs):
    """Seed several postings in one sync.

    One call, deliberately: `sync_postings` closes anything absent from the list it is
    given, so seeding row by row would quietly close every row seeded before it.
    """
    postings = [Posting("Acme", jid, title, f"https://x/{jid}") for jid, title, _, _ in specs]
    store.sync_postings(conn, "Acme", postings, TODAY)
    for jid, _title, description, verdict in specs:
        if description is not None:
            store.set_description(conn, "Acme", jid, description)
        store.record_verdict(conn, Verdict("Acme", jid, verdict, "r", "rules"), TODAY)
    conn.commit()


def _uncertain(conn, *jids, title="Backend Software Engineer", description="d"):
    _seed(conn, [(jid, title, description, Decision.UNCERTAIN) for jid in jids])


class _Answering:
    """A client that always returns the same well-formed answer."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def complete(self, *_a, **_k):
        self.calls += 1
        return json.dumps(self.payload)


class _Silent:
    async def complete(self, *_a, **_k):
        return None


class _Exploding:
    async def complete(self, *_a, **_k):
        raise RuntimeError("the router fell over")


# -- selection ---------------------------------------------------------------------
def test_priority_is_the_pipeline_dependency_order():
    """level -> judge -> prefill. Each stage produces what the next one consumes.

    Draining the earliest stage with work is therefore the same instruction as never
    letting a downstream stage starve; if this order is ever changed, that stops being
    true and the reason for the whole scheduler goes with it.
    """
    assert task_names() == ["level", "judge", "prefill"]
    assert [t.priority for t in all_tasks()] == sorted(t.priority for t in all_tasks())


def test_the_first_task_with_work_wins(criteria, profile):
    conn = store.connect(":memory:")
    _seed(conn, [
        ("1", "Backend Software Engineer", "d", Decision.UNCERTAIN),
        ("2", "Software Engineer", "d", Decision.MATCH),
    ])

    ctx = _ctx(criteria, profile)
    candidates = survey(conn, ctx)
    by_name = {c.task.name: c for c in candidates}
    assert by_name["level"].pending == 1
    assert by_name["judge"].pending == 1
    # Both have work; level is earlier in the pipeline, so it goes first.
    assert select(candidates).task.name == "level"
    conn.close()


def test_a_task_missing_its_configuration_is_unavailable_not_broken(criteria):
    """Missing config reads as 'unavailable', missing work as 'nothing to do'.

    They are different states and the report says which — an absent answers.yaml is
    something to go and fix, an empty queue is a healthy Tuesday.
    """
    conn = store.connect(":memory:")
    ctx = _ctx(criteria, profile=None)     # no profile loaded, no answers loaded
    by_name = {c.task.name: c for c in survey(conn, ctx)}
    assert by_name["judge"].unavailable and "profile" in by_name["judge"].unavailable
    assert by_name["prefill"].unavailable and "answers" in by_name["prefill"].unavailable
    assert by_name["level"].unavailable is None
    conn.close()


def test_a_posting_with_no_description_is_not_queued_work(criteria):
    """`resolve` stopped fetching, so an unread posting is work that cannot be done.

    Counting it as pending overstated the backlog every night and sent `--limit` runs
    to postings guaranteed to be no-ops.
    """
    conn = store.connect(":memory:")
    _uncertain(conn, "1", description=None)
    assert get_task("level").pending_count(conn, _ctx(criteria)) == 0
    store.set_description(conn, "Acme", "1", "now it has one")
    conn.commit()
    assert get_task("level").pending_count(conn, _ctx(criteria)) == 1
    conn.close()


def test_a_non_engineering_title_is_never_queued(criteria):
    conn = store.connect(":memory:")
    _uncertain(conn, "1", title="Field Marketer")
    assert get_task("level").pending_count(conn, _ctx(criteria)) == 0
    conn.close()


# -- containment -------------------------------------------------------------------
def test_each_unit_commits_on_its_own(criteria, tmp_path):
    """An interrupted run keeps every unit that already landed.

    Asserted by reading the rows through a *second* connection that never committed
    anything: if the runner were holding one open transaction, this would see nothing.
    """
    db = tmp_path / "s.db"
    conn = store.connect(db)
    _uncertain(conn, "1", "2", "3")

    client = _Answering({"level": "not_entry", "evidence": "8+ years"})
    report = asyncio.run(run_task(conn, get_task("level"), client, _ctx(criteria)))
    assert report.applied == 3

    observer = store.connect(db)
    assert observer.execute(
        "SELECT count(*) FROM verdicts WHERE verdict='reject'"
    ).fetchone()[0] == 3
    observer.close()
    conn.close()


def test_a_budget_stops_the_run_and_reports_what_is_left(criteria):
    conn = store.connect(":memory:")
    _uncertain(conn, "1", "2", "3", "4")

    client = _Answering({"level": "not_entry", "evidence": "senior"})
    report = asyncio.run(
        run_task(conn, get_task("level"), client, _ctx(criteria), budget=2)
    )
    assert report.attempted == 2 and report.applied == 2
    assert client.calls == 2          # the budget bounds model calls, not just writes
    assert report.remaining == 2
    conn.close()


# -- failure is absence ------------------------------------------------------------
def test_a_silent_model_writes_nothing(criteria):
    conn = store.connect(":memory:")
    _uncertain(conn, "1")
    report = asyncio.run(run_task(conn, get_task("level"), _Silent(), _ctx(criteria)))
    assert report.applied == 0 and report.no_answer == 1
    assert conn.execute(
        "SELECT verdict FROM verdicts WHERE ats_job_id='1'"
    ).fetchone()[0] == "uncertain"           # exactly where it was
    conn.close()


def test_a_raising_client_is_an_error_not_a_crash(criteria):
    """A task that throws must cost one unit, not the run."""
    conn = store.connect(":memory:")
    _uncertain(conn, "1")
    report = asyncio.run(run_task(conn, get_task("level"), _Exploding(), _ctx(criteria)))
    assert report.errors == 1 and report.applied == 0
    row = conn.execute("SELECT * FROM task_attempts").fetchone()
    assert row["last_status"] == "error"
    assert "fell over" in row["last_error"]
    conn.close()


def test_a_unit_that_keeps_failing_stops_consuming_budget(criteria):
    """Three nights of the same failure is a broken unit, not a flaky one.

    Without this the head of the queue would eat the whole budget forever while
    everything behind it starved.
    """
    conn = store.connect(":memory:")
    _uncertain(conn, "1")
    ctx = _ctx(criteria)

    for _ in range(MAX_ATTEMPTS):
        asyncio.run(run_task(conn, get_task("level"), _Exploding(), ctx))

    assert conn.execute("SELECT attempts FROM task_attempts").fetchone()[0] == MAX_ATTEMPTS
    # Still genuinely pending as far as the task is concerned...
    assert get_task("level").pending_count(conn, ctx) == 1
    # ...but the runner will not spend anything more on it.
    report = asyncio.run(run_task(conn, get_task("level"), _Exploding(), ctx))
    assert report.attempted == 0
    assert survey(conn, ctx)[0].blocked == 1
    conn.close()


def test_a_success_clears_the_failure_count(criteria):
    """A unit that works tonight has not used up two of its three lives."""
    conn = store.connect(":memory:")
    _uncertain(conn, "1")
    ctx = _ctx(criteria)

    asyncio.run(run_task(conn, get_task("level"), _Exploding(), ctx))
    assert conn.execute("SELECT attempts FROM task_attempts").fetchone()[0] == 1

    asyncio.run(run_task(conn, get_task("level"), _Answering(
        {"level": "unclear", "evidence": ""}), ctx))
    # 'unclear' is an answer that changes nothing, so it is still no_answer...
    assert conn.execute("SELECT attempts FROM task_attempts").fetchone()[0] == 2

    asyncio.run(run_task(conn, get_task("level"), _Answering(
        {"level": "not_entry", "evidence": "senior"}), ctx))
    assert conn.execute("SELECT attempts FROM task_attempts").fetchone()[0] == 0
    conn.close()


def test_a_task_that_cannot_write_rolls_back_rather_than_half_applying(criteria):
    conn = store.connect(":memory:")
    _uncertain(conn, "1")

    class _BadWrite(Task):
        name = "badwrite"
        priority = 999

        def pending(self, conn, ctx, limit=None):
            return [TaskUnit(task=self.name, company="Acme", ats_job_id="1",
                             unit_key="k", title="SWE")]

        async def run(self, unit, client, ctx):
            return "something"

        def apply(self, conn, unit, result, ctx):
            conn.execute("INSERT INTO manual_checks (company, last_surfaced) VALUES (?,?)",
                         ("Acme", "2026-08-13"))
            raise RuntimeError("halfway through")

    report = asyncio.run(run_task(conn, _BadWrite(), _Silent(), _ctx(criteria)))
    assert report.errors == 1 and report.applied == 0
    # The partial write was rolled back rather than committed alongside the failure.
    assert conn.execute("SELECT count(*) FROM manual_checks").fetchone()[0] == 0
    conn.close()


# -- idempotency -------------------------------------------------------------------
def test_the_unit_key_is_the_question_not_the_posting(criteria, profile):
    """Judging carries the prose hash, so editing the profile makes every unit new.

    A failure answering the old question says nothing about the new one, so the retry
    count resets with it. That is why the hash is in the key rather than beside it.
    """
    conn = store.connect(":memory:")
    _seed(conn, [("1", "SWE", "d", Decision.MATCH)])

    units = get_task("judge").pending(conn, _ctx(criteria, profile))
    assert units[0].unit_key == profile.prose_hash
    assert profile.prose_hash in units[0].idempotency_key()
    conn.close()


def test_an_llm_verdict_never_displaces_a_human_ruling(criteria):
    """`set_override` declines, and then the verdict is not ours to write either."""
    conn = store.connect(":memory:")
    _uncertain(conn, "1")
    store.set_override(conn, "Acme", "1", "match", TODAY, reason="mine",
                       decided_by="human")
    conn.commit()

    client = _Answering({"level": "not_entry", "evidence": "10+ years"})
    report = asyncio.run(run_task(conn, get_task("level"), client, _ctx(criteria)))
    assert report.outcomes == {"yours": 1}
    assert conn.execute(
        "SELECT decision, decided_by FROM overrides"
    ).fetchone()["decided_by"] == "human"
    conn.close()
