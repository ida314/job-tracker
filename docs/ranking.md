# Ranking: the three jobs to apply to tomorrow

`resolve` decides whether a posting is **on-target**. `rank` decides which of the ones
that are is **urgent**, and surfaces three of them. Two bounded questions, two bounded
model roles — and the model's part here is the smaller one.

```
check   fetch, match, cache descriptions        (the only step that touches an ATS)
work    the next model task, whichever it is    (the router; optional)
rank    judge open matches, score the queue     (the router; optional)
today   the three to apply to                   (offline)
```

Judging is the `judge` task in the queue (docs/tasks.md) — same scope, same prompt, one
commit per posting instead of one per run. Scoring is deliberately **not** a task: it
needs no model, it must run on every invocation whether or not one is reachable, and it
is arithmetic over rows the task already wrote. `jobtracker rank` still runs both, in
that order.

## What the model does, and what it does not

It reads **one** posting against your profile and answers four questions:

```json
{"backend_fit": "strong|moderate|weak|none",
 "growth":      "strong|moderate|weak|none",
 "entry_risk":  "low|medium|high",
 "why":         "one sentence citing the description"}
```

It never sees another posting. It never returns a score, a rank, or a comparison.
Python turns those labels into a number using weights from `profile.yaml`, combined
with three things the pipeline already knows: company tier, location rank, and how long
ago the posting went up.

That division is the design, not an implementation detail. From DESIGN.md §3.2 — the
model supplies a fact, the rules decide what to do with it. Widening it to "pick the
best three" would put a nondeterministic component back in the ordering itself, which
is the thing DESIGN.md was written to undo.

### Why labels instead of a 0–100 score

Absolute numeric scores from an LLM cluster in a narrow band — nearly everything lands
between 70 and 85 — and the distribution shifts whenever the prompt or the model
changes. A prompt edit intended to fix one posting silently re-ranks the whole queue.
A four-point labelled scale is stable across both, and it keeps the arithmetic in
Python where you can read it, diff it, and test it.

## profile.yaml

Curation, not observation (DESIGN.md §3.3): human-authored, git-tracked, never written
by a run. It sits beside `companies.yaml` and `criteria.yaml`, and deliberately **not**
inside `criteria.yaml` — that file is matching rules guarded by `jobtracker eval`, and
profile edits have nothing to do with title matching.

Two halves, and the split is the point:

| | Goes to | Changing it |
|---|---|---|
| `target_roles`, `career_goals`, `profile` | the model, verbatim | re-judges everything |
| `weights`, `recency_half_life_days` | Python only | re-sorts instantly, **zero model calls** |

`Profile.prose_hash` covers only the prose. That is the whole mechanism: a judgment is
an *answer to a question*, so changing the question invalidates it and changing how you
weigh the answer does not.

```bash
$EDITOR profile.yaml      # bump `growth: 25` to `growth: 40`
jobtracker rank           # re-sorts the entire queue with the GPU switched off
```

The loader is strict in the same way `criteria.py` is: unknown key, missing weight,
non-numeric weight, and non-positive half-life all raise. A weight that silently
defaulted would drop a whole axis out of the score with nothing to see.

## The score

```
score = w.fit        × ordinal(backend_fit)     # strong 1.0 · moderate 0.6 · weak 0.25 · none 0
      + w.growth     × ordinal(growth)
      + w.recency    × 0.5 ** (days_since_posted / half_life)
      + w.tier       × band(tier)               # anchor 1.0 · applied 0.55 · research 0.2
      + w.location   × band(location_rank)      # nyc 1.0 · us 0.7 · unknown 0.4 · non-us 0.15
      + w.entry_risk × risk(entry_risk)         # weight is NEGATIVE, so this subtracts
```

Every term is named and kept on the `ScoreBreakdown`, so "why is this #1" decomposes
into `fit +30.0 · recency +18.2 · growth +15.0 · …`. DESIGN.md §3.5 requires every
automated verdict to carry its reason; an unexplained ordering is the same failure
wearing a different hat.

Tier uses **three bands, not seven steps**, matching the dashboard's colouring and
CLAUDE.md's own grouping. Tier numbers encode a strategy — tier 4 is not "twice as
good" as tier 2.

### Scores are absolute, and that is what makes the queue work

Because a score does not depend on what else is in the list, a new posting is judged
once and lands in its slot without disturbing anything above or below it. Nothing is
renumbered, there is no comparison state to go stale, and a single bad judgment can
misplace exactly one posting.

The alternative — pairwise comparison, binary-searching each new job into an ordered
list — is what an LLM is genuinely better at in isolation, but it costs ~log₂(n) calls
per posting, produces a path-dependent order that changes if you re-run it, and cannot
self-correct when the model's comparisons turn out non-transitive. A profile change
would force a full n log n re-sort instead of a free rescore.

## Two absences, handled deliberately

Both of these would otherwise look exactly like a working ranking, which is why each
has a test:

- **An unjudged posting scores `None`, never `0.0`.** Zero buries a model failure at
  the bottom of the list where nobody looks. Unjudged matches are excluded from the top
  three and reported as a count — on the CLI and on the dashboard.
- **An undated posting scores mid-scale (0.5), never "today".** `posted_on` is NULL
  until normalized, and treating that as fresh would float every stale req to the top.
  Treating it as ancient would bury a genuinely new posting whose board is stingy with
  metadata.

## Failure is absence

Every failure path in `judge_posting` returns `None`: unreachable server, timeout,
non-2xx, unparseable body, off-enum value. `None` means the posting stays unjudged and
out of the picks — never scored wrongly. Nothing here raises for a model that is down.

`rank` itself never fails for want of a model. With no provider configured, or an
unreachable one, it skips judging and **still scores** from the judgments it already
holds. Yesterday's ordering beats surfacing nothing.

`parse_judgment` rejecting off-enum values is the same guard that turned the
`guided_json` regression into "resolves nothing" rather than "wrong answers" — see
docs/llm.md. A server that silently ignores `response_format` and answers in prose must
produce no ranking, not a fabricated one.

## The queue, and how a job leaves it

A pick holds its slot until you say what happened to it. Without that, tomorrow either
repeats today's three or drops them unseen.

```bash
jobtracker today                                   # the three, with reasoning
jobtracker today --applied 'Palantir' <job_id>     # -> applications
jobtracker today --skip    'Palantir' <job_id>     # gone for good
jobtracker today --snooze  'Palantir' <job_id> --days 14
```

Or click the buttons under `jobtracker serve`, which POST to `/api/disposition`. The
static file written by `jobtracker dashboard` has no buttons on purpose — it must stay
self-contained, offline, and read-only, and a dead button in a mailed file is worse
than no button.

`applied` goes to the existing `applications` table, not to `deferrals`: it is not a
deferral, it is the start of the outer loop that table already tracks. A snooze returns
the posting on its own once `until` passes — that is the difference between snoozing
and skipping.

## Two budgets, and how they interact

`rank` can only judge what `check` has cached. A match with no stored description is
never queued for judgment — there is nothing to read, and burning a model call to
discover that is waste.

So a fresh corpus drains over a few nights:

| Budget | Flag | Default | Governs |
|---|---|---|---|
| descriptions | `check --max-descriptions` | 400 | ATS requests per run (~0.6s each) |
| judgments | `rank --limit` / `work --budget` | unlimited | model calls per run (~8s each) |

If `rank` reports a large "still unranked" count while the model is up, the cause is
usually the description backfill still draining, not the model.

## Running it

```bash
jobtracker check                                    # caches descriptions
jobtracker work --llm-url http://localhost:8000     # drains level, then judge
jobtracker rank --llm-url http://localhost:8000     # judge what is left, then score
jobtracker dashboard && jobtracker today
```

`$JOBTRACKER_LLM_URL` works here exactly as it does for `work`, as do the SDK's own
`$SIR_BASE_URL` / `$SIR_ENDPOINTS`. `rank` exits 0 whether or not a model was reachable;
sequencing is the caller's job, not the app's (docs/deployment.md).

## Checking its work

- `jobtracker rank` prints what it judged, what it scored, and what it could not read.
- The `why` on each pick cites the description, so a wrong judgment is visible rather
  than merely felt. If it is citing nothing specific, the description probably did not
  fetch — check `postings.description`.
- Reweight and re-run. If the order does not move, the weights are not doing what you
  think; print a `ScoreBreakdown` and look at which term dominates.
- Judgments are deterministic (`temperature: 0`), so the same posting under the same
  prose judges identically. Two runs disagreeing means the prose changed, not the model.
