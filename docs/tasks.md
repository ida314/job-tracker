# The task queue

`jobtracker work` runs the next available piece of model work. It picks the task, drains
some of it, and commits each unit on its own.

```
level   10   read a description for the level its title omitted   -> produces matches
judge   20   judge a match against profile.yaml                   -> produces scores
prefill 30   work out what goes in an application form            -> consumes scores
```

Cron is out of scope and deliberately so: this repo does not know about schedulers
(docs/deployment.md). It ships the command; the machine that runs it decides when.

## Why a queue at all

There were two model passes and they were the same function written twice —
`classify_level` and `judge_posting` differed only in prompt, schema, and parser. Each
was a sequential loop with one commit at the end, so an interrupted run wrote nothing.
And "what should this system do next" had no answer in the code: you picked by typing a
subcommand.

A task makes all three explicit. It is one bounded question the model can be asked, plus
the two pieces of deterministic code around it: which postings still need asking, and
what to write down once it answers.

## Selection is the pipeline order

Tasks are polled in ascending priority and the first with work wins. That order is not a
preference — it is the dependency chain. `level` settles uncertain postings into matches,
`judge` scores those matches, `prefill` works down the scored ones. So "always work the
earliest stage that has work" is the same instruction as "never starve a later stage",
and there is only one knob to get wrong instead of three.

```
$ jobtracker work --dry-run
Task queue, in the order the scheduler considers it:

   10  level      2,314 pending
                  read descriptions to settle UNCERTAIN postings
   20  judge      nothing to do
                  judge open matches against profile.yaml
   30  prefill    unavailable — answers.yaml not loaded
                  prefill applications for the best-matched jobs

Would work: level (2314 unit(s))
```

**Unavailable and nothing-to-do are different states**, and the report says which. An
absent `answers.yaml` is something to go and fix; an empty queue is a healthy Tuesday.
Collapsing them would hide a misconfiguration behind a number that looks fine.

## The queue is derived, never stored

Every task's `pending()` is a SQL read over tables that already exist. There is no queue
table, so there is nothing to reconcile: a posting that closed overnight simply stops
appearing, and a verdict you pinned by hand removes its unit without anything having to
notice.

The one piece of new state is `task_attempts`, and it is a **failure ledger**, not a
queue. After `MAX_ATTEMPTS` (3) consecutive failures a unit is set aside, because three
nights of the same failure is a broken unit rather than a flaky one, and continuing to
spend the budget on it starves everything behind it. A success resets the count — a unit
that works tonight has not used up two of its three lives.

Set aside, not deleted. `survey()` reports the count, so a growing blocked figure is
visible rather than being an inexplicably shrinking queue.

## A unit is a question, not a posting

`unit_key` identifies the question:

| task | unit_key | so a change to… |
|---|---|---|
| `level` | `"level"` | nothing re-asks it; a re-run is a retry |
| `judge` | `profile.prose_hash` | profile prose re-asks every posting |
| `prefill` | `answers.hash` | answers.yaml re-plans every application |

That is not bookkeeping. Editing the prose makes every posting a *new* unit, so its
retry count starts clean — correct, because a failure answering the old question says
nothing about the new one. It is also what makes re-asking cheap: change a `weight` in
profile.yaml and no judgment is invalidated at all, because weights are not in the prose.

The key is also handed to the router as an idempotency key, so a resubmit after a router
restart is collapsed rather than re-run.

## Containment

Each unit is prompted, parsed, applied, and **committed by itself**. Interrupt a run at
95% — ^C, a killed container, a router that goes away mid-batch — and you keep the 95%.
A task that raises while writing is rolled back to the last committed unit, so there is
no half-applied state to clean up.

**Failure stays absence**, inherited whole from docs/llm.md. A unit that times out,
returns nonsense, or throws writes *nothing*; the posting stays exactly where it was.
Every task in this package must be safe to abandon halfway through, on every night,
forever. The model can add resolution to this pipeline; it cannot subtract correctness.

## Concurrency

Units run through a bounded `asyncio.Semaphore`, default 4 — the same cap
`fetch.MAX_WORKERS` uses, and for the same reason: it is a politeness bound on a shared
resource, not a throughput knob. The router queues per model anyway, so sending 200 at
once would not make the GPU faster, it would move the queue into someone else's process.

Results are reassembled into **input order** before being applied, exactly as `fetch_all`
does with `as_completed`. Nothing downstream may depend on which unit finished first.

Within a single unit, `prefill` asks about its unresolved fields one at a time. That is
deliberate: parallelising there would let one unit exceed the concurrency cap on its own.

## Adding a task

One module in `jobtracker/tasks/`, one import line in `__init__.py` — the same two steps
as adding an ATS to `sources/`, and the same rule: **the task module is pure**. It builds
prompts, parses answers, and describes what to write. `runner.py` owns every socket,
every transaction, and the clock.

```python
class MyTask(Task):
    name = "mine"
    priority = 40
    summary = "what this is for, shown in --dry-run"

    def unavailable_reason(self, ctx): ...        # missing config, or None
    def pending(self, conn, ctx, limit=None): ... # pure read, ordered, most important first
    async def run(self, unit, client, ctx): ...   # None means "no answer", never "wrong answer"
    def apply(self, conn, unit, result, ctx): ... # write; the runner commits

register(MyTask())
```

`pending()` must return only work the task can actually do. `level` excludes postings
with no cached description and `prefill` excludes companies whose form it has never seen,
because counting those as pending overstates a backlog no model could drain — and sends a
`--budget` run to units guaranteed to be no-ops.

## Running it

```bash
jobtracker work                                    # the next task, whatever it is
jobtracker work --task prefill --budget 20         # pin one, cap the units
jobtracker work --dry-run                          # the queue; changes nothing
jobtracker work --concurrency 8 --llm-url http://gpu:8000
```

`resolve` is `work --task level`, kept because it is in muscle memory, in the docs, and
in whatever cron already calls it. `rank` still exists too: its judging phase is the
`judge` task, and its scoring phase is not a task at all — scoring needs no model, must
run whether or not one is reachable, and is arithmetic over rows `judge` already wrote.

`work` never fails for want of a model. With none configured or reachable it prints the
queue and changes nothing, so it is safe to run before the router is up.

## Telemetry

Spans `task.run` → `task.unit`, attributes `task.name` and `task.outcome`. Metric
attributes stay bounded — three tasks × three outcomes — the same rule that keeps
`company` out of metrics and in traces (docs/observability.md).
