# Applications: the outer loop

`check` and `work` find roles. `rank` decides which three to do today. This is what
happens after you click Apply — the part of the system where the input is you.

Since 2026-08-16 there is one other source of input, and it is deliberately not a writer:
your mailbox can **propose** that an application moved, and accepting the proposal is what
writes. See "Proposals from mail" below and docs/mail.md.

Two surfaces, one dataset:

| | `/applications` under `serve` | the Applications tab in `jobtracker dashboard` |
|---|---|---|
| Add a job by hand | yes | no |
| Change a stage, set a reminder, delete | yes | no |
| See everything, with history | yes | yes |

Plus a terminal mirror: `jobtracker applications` lists the same grouping, and
`jobtracker apply` writes.

---

## Why the table needed more than it had

`applications` has existed since the first schema and was **written by three code paths
and read by none** — `all_applications` had no production caller. Worse, all three
writers began with `SELECT ... FROM postings` and bailed when there was no row, so a
referral or a job off LinkedIn could not be recorded at all. And the row was a snapshot:
`status` moved in place, so "applied 8/1, OA 8/4, screen 8/11" collapsed to one word.

Three changes fix that, and each one is a rule worth keeping.

---

## 1. Stages are repeatable, and history is a log

```
applied · oa · screen · interview · offer · rejected · withdrawn
```

`interview` is **one status, entered as many times as you interview**. The alternative —
`round_1`, `round_2`, `onsite` — caps the enum at however many rounds you guessed, and a
fourth round has nowhere to go. A repeated event with a note says `round 2 — system
design` without a schema change, and counting those events is what renders as
`interview ×3`.

That requires two tables, and the split between them is load-bearing:

- **`applications`** is the current state, one row per `(company, ats_job_id)`.
- **`application_events`** is append-only, with no primary key, because
  `(company, ats_job_id, status)` genuinely repeats.

And therefore two writers:

```python
store.record_application(...)   # set state. Logs nothing.
store.add_application_event(...)  # append. Changes no state.
store.advance_application(...)  # both — "something happened"
```

Folding the append into `record_application` looks tidier and is wrong in both
directions: editing a note would duplicate an event, *or* a second interview round would
be suppressed as a no-op. From inside an upsert those two cases are indistinguishable.

So: moving a stage is `advance_application`. Changing a reminder is
`record_application` alone — rescheduling a phone call is not a thing that happened to
the application, and a timeline that records reschedules stops being a readable history
of the stages, which is the only thing it is for.

### There is no `ghosted`

Silence is **derived** from `updated_at` (`applications.is_stale`, 30 days). A status
only you can set is one you will not remember to set, and an unset "no reply" is
indistinguishable from an application that is going fine. Same reasoning as the dbt Labs
empty-board exemption: the system should not ask you to maintain a fact it can compute.

---

## 2. Optional columns update through COALESCE

`url`, `location`, `source`, `next_action` and `next_action_note` all write as
`COALESCE(excluded.col, applications.col)`.

This is not defensive style, it is a bug that would otherwise ship. `jobtracker apply`
passes none of those fields, so a plain assignment would blank a URL set from the web
page on the very next status change — and you would only find out when you went looking
for where you applied. The identical rule already governs `sync_postings` writing
`posted_on`, and for the identical reason.

The contract at the API layer follows from it:

| payload | meaning |
|---|---|
| key absent, or `null` | leave what is stored |
| `""` | clear it |
| a value | set it |

`_optional_text` / `_optional_day` in `server.py` are what keep those three apart.

`source` is nullable and read as `COALESCE(source, 'tracked')` rather than carrying a
`NOT NULL DEFAULT`. A column default only fires when the INSERT *omits* the column, and
this one is always bound — so `NOT NULL DEFAULT 'tracked'` rejected every caller that
passed None. NULL reading as `tracked` is also right for rows written before manual
entry existed: they all came from the pipeline.

---

## 3. Manual entries mint their own id

A hand-entered job has no vendor id, so one is minted:

```python
store.manual_job_id("Backend Engineer, New Grad")   # -> "manual:backend-engineer-new-grad"
```

**Deterministic**, so re-adding the same role at the same company updates the row you
already have rather than silently creating a second one — the same stable-id rule the
aggregator adapter follows for postings with no vendor id. A title that slugs to nothing
(pure punctuation) falls back to a digest, or every such entry at one company would
collapse onto the same row.

The `manual:` prefix guarantees a hand-typed row can never collide with a real
`(company, ats_job_id)`, even if that company is later added to `companies.yaml` and
fetched for real.

Manual rows have **no `postings` row**, which is exactly why `all_applications` reads
`applications` directly instead of joining through `postings`. Join it and manual entries
disappear.

---

## The page

Grouped by what needs doing, not by date:

1. **Needs action** — a next-action date that is due or overdue, or an active
   application nobody has touched in 30 days. Soonest first.
2. **Active** — everything else still in play, **most stalled first**, so the ones going
   quiet surface without you having to filter for them.
3. **Closed** — offer, rejection, withdrawn. Most recent first; a record, not a queue.

A row carries: status pill (with `×N` when the stage repeats), tier chip, location, how
long ago you applied, days since anything moved, the next action and what it is, and the
event timeline behind a `<details>`.

### Next action, not deadline

One nullable date meaning *"do something about this by X"*. Not an apply-by deadline:
every row in this table exists because you already applied, so a closing date has nothing
left to inform. NULL is the normal state and never reads as overdue.

`I applied` schedules one automatically, seven days out — see
`applications.default_next_action`.

### Response rate

`responded / total`, where responded means the employer ever moved you past `applied`.
Read from the **event log**, not the current status: by the time a row says `rejected`,
the status alone no longer records that anyone ever replied. `withdrawn` does not count —
you caused it, and counting it would let giving up look like traction. A rejection does
count; a rejection is a reply.

This is the number `jobtracker apply`'s docstring has always been reaching for: the
evidence that would let tiers eventually be re-ranked by what actually converts rather
than by prior. Per-tier conversion is the obvious next step and is deliberately not
built yet — there is no data.

---

## Proposals from mail

A section above the add form, when there is one. Full design in docs/mail.md; what matters
to *this* table:

- **Accepting is the only path from the mailbox into `applications`.** The scan writes to
  `mail_candidates`, the model writes to `mail_proposals`, and neither touches this table
  or its event log. There is a test that snapshots both around a full task run.
- **The event note is composed by Python** — `from mail: <subject> (<date>)`. The model's
  quote is the evidence shown on the card and stays in `mail_proposals.evidence`. Nothing
  it wrote reaches an application, which is DESIGN.md §8.4's rule with your own history as
  the field.
- **An unresolved job is asked about, never guessed.** When a message identifies the
  company but not which of your applications, the card renders a dropdown and the endpoint
  refuses until you pick. A stage on the wrong job is not a thing you would notice later.
- **Dismissed is a resolution, not a delete.** The row stays so the next scan cannot
  propose the same message again.
- **The controls are `app-accept` and `app-dismiss`**, in the same `app-*` family as the
  rest of this page and therefore inside the exact-set test below.

## Rules that must not be "simplified"

- **The static file carries no buttons.** `dashboard._applications` renders read-only and
  has no `interactive` flag at all. A button in a `file://` page has nothing to POST to,
  and a button's handler must live in the file that renders the button — dashboard.py
  ships no application handlers, so it must ship no application buttons. Every control is
  emitted by `server.render_applications` and every handler is a branch in `server._JS`.
  There is a test asserting the button set and the handler set match exactly — it seeds a
  pending mail proposal too, because an equality that only ever sees four of six buttons
  is not the guard it looks like.
- **The applications panel is never a `table[data-filterable]`.** The filter JS selects
  those, so a tier or location filter left set on the All postings tab would silently
  empty the list of things you actually did. Same trap the picks are protected from, and
  it has its own test.
- **A refused write writes nothing.** Bad status, unparseable date, `javascript:` URL,
  missing title — all return `{"ok": false, "error": ...}` with HTTP 200 and touch
  neither table. A half-recorded application is worse than a refused one, because you
  would believe it saved.
- **A date that does not parse is a refusal, never stored raw.** Text sorted against real
  ISO dates would collate wrongly and the reminder would simply never come due — failure
  as absence, in the one feature whose entire job is to remind you.
- **`rank.is_available` excludes on the *presence* of a status, not on which one.** A
  rejection must not put the job back in tomorrow's top 3. There is a test that walks all
  seven statuses.
- **Timestamps are not days.** `applied_at`/`updated_at` are full timestamps;
  `next_action` is a plain day. `date.fromisoformat` rejects the former outright and the
  failure is silent — the row just reports no age. Every comparison goes through
  `applications.day_of` first.
- **Unknown is never zero.** `days_since` returns `None`, not 0, for an unreadable date,
  and an unreadable `updated_at` sorts *last* in the active list rather than first. A
  corrupt row must not masquerade as the most urgent thing on the page.

---

## CLI

```bash
jobtracker applications                       # list, grouped as the page groups it

# a tracked posting
jobtracker apply Stripe 7966029 --status interview --note "round 2 — system design"

# a job the pipeline never surfaced
jobtracker apply "Some Startup" --manual --title "Backend Engineer (Referral)" \
    --url https://... --next-action 2026-08-25 --next-action-note "ping Alex"

# what your mailbox thinks happened
jobtracker mail --list
jobtracker mail --accept '<CAF...@mail.gmail.com>'
```

`apply` appends an event on every run, so repeating it with `--status interview` twice is
how you record two rounds from the terminal.
