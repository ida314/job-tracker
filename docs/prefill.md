# Prefill: opening an application with your answers already in it

Two halves, and the split is what makes the second one optional:

```
work --task prefill    offline.  Read the form, place the answers, name what is missing.
prepare                offline.  Do that for exactly the postings `today` will surface.
Rebuild prefill        offline.  The same, for one posting, on demand, from the page.
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

You do not have to write it by hand. **The Settings tab under `jobtracker serve` collects
it**: filling in your name and email there creates the file, and the resume upload beside
it stores the file and points `resume:` at it. That is the bootstrap — before it existed
the only way in was `cp answers.example.yaml answers.yaml`, and the example ships Ada
Lovelace's name and email as documentation, so the copy that never got edited would have
typed a stranger's identity into a real application. The starter it writes instead carries
the explanatory comments and **no placeholder values at all**.

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

**They are shown in two lists, because they are worth two different amounts of your time.**
A question nine employers ask is answered once and fills nine forms forever; a question
only Stripe asks is worth exactly one application. Generic means a canonical or common
field, *or* asked by two or more employers, and the generic list sorts by how many ask —
which is the order in which answering pays. Everything else is grouped under the one
company that asked it.

That rule needs no new state and no maintained list: a question migrates into the generic
list on its own the day a second employer asks it, the same way `tuning`'s suggestions
avoid a hand-maintained blocklist. And it decides which list a question is *rendered* in
and nothing else — no write and no fill reads it — so a misfiled question costs ordering,
never correctness. The stub block in `answers.yaml` uses the same ordering, because both
are renderings of one table and should not disagree about what to do first.

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
edits use. Every write there — an answer, an identity field, the resume path — is text
surgery rather than a YAML round trip, because a round trip would delete every comment in
the file, including the stubs you are working through.

Two details of the identity writer, both learned from real files:

- **A commented-out field is filled in place.** The example ships the optional keys as
  `# phone: …` documentation. Appending a live entry above one would leave two lines that
  disagree, only one of which is what gets typed.
- **Clearing a field removes the key rather than blanking it.** An empty value would load
  as an answer and be typed as the empty string, which in a submitted form is
  indistinguishable from a field nobody had an answer for.

The resume upload is checked by **content**, not only by name: the extension has to be
`.pdf` or `.docx` and the bytes have to start the way that format starts. The name the
client sends is never used as the filename — only its suffix is read, and the file is
always written as `resume<ext>` beside the bank. Whatever lands there is attached to a
real application and read by a person.

Order matters in that write, and it is the reverse of the obvious one: the file lands
first, then the `resume:` key. `load_answers` refuses a resume path that is not a real
file, so writing the key first would guarantee a refused write.

A **file** upload gets a different stub. You cannot answer "Attach" with a sentence, so it
names the `resume:` and `cover_letter:` keys instead of offering a `value:` to type into.

## A resume for one posting

The bank's `resume:` is the default for everything. A posting may override it — a resume
tailored to an infra role need not go to a platform one.

Upload it from the pick card under `serve`. It travels the way the Settings resume does —
base64 inside the one JSON POST path this server has — which matters because the browser
that fills forms runs on the machine running `serve`. On a headless host that machine's
file picker shows *its* disk, not yours, so this upload **is** the file transfer. (noVNC is
not an alternative: generic VNC has no file transfer at all, and the rule that the app
never manages the viewer is not worth breaking for one.)

```
data/resumes/
  acme_7695702_1f4c9a02.pdf     <- the name this repo minted, not the one you uploaded
```

Three rules, all of them the same rules the Settings upload already follows, plus one that
is new:

- **The filename is minted here** — `<company>_<job>_<digest8><suffix>`. A filename is
  attacker-shaped input even when the attacker is you with a badly named file, and this
  one names a file that goes out with a real application. The digest of the exact pair is
  what stops two postings whose names slug alike from overwriting each other.
- **Validated by content**: suffix allowlist first (so `cv.exe` is told it is not a
  resume, rather than that its base64 failed), then magic bytes, then the size cap.
  `resumes.validate_upload` is one function, called by both upload routes.
- **A recorded file that has gone missing reads as no override**, and logs. Raising would
  surface with a browser already sitting on an open form.

**The override is never part of `Answers.hash`.** The hash covers the bank; a new column,
`prefill_plans.resume_key`, covers the posting. Two questions, two columns — folding the
override into the hash would make every plan built with one look permanently stale and
re-plan it every night forever. What puts the posting back in the queue instead is one
disjunct in `matches_needing_prefill` comparing the two stored columns, which is also why
attaching a resume re-plans **that posting and no other**.

Carrying it to the page takes two steps, not one, and the second is not optional:
`browser._plan_index` lets a stored plan value win over a fresh `resolve_field`, so
swapping `answers.resume` alone would still attach the bank's file wherever a plan already
existed. `retarget_resume` rewrites the plan's resume entries too. Both are applied in
`serve` **and** in `jobtracker apply-to`, so the button and the terminal cannot disagree
about which file went out under your name.

## Rebuild prefill

A button on the pick card. It re-plans that one posting against the answers, the bank's
resume, and the posting's own resume as they stand right now — so answering a question in
Settings and seeing the count move does not mean waiting for tonight.

**It opens no socket.** This server handles one request at a time, so a form fetch (rate
limited) or a router call (a 180-second timeout) would freeze the page for every other
tab. "The router is down" is therefore not one of this endpoint's states, because there is
no router in its path.

That is far less of a downgrade than it sounds. Every key the model has ever matched was
written onto `form_fields.question_key`, and `known_question_keys` replays those as alias
hits — the same mechanism `browser.fill_application` already relies on. The only thing a
full run can still do is ask about a field the model has *never seen*, so this pass's gap
count can only be equal to or higher than the nightly one. It can understate readiness; it
can never overstate it.

Two refusals rather than a misleading success:

- **No form learned for that company** → it says so, and says to press Open prefilled
  once. It never fetches. Reporting `0/0 · nothing left to type` for a form nobody has
  read is the absence-as-success failure DESIGN.md §3.4 exists to prevent.
- **No answer bank** → it names Settings.

One implementation note worth keeping: the unit is constructed directly rather than
filtered out of `task.pending()`. `matches_needing_prefill` excludes a plan whose
`answers_hash` already matches, so routing the button through the queue would make it
silently do nothing in exactly the case it exists for — the same shape as the Open
prefilled regression. `PrefillTask.apply` still does the writing, so the button and the
nightly run cannot drift about what a plan is.

## The browser

```bash
pip install 'jobtracker[browser]' && playwright install chrome
jobtracker apply-to Cloudflare 7695702
```

Launches a persistent Chrome (falling back to bundled Chromium), navigates to the
application, fills what it knows, attaches the resume with `set_input_files()` — the one
thing no URL can do — outlines the required fields it could not fill, scrolls to the
first, and **stops**.

**It never submits on its own.** `browser.py` holds exactly one click, in `_submit`,
reachable only from the hold loop and only once you have armed it on `/apply` — and a test
asserts that against the source: one such call, in that function, with `requestSubmit`,
`form.submit`, `dispatchEvent` and `keyboard.press` still absent. Nothing in the fill
itself can reach it. An application is irreversible and goes out under your name, so the
last action is yours; what changed in 2026-08-22 is where you take it, not whether you do.

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
- **Some employers redirect the hosted board to their own careers site — and this was
  wrong twice in the same direction.** Stripe's `absolute_url` is
  `stripe.com/jobs/search?gh_jid=…`, a search page with no form on it, so the canonical
  board URL `job-boards.greenhouse.io/{slug}/jobs/{id}` was used instead (2026-08-13).
  **That board redirects too.** Asana's 302s to `asana.com/jobs/apply/…`, a JS shell whose
  form is a cross-origin iframe; the browser found zero fields and the card said *"no
  application form found"* about a job with 32 of them. Measured across every Greenhouse
  board we track on 2026-08-19: **25 of 45 do not carry the form at the board URL**
  (airbnb, asana, betterment, brex, coinbase, databricks, datadog, dropbox, lyft, mongodb,
  okta, pinterest, stripe and more; Cedar's answers 403 there). Greenhouse is now pointed
  at `job-boards.greenhouse.io/embed/job_app?for={slug}&token={id}` — the form itself,
  keyless, complete with the employer's own submit button, and carrying it on all 45.
  **Finding zero fields is still reported as "no application form found", never as "0/0
  filled, nothing left to do."** Absence read as success is the failure this project
  exists to avoid (DESIGN.md §3.4).
- **The form is not always in the main frame.** `page.evaluate` runs in the main frame and
  nowhere else, so an employer that embeds its ATS — ordinary practice, not exotic — reads
  as having no form at all. `_discover` falls back to the frames and returns the *surface*
  the fields were read off, which every later write, highlight and re-reading then uses:
  a handle minted in one frame names nothing in any other. The apply URL now lands on the
  form directly, so this is the second line of defence, not the first — but zero
  discovered is the one reading this project may never take at face value.

Under `jobtracker serve`, the Today tab's "Open prefilled" button does the same thing.
It runs on a daemon thread and returns immediately, because `server.py` handles one
request at a time and driving a browser inline would freeze the page for as long as the
window stayed open. Playwright's sync API is fine on a plain thread; what it must not do
is run inside an asyncio loop, which is why the task runner (async) and the browser
(sync) are separate modules.

That thread cannot report back to the click, so the endpoint answers everything knowable
first — is there a posting, an answer bank, a Playwright to import, a window already
open — and the card prints what came back. Only once those pass does the thread start,
and it then **blocks until you close the browser** (`hold=True`). Both halves are there
because both were missing: the click had no handler at all on the dashboard page, and
the thread it should have started closed the window as soon as the last field was
filled. Either one alone looks identical from the outside — a button that does nothing.

The window needs the optional extra: `pip install 'jobtracker[browser]'` and
`playwright install chrome`. Without it the button says so rather than pretending.

**The window opens where `serve` runs.** Not where the page is being viewed — Playwright
drives a browser on the server's own display. That is also why holding the window open
has to wait *inside* a Playwright call: the sync API dispatches events only while one is
in flight, so a plain sleep loop never learns that the window closed.

On a headless host that leaves a window nobody can see, and **that is now the intended
state**. It still needs a display — point `$DISPLAY` at an X server not attached to a
monitor (`Xvfb :100`), because Chromium will not launch headful without one — but nothing
carries that display anywhere and nothing in the app links to it.

**The viewer is gone** (2026-08-22). `JOBTRACKER_BROWSER_VIEW_URL`, the "View window" link
on the Today card, and the one on this page were all deleted. They existed because the
window was where you did the two things the mirror could not do: read the application over,
and submit it. The first is what the full-page preview is for. The second is now `/apply`'s
Submit button. What is left — a remote X server shipping video frames for fifteen text
fields — was only ever the slow path, and keeping a link to it advertised an interface that
does not work well enough to use.

The static dashboard shows the prefill counts — `prefill 13/16 fields · 3 need you` —
and **no button**. The counts are useful offline; a button that cannot drive a browser is
worse than no button, the same rule the disposition buttons follow.

## Filling it in: `/apply`

**The window is no longer where you type.** `serve`'s "Open prefilled" navigates to
`/apply`, which renders one HTML field per field discovered on the real form. You type
there, the value is pushed to the real browser, and a screenshot every couple of seconds
shows what the page actually looks like. The window still exists, and it is still the only
place an application can be submitted from.

The reason is latency. The window opens where `serve` runs, so on a headless host you were
watching it through VNC — a remote X server shipping video frames for a task that is
fifteen text fields. That is slow because it is video, not because it is badly tuned. The
fields, meanwhile, were already known: `_DISCOVER_JS` tags every input, `_fields_from_dom`
names them, and `_write` puts a value into exactly one of them. All that was missing was a
channel from the page you are looking at to that writer.

```
Cloudflare — Backend Engineer, New Grad          [Read the form again]

┌─ preview ───────────── Pause ─┐   First Name   [ Dylan          ]  filled
│  [jpeg of the real form]      │   Resume/CV    [ Choose file    ]  filled
│                               │   Work auth?   [ Yes         ▾  ]  filled
└───────────────────────────────┘   Why us?      [                ]  needs you
                                      ☐ also save to my answer bank as `why_us`
Review & submit
View window ↗                       3/4 fields filled · 1 need you
```

### How it works

`jobtracker/live.py` holds one `Session`: the mirrored rows, a command queue, and the
latest screenshot. It is pure — no Playwright, no HTTP, no SQLite — so `browser.py` and
`server.py` both import it, neither imports the other, and the whole mechanism is testable
with neither a browser nor a socket.

The fill publishes into it and then holds the window as before. `_hold_until_closed` was
already ticking every 500ms inside `page.wait_for_timeout`, and it now drains the queue in
that same tick. That placement is not incidental: **Playwright objects belong to the
thread that made them**, and the drain is the only code that touches `page` outside the
fill. An HTTP handler must never call into it.

So a write from the page is queued and answered immediately, and the outcome arrives on
the next poll. Same shape as `apply-to` itself, for the same reason — this is
`HTTPServer`, one request in flight, and blocking on a browser would freeze every tab.

### Rules that are load-bearing

- **A command points, it does not write.** The vocabulary is exactly five names — `set`,
  `clear`, `rediscover`, `shoot`, `highlight` — and a command carries a field *handle*,
  never a selector and never anything the browser thread evaluates. That is `browser.py`'s
  no-click-path rule carried across the new channel, and it has its own test.
- **Deleting is `clear`, and it is not `set` with an empty value.** Two reasons, both
  real. A `file` row's value is a path on this machine, so `""` there means *no file*
  rather than *no text*, and the one field where the two readings differ is the one
  holding your resume. And an empty `set` used to succeed — `page.fill(el, "")` is
  exactly how Playwright clears a field — leaving the row recorded `filled` while holding
  nothing: counted as done, counted out of "need you", and indistinguishable from a
  question nobody ever answered. That is the reading `answers.yaml` refuses for the same
  reason, here on a form that is about to be submitted. So an empty `set` is refused at
  the endpoint, `clear` has its own status (`cleared`), and that status counts as needing
  you.
- **Every kind of control can be emptied, not just text.** `_clear` is the inverse of
  `_write` branch for branch — `fill("")`, `select_option([])`, `uncheck`,
  `set_input_files([])` — because three of the four had no path at all: unticking a
  checkbox was recorded "would not take it" and did nothing to the page, a dropdown's
  blank option was dropped client-side, and a file input is the one control a browser
  gives you no way to empty, which is why a file row that holds something renders a
  **detach** button.
- **A handle is only valid for the discovery that minted it.** `_DISCOVER_JS` renumbers
  `jt0…jtN` from scratch on every pass, so once the form changes shape the same handle
  names a different input. Every command carries the `epoch` it was written against and is
  dropped on a mismatch — on the browser thread, where it cannot be bypassed. This is the
  one way this feature could put an answer you did not give into a field you cannot see.
- **The epoch moves only when the handles actually moved.** A successful write re-reads
  the form, because forms reveal questions once you answer others. If that always bumped
  the epoch, the second field you typed would be refused because the first one succeeded —
  every edit poisoning the next. `live.signature` asks the one question the epoch is
  about: does every position still report the same field under the same handle.
- **The preview is the whole page, not the window.** Chromium's viewport is 720px tall and
  an application form is several thousand — Asana's measured 1280x3352 — so a
  viewport-shaped shot showed five fields of thirty-two, over a window nobody looking at
  this page can scroll. `_shoot` passes `full_page=True`; the page renders it scaled to
  fit and a Fit/100% toggle switches to full width inside a scrolling box. **That toggle
  is two CSS classes and nothing else** — no command, no endpoint, no session state, since
  the capture is always the whole page either way. The cadence moved 2s → 4s to pay for
  the bytes (190 KB / 113 ms against 22 KB / 36 ms, measured 2026-08-19), which the design
  can afford: the picture is meant to be behind, and the fields are not.
- **No screenshots for a page nobody is looking at.** Each poll refreshes a deadline and
  the drain shoots only inside it. That is also the Pause button, and the closed-tab case.
- **`img-src 'self'` is as load-bearing as `connect-src 'self'`.** The CSP is
  `default-src 'none'`, both fall back to it, and both fail the same silent way — the
  preview would be a broken image over a browser working perfectly.
- **The page can submit, and it is a gate rather than a command.** An application is
  irreversible and goes out under your name, which used to be the reason there was no
  control at all; it is now the reason there are three checks in three places. `submit` is
  deliberately **not** in the vocabulary — nothing a queued request carries may activate
  anything — so it is a session-level flag, armed by `request_submit` (phase `ready`,
  epoch matching, a submit control found, every required field filled, the company name
  typed), re-checked in full on the browser thread before the click, and spent exactly
  once by `claim_submit` under the lock.
- **The click is a real press of the employer's own button.** `browser.py` has exactly one
  click, in `_submit`, reachable only from the hold loop; `requestSubmit`, `form.submit`,
  `dispatchEvent` and `keyboard.press` stay banned. Pressing the control runs the
  employer's validation, their required-field checks and their captcha hooks. Submitting
  the form programmatically would skip them, which is how an application their own page
  would have rejected goes out anyway.
- **Zero submit controls is "no submit button found".** The same finding as zero fields
  discovered, one control along, and the page renders no button rather than a disabled one.
- **What happened after the click is reported, not assumed.** Nothing here can prove an
  employer received anything, so the page says what changed — the URL, the field count —
  and says plainly when nothing did.

### What it cannot do

- **Captchas.** They happen in the window, and nothing links you to the window any more.
  A form that raises one is a form this cannot finish; the preview shows you that it did,
  and the fallback is a display you reach at the deployment layer, not through this app.
- **Fields the DOM pass cannot see.** `_DISCOVER_JS` skips anything with no `offsetParent`
  — a collapsed section — and anything that is not a real input, such as a rich-text
  editor or a drag-and-drop dropzone. The page prints what it read and says so; it must
  never imply the list is the whole form. Zero fields is still *"no application form
  found"*, never "nothing left to type".
- **A form that rewrites itself while you are in it.** Re-reading after each write narrows
  the window; when the shape does change, the page says so and stops rather than pushing
  into whatever is there now. "Read the form again" is one click.

### Ending a session

**Done — close the window** on `/apply` is the way out, and on a headless host it is the
only one. The browser opens on the machine running `serve`; if you cannot reach that
machine's screen, you cannot close the window, and until it closes `_APPLY_LOCK` stays
held and every later "Open prefilled" answers *"a prefilled window is already open"* —
until `serve` itself is restarted. Observed 2026-08-19.

`POST /api/session/close` sets `Session.closing`; `_hold_until_closed` reads it in the
tick it was already doing, breaks, and `fill_application` closes the context on the way
out, which releases the lock.

**The page has to land that, and at first it did not** (fixed the same day). The window
closed, the lock freed and the log said so, while the page sat on *"closing…"* with every
field still accepting input — indistinguishable from a hang. A closed phase, or a session
that has gone entirely, now unhides the `#gone` banner, disables every control and **stops
the poll**; `render_apply` emits the same state server-side so a reload with no script is
just as honest. **And the button confirms first**, because closing discards the fill: no
ATS keeps a draft for an anonymous candidate, which is the same fact that makes this a
browser rather than a link.

Two things about the endpoint's shape:

- **It is not a `live.Command`.** The vocabulary is what a web request may do to the
  *form*, and closing the window does nothing to the form. Keeping it out leaves that
  closed list — and the test on it — meaning exactly what it says.
- **It is not conditional on the phase.** A session stuck part-way through filling is
  precisely when you want the window gone, and it is also the state most likely to be
  holding the lock. `submit()` refuses commands once the phase is `CLOSED`; this must
  not.

None of this removes the need for `DISPLAY` or `Xvfb`: Chromium still has to draw
somewhere, and it will not launch headful without a display. What it removes is any reason
to look at what it draws.

## `prepare`: is tomorrow morning actually useful?

```
$ jobtracker prepare
Tomorrow: 2/3 ready to apply to

  1. Cloudflare — Backend Engineer, New Grad
       prefill 6/13 fields · 7 need you
  2. Ramp — Software Engineer, Platform
       NOT READY — ashby does not publish its form — run
       `jobtracker apply-to Ramp abc123` once to learn it
  3. Figma — Backend Engineer
       prefill 11/11 fields · nothing left to type
```

Exit 0 when every pick has a plan, exit 2 when one does not — because a pick with no plan
is the state that leaves you opening a blank form. **Gaps never cause exit 2.** A form
with questions you have not answered is the normal state, especially in the first weeks,
and failing on it would leave a nightly unit permanently red for something only you can
clear. That is the dbt-Labs-is-legitimately-empty rule applied here.

Each not-ready line names its own reason, because three situations look identical in the
database and want different things from you: no answer bank, an ATS that publishes no
form (go visit it once), or a router that was down.

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
