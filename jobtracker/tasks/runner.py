"""Pick the next task, drain some of it, and commit each unit on its own.

This is the only module here that owns a socket, a transaction, or a clock. Everything
in the task modules is a pure function of rows and prompts.

Three properties it exists to guarantee:

**Containment.** Each unit is answered, applied, and committed by itself. Interrupting a
run — ^C, a killed container, a router that goes away mid-batch — loses at most the one
unit in flight. The passes this replaced held everything in memory until a single commit
at the end, so a run interrupted at 95% wrote nothing at all.

**Selection is the pipeline order.** Tasks are considered by ascending priority, and the
first with pending work wins. `level` produces the matches `judge` scores, and `judge`
produces the scores `prefill` works down, so "always work the earliest stage that has
work" is also "never let a downstream stage starve".

**Failure stays absence.** A unit that raises, times out, or comes back unparseable
writes nothing and is counted. It never produces a partial or a guessed answer. The
model can add resolution to this pipeline; it cannot subtract correctness.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from opentelemetry import trace

from .. import store
from .base import MAX_ATTEMPTS, Task, TaskContext, TaskUnit, all_tasks, get_task

log = logging.getLogger("jobtracker.tasks")
tracer = trace.get_tracer("jobtracker.tasks")

# Concurrent units in flight. Matches `fetch.MAX_WORKERS`, and for the same reason: it
# is a politeness bound on a shared resource, not a throughput knob. The router queues
# per model anyway — sending 200 at once would not make the GPU faster, it would just
# move the queue into someone else's process.
DEFAULT_CONCURRENCY = 4


@dataclass
class TaskReport:
    task: str = ""
    attempted: int = 0
    applied: int = 0
    no_answer: int = 0
    errors: int = 0
    outcomes: dict = field(default_factory=dict)
    remaining: int = 0

    def summary(self) -> str:
        detail = " · ".join(
            f"{n} {label}" for label, n in sorted(self.outcomes.items())
        )
        line = (
            f"{self.task}: {self.attempted} attempted · {self.applied} applied · "
            f"{self.no_answer} no answer · {self.errors} error"
        )
        if detail:
            line += f"  ({detail})"
        if self.remaining:
            line += f"\n  {self.remaining} unit(s) still pending"
        return line


@dataclass
class Candidate:
    """A task's standing at selection time."""

    task: Task
    pending: int = 0
    blocked: int = 0
    unavailable: Optional[str] = None


def survey(conn: sqlite3.Connection, ctx: TaskContext) -> list[Candidate]:
    """Every task's standing, in the order selection considers them.

    `pending` here counts units the task believes it could do, minus those out of
    retries. It is an estimate in exactly one direction: a unit can drop out between the
    survey and the run (a posting closes, an override lands), never appear. Over-reporting
    by a handful is the acceptable side of that, because under-reporting would let a task
    with work look idle.
    """
    out: list[Candidate] = []
    for task in all_tasks():
        reason = task.unavailable_reason(ctx)
        if reason:
            out.append(Candidate(task=task, unavailable=reason))
            continue
        blocked = store.blocked_units(conn, task.name, MAX_ATTEMPTS)
        try:
            units = task.pending(conn, ctx)
        except Exception as exc:  # noqa: BLE001 — a broken query must not kill the run
            log.warning("task %s could not survey its queue: %s", task.name, exc)
            out.append(Candidate(task=task, unavailable=f"queue unreadable: {exc}"))
            continue
        live = [u for u in units if u.ident not in blocked]
        out.append(Candidate(task=task, pending=len(live),
                             blocked=len(units) - len(live)))
    return out


def select(candidates: list[Candidate]) -> Optional[Candidate]:
    """The highest-priority task that has work. None if the queue is empty."""
    for candidate in candidates:
        if candidate.unavailable is None and candidate.pending > 0:
            return candidate
    return None


def _units_for(
    conn: sqlite3.Connection, task: Task, ctx: TaskContext, budget: Optional[int]
) -> list[TaskUnit]:
    """The next `budget` units of `task`, minus the ones out of retries.

    Over-fetches by the size of the blocked set so that a budget of 20 still yields 20
    live units when a few at the head of the queue are permanently stuck.
    """
    blocked = store.blocked_units(conn, task.name, MAX_ATTEMPTS)
    over = None if budget is None else budget + len(blocked)
    units = [u for u in task.pending(conn, ctx, limit=over) if u.ident not in blocked]
    return units if budget is None else units[:budget]


async def run_task(
    conn: sqlite3.Connection,
    task: Task,
    client,
    ctx: TaskContext,
    budget: Optional[int] = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    units: Optional[list[TaskUnit]] = None,
) -> TaskReport:
    """Drain up to `budget` units of one task.

    `units` overrides the queue, for a caller that has already decided *which* work it
    wants — `prepare` targets exactly tomorrow's picks rather than "the N highest-scored
    postings needing a plan", which are usually but not always the same set. The
    out-of-retries filter still applies: a unit that has failed three nights running is
    not worth spending on just because someone asked for it by name.
    """
    report = TaskReport(task=task.name)
    if units is None:
        units = _units_for(conn, task, ctx, budget)
    else:
        blocked = store.blocked_units(conn, task.name, MAX_ATTEMPTS)
        units = [u for u in units if u.ident not in blocked]
        if budget is not None:
            units = units[:budget]
    if not units:
        return report

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def answer(unit: TaskUnit):
        async with semaphore:
            with tracer.start_as_current_span("task.unit") as span:
                span.set_attribute("task.name", task.name)
                try:
                    return await task.run(unit, client, ctx)
                except Exception as exc:  # noqa: BLE001 — a task bug is not a run bug
                    log.debug("task %s raised on %s: %s", task.name, unit.label, exc)
                    span.set_attribute("task.outcome", "error")
                    return exc

    with tracer.start_as_current_span("task.run") as span:
        span.set_attribute("task.name", task.name)
        span.set_attribute("task.units", len(units))

        # `gather` preserves input order, so results are applied in queue order however
        # they finished. Downstream must never depend on which unit came back first —
        # the same rule `fetch_all` follows when it reassembles by input order.
        results = await asyncio.gather(*(answer(u) for u in units))

        for unit, result in zip(units, results):
            report.attempted += 1
            if isinstance(result, Exception):
                report.errors += 1
                _finish(conn, unit, "error", ctx.today, str(result)[:200])
                continue
            if result is None:
                report.no_answer += 1
                _finish(conn, unit, "no_answer", ctx.today)
                continue
            try:
                outcome = task.apply(conn, unit, result, ctx)
            except Exception as exc:  # noqa: BLE001
                # The write failed, so the unit did not happen. Roll back to the last
                # committed unit rather than leaving a half-applied one behind.
                conn.rollback()
                log.warning("task %s could not apply %s: %s", task.name, unit.label, exc)
                report.errors += 1
                _finish(conn, unit, "error", ctx.today, str(exc)[:200])
                continue
            report.applied += 1
            report.outcomes[outcome] = report.outcomes.get(outcome, 0) + 1
            _finish(conn, unit, "ok", ctx.today)

    # Re-read rather than subtracting what was applied: an applied unit has left its
    # task's queue by construction (the verdict moved, the prose hash now matches, the
    # plan is current), so subtracting would count it twice. What legitimately stays is
    # everything the budget did not reach plus everything that answered nothing.
    blocked = store.blocked_units(conn, task.name, MAX_ATTEMPTS)
    report.remaining = max(0, task.pending_count(conn, ctx) - len(blocked))
    return report


def _finish(
    conn: sqlite3.Connection,
    unit: TaskUnit,
    status: str,
    today: str,
    error: Optional[str] = None,
) -> None:
    """Record the attempt and commit. One unit, one transaction."""
    store.record_attempt(
        conn, unit.task, unit.company, unit.ats_job_id, unit.unit_key,
        status, today, error,
    )
    conn.commit()


async def run_next(
    conn: sqlite3.Connection,
    client,
    ctx: TaskContext,
    task_name: Optional[str] = None,
    budget: Optional[int] = None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> Optional[TaskReport]:
    """Work the next available task. None when there is nothing to do.

    `task_name` pins the choice, which is what `resolve` and `rank` do — they are the
    same machinery aimed at one task rather than a different mechanism.
    """
    candidates = survey(conn, ctx)
    if task_name:
        task = get_task(task_name)
        if task is None:
            raise ValueError(f"unknown task {task_name!r}")
        chosen = next((c for c in candidates if c.task.name == task_name), None)
        if chosen is None or chosen.unavailable:
            reason = chosen.unavailable if chosen else "not registered"
            log.warning("task %s unavailable: %s", task_name, reason)
            return None
    else:
        chosen = select(candidates)
        if chosen is None:
            return None
        log.info(
            "working %s (%d pending) — %s",
            chosen.task.name, chosen.pending, chosen.task.summary,
        )

    return await run_task(
        conn, chosen.task, client, ctx, budget=budget, concurrency=concurrency
    )
