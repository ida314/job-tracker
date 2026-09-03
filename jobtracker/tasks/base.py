"""What a task is, and the registry of them.

Deliberately the same shape as `jobtracker/sources/`: the task modules are pure —
they build prompts, parse answers, and describe what to write — and one module
(`runner.py`) owns the loop, the sockets, the commits, and the telemetry. That split is
what lets a task be tested against a recorded payload with no network and no database.

A **task** is one bounded question the model can be asked, plus the two pieces of
deterministic code around it: which postings still need asking, and what to write down
once it answers. A **unit** is one posting's worth of that question. Units are the thing
the runner schedules, and each one is committed on its own.

Why a unit and not just a posting
---------------------------------
`unit_key` identifies the *question*, not the row. `judge` asks "how does this posting
fit the candidate described by prose hash X" — change the prose and it is a different
question about the same posting, so it is a different unit. That is not bookkeeping: it
is what makes re-asking cheap and correct,
and it is what stops a failure against an old question from being held against a new one.

The failure contract, inherited whole from `llm/client.py`: **a unit that cannot be
answered writes nothing**. The posting stays exactly where it was. Every task in this
package must be safe to abandon halfway through, on every night, forever.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

# How many consecutive failures before a unit is set aside. Three nights of the same
# failure is a broken unit, not a flaky one, and continuing to spend budget on it starves
# the rest of the queue. Set aside, not deleted: `jobtracker work --ledger` lists them.
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class TaskUnit:
    """One posting's worth of one task's question.

    `ats_job_id` is not always a posting. The `inbox` task's units are messages, and a
    message that identifies a company but not which of your applications it is about
    carries `''` here until you say. Anything that assumes this field names a row in
    `postings` is wrong for that task — which is also why `unit_key` carries the message
    id, so two such messages at one company do not collapse onto one `ident`.
    """

    task: str
    company: str
    ats_job_id: str
    unit_key: str
    title: str = ""
    payload: dict = field(default_factory=dict)

    @property
    def ident(self) -> tuple[str, str, str]:
        return (self.company, self.ats_job_id, self.unit_key)

    @property
    def label(self) -> str:
        return f"{self.company} — {self.title[:50]}" if self.title else self.company

    def idempotency_key(self) -> str:
        """Handed to the router so a resubmit after a restart is collapsed, not repeated."""
        return f"jobtracker:{self.task}:{self.company}:{self.ats_job_id}:{self.unit_key}"


@dataclass
class TaskContext:
    """Everything the tasks might need, loaded once per run.

    One bag rather than a signature per task, because the runner does not know which
    task it is about to select. A task that needs something absent — `prefill` with no
    answers.yaml — reports itself unavailable rather than failing at unit time.
    """

    today: str
    criteria: Any = None
    profile: Any = None
    answers: Any = None
    answers_path: Any = None
    tiers: dict = field(default_factory=dict)
    companies: dict = field(default_factory=dict)
    fetcher: Any = None
    # The path to the job-search mailbox, or None. A path, inspected — `inbox` reports
    # itself unavailable when this is absent and never opens it: reading the maildir is
    # `jobtracker mail`'s job, and the task is a pure read of what that recorded.
    maildir: Any = None
    # The resume, as source text, plus the format that owns it and a hash of the text.
    # Loaded here by the caller for the same reason `criteria` and `profile` are, and for
    # one more: a task never opens a file. `inbox` gets its mail from `mail_candidates`
    # rather than a maildir, and this is the same rule — the text arrives already read,
    # so nothing in the queue depends on a file being present at unit time.
    #
    # The hash is over the TEXT, not the filename. `Answers.hash` covers only a resume's
    # basename, so a document edited in place would not move it — and moving when the
    # content moves is the entire point of it being `tailor`'s unit_key.
    resume_text: Any = None
    resume_format: Any = None
    resume_hash: str = ""


class Task:
    """One bounded question, its queue, and what to do with the answer."""

    name: str = ""
    priority: int = 100
    summary: str = ""

    # -- availability ---------------------------------------------------------------
    def unavailable_reason(self, ctx: TaskContext) -> Optional[str]:
        """Why this task cannot run at all right now, or None if it can.

        Missing *configuration* belongs here — an absent answers.yaml, an unloadable
        profile. Missing *work* does not; that is a pending count of zero, which is a
        healthy state and reads differently in the report.
        """
        return None

    # -- the queue ------------------------------------------------------------------
    def pending(
        self, conn: sqlite3.Connection, ctx: TaskContext, limit: Optional[int] = None
    ) -> list[TaskUnit]:
        """Units this task could do right now, most important first.

        A pure read, derived from the tables that already exist. There is no stored
        queue to reconcile: if a posting closed overnight it simply stops appearing.
        """
        raise NotImplementedError

    def pending_count(self, conn: sqlite3.Connection, ctx: TaskContext) -> int:
        """How much work is waiting.

        The default is honest but not free — it builds the full pending list. Every
        current task's queue is at most a few thousand rows, so this stays cheap; a task
        whose queue grows past that should override with a COUNT.
        """
        return len(self.pending(conn, ctx))

    # -- the work -------------------------------------------------------------------
    async def run(self, unit: TaskUnit, client, ctx: TaskContext) -> Optional[Any]:
        """Answer one unit. None means "no answer", never "wrong answer".

        May make zero, one, or several model calls — `prefill` makes one per field it
        cannot resolve by rule, and often none at all. The runner treats any exception
        as None, but a task should not rely on that.
        """
        raise NotImplementedError

    def apply(
        self, conn: sqlite3.Connection, unit: TaskUnit, result: Any, ctx: TaskContext
    ) -> str:
        """Write the answer down. Returns a short label for the log.

        Called inside the runner's per-unit transaction; the runner commits after this
        returns and rolls back if it raises. Do not commit here.
        """
        raise NotImplementedError


_REGISTRY: dict[str, Task] = {}


def register(task: Task) -> Task:
    if not task.name:
        raise ValueError("a task needs a name")
    _REGISTRY[task.name] = task
    return task


def get_task(name: str) -> Optional[Task]:
    return _REGISTRY.get(name)


def all_tasks() -> list[Task]:
    """Every registered task in the order the runner will consider them.

    Priority is the pipeline's own dependency chain — level settles postings into
    matches, judge scores matches, prefill works down scored matches — so draining in
    priority order is the same instruction as never starving a downstream stage.
    """
    return sorted(_REGISTRY.values(), key=lambda t: (t.priority, t.name))


def task_names() -> list[str]:
    return [t.name for t in all_tasks()]
