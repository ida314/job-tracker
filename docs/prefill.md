# Prefill: opening an application with your answers already in it

Two halves, and the split is what makes the second one optional:

```
work --task prefill    offline.  Read the form, place the answers, name what is missing.
apply-to COMPANY JOB   a browser. Carry that plan to the page, attach the resume, stop.
```

The first runs in the nightly queue and needs no browser. The second is on demand, needs
Playwright, and is the only part that ever opens a window.

## Why it cannot be a link

The obvious design — a URL that opens the form prefilled, or a saved cookie that carries
your answers — does not work, and it is worth writing down why so nobody rebuilds it.

**No server-side draft exists.** Greenhouse, Ashby and Lever hold nothing for an
anonymous candidate. Filling a form in one browser mutates that browser's DOM and leaves
nothing behind, so there is no state for a cookie to point at. Hand yourself the cookie,
open the link, and you get an empty form.

**Query-parameter prefill is nearly unavailable.** Only Lever honours it — 2 of the 62
API boards here.

**No URL can attach a file.** The resume is the single most valuable field to fill, and a
link cannot do it under any design.

What actually fills a third-party form is code running on the page. That is how the
commercial autofill extensions do it; this does the same with Playwright rather than an
extension, so it lives in the repo instead of in a browser store.

The browser profile *is* persistent, and that does earn its keep — just not for prefill.
It keeps candidate-account logins alive between runs, which is the only thing that could
ever make the 35 `manual` companies tractable. Not used for that yet.

## answers.yaml

Curated, git-ignored, and the source of truth. `answers.example.yaml` is the tracked file
that documents the shape.

```yaml
identity:                 # canonical names; first_name, last_name, email are required
  first_name: Dylan
  email: dyd2008@nyu.edu
resume: ./resume.pdf      # a real file, checked at load

answers:
  work_authorization:
    value: "Yes"
    aliases: ["Are you legally authorized to work in the United States?"]
  current_employer: "New York University"     # a scalar is fine
```

Strict in the same way `profile.py` and `criteria.py` are: unknown key, unknown identity
field, missing name or email, empty answer, or a resume path that is not a file all
raise. A silently-defaulted answer would be typed into a real job application. The resume
is checked at **load**, not at fill time — discovering it is missing with a browser
sitting on an open application is the worst possible moment.

**`aliases` are the mechanism, not decoration.** An alias is the exact question text an
employer used. Write one down and every company that phrases the question the same way is
answered from then on with no model call at all. The gap loop below fills them in for you.

`Answers.hash` covers the answers and nothing else — the same contract
`Profile.prose_hash` has for judging. Add an answer and the plans that needed it rebuild;
edit a comment and nothing does.

## Where a form's questions come from

**Greenhouse publishes them, keylessly and completely:**

```
GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}?questions=true
-> questions[].fields[] : name, type, required, and every option of every select
```

That is 47 of the 62 API boards, and it is what makes gap detection possible without ever
opening a browser.

**Nobody else does.** Ashby's per-job posting-api answers 401 and its GraphQL
introspection is disabled; Lever's public postings API carries no custom questions.
Verified 2026-08-13. Their forms are read from the DOM on the first `apply-to` visit and
cached per company — forms are stable per employer, so one visit teaches the system that
employer's form permanently.

This is a real coverage limit and it is surfaced rather than hidden: a company whose form
we neither hold nor can fetch is **not counted as pending prefill work**, because it is
waiting on a browser visit, not on a model.

## The three passes

Only the last one costs a model call, and most nights there are none.

1. **exact** — a canonical ATS field name, or a label that means an identity field.
   `first_name`, `email`, and "Email Address" on a form that names nothing all land here.
2. **alias** — the normalized question text matched against a question already answered,
   here or at any other company. Pure string matching.
3. **model** — asked only about a label neither pass recognized: *"which of these answer
   keys, if any, answers this question?"* It replies with a key or `none`.

The model's role is bounded harder here than anywhere else in this repo. Its schema is an
enum of keys you already wrote plus `none`, so **it cannot produce text**. There is no
code path by which a sentence the model composed reaches a form field. It is the fourth
bounded model role in DESIGN.md §8, and the narrowest: level extraction reads a
description for a fact, ranking reads one for a judgment, this one only points.

A key it names still has to survive the rules: if the field is a dropdown and the stored
answer is not one of its options, it is a **gap**, not a fill. Typing "Yes" into a menu
offering "Authorized" and "Not authorized" would either fail silently or pick the wrong
entry, and both are worse than being asked.

## The gap loop

Anything unplaced becomes a gap: recorded once per question across every company that
asks it, and mirrored into the tail of `answers.yaml`.

```yaml
# ===== unanswered questions · regenerated by `jobtracker work --task prefill` =====
#
# how_did_you_hear_about_this_role:
#   value: ""
#   aliases: ["How did you hear about this role?"]
#   # asked by: Stripe, Ramp  ·  type: text
```

Commented out, deliberately: a live key with an empty value would load as an answer and
be typed into a form. Uncomment it, fill it in, and the next run stops listing it — and
fills that field at every employer that asks it, forever.

**Everything above the marker is yours and is never read, parsed, or rewritten.** The
block below it is regenerated wholesale from the database on every prefill run, which is
why answering a question makes its stub disappear on its own. The database is the truth;
the block is a rendering of it.

The Settings tab under `jobtracker serve` is the same loop with a text box, and it writes
through the same safe path (candidate file → parse → `.bak` → atomic swap) that criteria
edits use. Adding an answer there is text surgery rather than a YAML round trip, because
a round trip would delete every comment in the file — including the stubs you are working
through.

A **file** upload gets a different stub. You cannot answer "Attach" with a sentence, so it
names the `resume:` and `cover_letter:` keys instead of offering a `value:` to type into.

## The browser

```bash
pip install 'jobtracker[browser]' && playwright install chrome
jobtracker apply-to Cloudflare 7695702
```

Launches a persistent Chrome (falling back to bundled Chromium), navigates to the
application, fills what it knows, attaches the resume with `set_input_files()` — the one
thing no URL can do — outlines the required fields it could not fill, scrolls to the
first, and **stops**.

**It never submits.** There is no click path in `browser.py` at all, and a test asserts
it against the source: no `.click(`, no `.press(`, no `requestSubmit`, no `dispatchEvent`.
An application is irreversible and goes out under your name, so the last action is yours.

It also **discovers**. Every input, select, textarea and contenteditable on the page is
read, its label resolved through four conventions in order (`aria-label`,
`aria-labelledby`, `label[for]`, a wrapping label, then a nearby label-ish element, then
the placeholder), and stored against that company. That is what puts Ashby, Lever and
even a Workday portal into the same gap loop as Greenhouse, with no API involved.

Three things learned from live forms, all now handled:

- **Greenhouse's current UI sets no `name` attributes.** Everything is keyed off `id`,
  including `id="resume"`. Reading only `name` found the file input and could not
  recognize it — the resume, the most valuable field, silently went unattached. Field
  keys are `name` → `id` → a slug of the label.
- **One question can be several inputs.** A combobox renders as a text input plus a
  hidden select, and "Resume/CV" as a file input plus a textarea, either of which
  satisfies it. Once any of them holds the answer the question is answered, and its
  siblings are not reported as gaps.
- **Some employers redirect the hosted board to their own careers site.** Stripe's
  `absolute_url` is `stripe.com/jobs/search?gh_jid=…`, a search page with no form on it;
  their `job-boards.greenhouse.io` URL redirects there too. For Greenhouse the canonical
  board URL is used when the slug and job id are known, and **finding zero fields is
  reported as "no application form found", never as "0/0 filled, nothing left to do."**
  Absence read as success is the failure this project exists to avoid (DESIGN.md §3.4).

Under `jobtracker serve`, the Today tab's "Open prefilled" button does the same thing.
It runs on a daemon thread and returns immediately, because `server.py` handles one
request at a time and driving a browser inline would freeze the page for as long as the
window stayed open. Playwright's sync API is fine on a plain thread; what it must not do
is run inside an asyncio loop, which is why the task runner (async) and the browser
(sync) are separate modules.

The static dashboard shows the prefill counts — `prefill 13/16 fields · 3 need you` —
and **no button**. The counts are useful offline; a button that cannot drive a browser is
worse than no button, the same rule the disposition buttons follow.

## Checking its work

```bash
jobtracker work --task prefill --dry-run          # what it would plan, and why not more
sqlite3 data/state.db 'select company, fields, gaps from prefill_plans order by gaps desc;'
sqlite3 data/state.db 'select question_key, seen_on from prefill_gaps where resolved_at is null;'
jobtracker apply-to Acme 123 --headless           # discover and report, no window
```

A plan with a suspiciously high `gaps` count usually means a form full of questions you
have not answered yet — that is the first-visit state, and it drops sharply once the
common ones (work authorization, sponsorship, current employer) are in the bank. A plan
with `fields` of 0 means the form was never read: check whether that company publishes
its questions, and if not, visit it once with `apply-to`.
