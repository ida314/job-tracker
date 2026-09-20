# The posting page

`/posting?company=…&job=…` under `serve`. One page per posting: what the job is, which
documents would go out, the questions the form asks beyond those, and — once you have
applied — what you actually sent.

Before this, clicking a posting anywhere opened the employer's form in a new tab. That is
the last step, and everything before it lived somewhere else: the tailored resume behind a
`↓` in the actions cell, the cover letter behind a `✉`, the resume override behind the
mothballed `/apply` page, and the short questions an ATS really asks — work
authorization, sponsorship, why this company — nowhere at all. They were retyped into
every form and forgotten afterwards.

```
Today's pick / a table row / an /applications card
        │
        └──▶ /posting          prepare here
                 │
                 ├── Open the application ↗     the employer, when you are ready
                 └── I applied                  freezes what went out
```

---

## Two surfaces, and why only one of them links here

| | `serve` | `jobtracker dashboard` (the static file) |
|---|---|---|
| a posting title links to | `/posting` | the employer's URL |
| the pick's big button | `Prepare →` | `Apply` |

The static dashboard is an artifact you mail and open offline. `/posting` in it is a link
to nothing — the same defect as a button with no handler, which is the whole reason
`interactive` exists. `dashboard._posting_href` / `_posting_attrs` are the one place that
decision is made, and `_posting_attrs` carries the `target`/`rel` difference with it:
the internal link is same-origin and must **not** open a new tab, the external one must
carry `rel="noopener"`. Splitting those two facts apart is how one call site ends up
opening the employer in-place.

---

## What it reads, and why it never joins

Four sources, in order. A manual application — a referral, something off LinkedIn — has
**no `postings` row at all**, and joining through one is exactly how `all_applications`
would lose it (docs/applications.md §3). So:

1. `applications` — title, url, location, status. The only source a manual entry has.
2. `store.posting_detail` — the posting with its verdict, judgment and deferral in one
   query. Optional, and absent for a manual entry.
3. the two documents, keyed on the pair — valid either way.
4. `posting_answers`, and `application_submissions` if one was frozen.

All four empty is the only 404, and it says which pair it was asked for.

---

## The questions

Two tables, and the split is the point.

- **`question_template`** — the list you maintain once on `/settings`, with a default
  answer. A **checklist**, not an answer bank: nothing reads it to fill a form and nothing
  sends it anywhere.
- **`posting_answers`** — what you actually put, for one posting.

`questions.merge` puts them on the page together **without writing anything**. A GET never
mutates, and here that is not a formality: a merge that wrote would turn *opening* a page
into a claim that you had answered something. A template row renders marked *from your
template* and becomes this posting's own only when you save it.

Three consequences worth keeping:

- **A template default you never saved is not recorded as submitted.** CLAUDE.md's
  "nothing may put a value in a field the user did not give for it", inside the one
  artifact whose job is to be true. `questions.answered` is where that is enforced.
- **A saved answer with no text is not recorded either.** Keeping the question and
  recording no reply reads afterwards as "I left this blank", which is a claim about the
  form rather than about you.
- **The wording is stored as the page rendered it.** Editing the template later must not
  rewrite an answer you already gave, and re-deriving the question text is how it would.

`qid` is minted from the question text **once** and never re-derived — that is the whole
difference from `store.manual_job_id`, whose determinism is its feature. Minting once is
what lets you fix a question's wording without orphaning its answers. Two questions that
slug alike on one posting get a numeric suffix; a question of pure punctuation gets a
digest, or every such question would collapse onto one row.

Ordering is computed at render across the merged list, on `(ordinal, qid)`. "Stored first,
then template" would put two rows at ordinal 3 and swap them under your cursor after a
save; a newly saved row takes `max(ordinal) + 1`, so saving never moves anything above it.

---

## The documents

Both read through `submissions.effective_resume` / `effective_letter` — **the same call
the freeze makes**, so the page and the archive cannot disagree about which file this is.
That is `resumes.py`'s rule ("an application goes out under your name, and the two paths
disagreeing about which PDF went with it is not a thing you could discover afterwards") at
the one moment it would matter.

```
resume   posting_resumes upload  →  the bank's resume:
letter   posting_letters upload  →  the written one, if letter.is_current  →  the bank's cover_letter:
```

**The tailored PDF is deliberately not a term in the resume chain.** `tailor build
--attach` is what makes it this posting's resume, and attaching writes it into
`posting_resumes` — so it reaches an application through the first branch, after you have
read the diff, and never because a build happened to succeed last night. There is no
attach control on this page and there must not be one.

Letter currency matters in a way resume currency does not: `cover_letters.written_at`
moves when the model rewrites the paragraphs, and a stale PDF is not what you sent.

### Where an uploaded letter goes, and where it must not

`RESUMES_DIR`, under `resumes.letter_upload_name` — **not** `LETTERS_DIR`. Three reasons,
and the third is the one that bites:

- That directory is documented as generated state where "deleting it costs a rebuild, not
  a file". A letter you wrote is a file.
- `letter.letter_path` already owns `LETTERS_DIR/<letter_stem>.pdf`, so a `.pdf` upload
  would land on top of the letter `coverletter build` writes.
- `Dockerfile.serve` sets `JOBTRACKER_RESUMES` but **not** `JOBTRACKER_LETTERS`, so in the
  serve image that directory is inside the image layer and `docker pull` would take the
  only copy with it.

The `_letter` infix is what keeps it off the resume override for the same posting, which
`stored_name` mints from the identical digest.

### What each upload gates on, and what it answers

The two uploads are symmetric, and the symmetry is a thing that has to be maintained
rather than a thing that holds by itself. Both `/api/posting-resume` and
`/api/posting-letter` — and `/api/posting-answer` with them — gate on
`Handler._known_posting`, which resolves a pair through `postings` **or**
`applications`. That second half is the whole point: a manual application has no posting
row, and it is the record you most want to keep a document against. Until 2026-09-20 the
resume upload carried its own inline `SELECT … FROM postings`, so it answered "no such
posting" for a manual entry while the page rendered the upload button and the other two
writes took it. A parity test reads the gate off the source of all three, because the
drift is invisible from a passing test of any one of them.

Both answer flatly — `{"ok": true, "filename": …, "bytes": …}` for an upload,
`{"ok": true, "detail": "removed"}` for a clear — and neither does anything after the
commit that could decide its answer. The resume half used to tail into
`_rebuild_plan` and then `setdefault("ok", True)`, which cannot overwrite an explicit
False; `_rebuild_plan` refuses whenever no application form has been learned, and with
prefill mothballed none ever is, so every successful upload reported a refusal *after*
writing the file and the row. `server._JS`'s `p-upload` handler alerts and skips its
`location.reload()` on `!ok`, which is what made the page go on saying "your default,
from the answer bank" over an override that had landed. `_rebuild_plan` still exists and
`/apply` still calls it; what it may not do is answer for a write that already happened.

---

## The freeze

`submissions.freeze` copies the two documents into
`SUBMISSIONS_DIR/<stored_name>/{resume,letter}.pdf` and writes one
`application_submissions` row carrying the frozen Q&A as JSON.

**It stores the bytes.** A filename would be cheaper and would quietly become a lie:
`tailor build` rewrites a posting's PDF at a deterministic path, so a stored name goes on
resolving forever while coming to mean a document you never sent. Copying is the only form
of "this is what I sent" that survives the next rebuild. There is a test that freezes,
rewrites the working document, and asserts the archive did not move.

It is called by the three paths that can *create* an application, right after their
existing `advance_application` — no new write path:

| caller | condition |
|---|---|
| `server._api_disposition`, `applied` | what `+ tracker` and the picks' **I applied** post |
| `server._api_application` | only when `get_application` was None **before** the write |
| `cli.cmd_apply` | same condition, so the terminal records what the page does |

`_api_mail_accept` does not freeze: its narrower is built from `applications`, so it can
only move a row that already exists.

**Write-once.** A submission is a fact about a moment, so a second `applied` finds the row
standing and leaves it and its files alone. The row is claimed *before* anything is
copied, so two writers racing cannot both decide they are the first and then overwrite
each other's bytes.

The exception is **Update what I submitted**, the only caller anywhere passing
`replace=True`. It exists for one flow: `+ tracker` on a table row records an application
before you have opened this page, so the snapshot it froze is of a page you had not filled
in. It is scoped to an application still at `applied` — once an employer has replied, what
you sent is history, and rewriting it is not a correction.

A copy that fails is a log and a gap in the row, never an exception: this runs inside the
click that records an application, and a refused "I applied" is work you have to redo.

## Taking a document back out

**Did not send** (`sub-drop` → `POST /api/submission/clear`, `submissions.drop_document`)
removes one frozen document from the record: the column goes NULL, `*_kind` goes empty,
the archived copy is deleted, and the row reads "none went out" like any application that
had no letter to begin with.

It exists because the freeze records the document that was **in effect**, and a letter
`coverletter build` wrote is in effect for a posting whether or not you attached it to the
employer's form. Nothing here can see the form you filled in, so the one correction this
record needs is subtractive.

It is the second writer of `application_submissions`, and deliberately not
`freeze_submission(replace=True)` in different clothes:

| | Update what I submitted | Did not send |
|---|---|---|
| direction | rewrites the row from what is in effect **now** | only ever removes |
| scope | the whole row, both documents and the Q&A | one document |
| status | `applied` only | any status |

The status difference falls out of the direction. An update after a reply would put
today's documents into yesterday's record, which is inventing history; a removal can only
ever say less than the row already said, and the month you notice a record is wrong is
rarely the month you applied.

**The row goes first, then the bytes** — `/api/posting-letter/clear`'s rule. An orphaned
file under `SUBMISSIONS_DIR` is inert; a row naming a file that is not there logs on every
lookup and renders a download that 404s. A failed unlink is a log, not an exception: by
then the record is already correct. Removing what is not there is a refusal, not a no-op,
and like every refusal here it writes nothing. `kind` names a column, so it is checked
against `store.SUBMISSION_DOCUMENTS` rather than bound. The control renders only beside a
document the row actually carries, and `/api/submission/clear` stays out of
`_UPLOAD_ROUTES` for the same reason its `posting-letter` sibling does.

---

## Rules that must not be "simplified"

- **Every control's handler is a branch in `server._JS`**, the script this page emits.
  A button rendered here with its handler in `dashboard._JS` is a button that does
  nothing, which this repo has already shipped once. `app-save` / `app-meta` are shared
  with `/applications` and select `.app, .aprow` for exactly that reason — one handler,
  two containers, rather than a second copy that could log a stage differently.
- **`/posting` is not in `_NAV`**, and the page still carries `_NAV`. It is a destination
  you reach from a posting, not a tab — but a detail page with no way back is the one
  place its absence would be felt.
- **`/api/posting-letter` is in `_UPLOAD_ROUTES`; `/api/posting-letter/clear` is not.**
  The clear route carries two short strings, and joining the set would hand it a body cap
  of several megabytes. Both halves have a test.
- **`GET /api/document` is one route for four documents.** `_send_posting_file` is the
  shared containment check; a third and fourth copy of it is how one of them comes to be
  missing one.
- **A refused write writes nothing** — `{"ok": false}` at HTTP 200, no row and no file.
- **The log records counts, never content.** `answers=2`, not the answers: `state.db`
  holds prose you wrote, and the mail rule about subjects and bodies is the same rule.
- **`purge` keeps `posting_answers` and `application_submissions`.** The first is the one
  table here nothing can write a second time; the second is evidence behind an application
  that is itself kept. `posting_letters` mirrors `posting_resumes` — purged, and named by
  `purge_blockers` so it is in front of you first.
