"""The task queue: what the model should work on next, and in what order.

Importing this package registers every task, the same way importing `sources/`
registers every ATS adapter. Adding a task is one module plus one import line here.

Priority is the pipeline's dependency chain, not a preference:

    level   10   settle UNCERTAIN postings   -> produces matches
    judge   20   judge matches vs. profile   -> produces scores

so "work the next available task" and "keep every stage drained" are one instruction.

    inbox   40   read replies from employers -> proposes application updates

`inbox` is the exception, and it says so: it is not in that chain at all. It goes last
because its queue refills from an external stream on its own schedule, so anywhere
earlier a busy mailbox would starve the pipeline's own stages.

**`prefill` used to sit at 30, and left on 2026-08-25.** It stopped asking a model
anything — it resolves a form against answers you typed and nothing else — and this
package is for work that needs one. Keeping it here would have gated plan-building
behind `cmd_work`'s router check, so a night with `sir` down would silently build no
plans at all: the same reason scoring has never been a task. It is `jobtracker prefill`
now, and `prepare` calls it directly. See `jobtracker/prefill.py`.
"""

from .base import (  # noqa: F401
    MAX_ATTEMPTS,
    Task,
    TaskContext,
    TaskUnit,
    all_tasks,
    get_task,
    register,
    task_names,
)
from . import level  # noqa: F401  (side effect: register())
from . import judge  # noqa: F401  (side effect: register())
from . import inbox  # noqa: F401  (side effect: register())
from .runner import (  # noqa: F401
    DEFAULT_CONCURRENCY,
    Candidate,
    TaskReport,
    run_next,
    run_task,
    select,
    survey,
)

__all__ = [
    "Task",
    "TaskContext",
    "TaskUnit",
    "TaskReport",
    "Candidate",
    "MAX_ATTEMPTS",
    "DEFAULT_CONCURRENCY",
    "all_tasks",
    "get_task",
    "register",
    "task_names",
    "run_next",
    "run_task",
    "select",
    "survey",
]
