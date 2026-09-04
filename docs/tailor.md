# Tailoring a resume, without letting a model write one

`tailor` reads a posting's description and your resume, and proposes small, grounded edits
to the resume. `jobtracker tailor build` applies them and compiles a PDF. Nothing it
produces reaches an application until you say so.

It is the **fifth bounded model role** (DESIGN.md §8) and the first that composes prose,
which is why most of this document is about what it cannot do.

```
work --task tailor   the model.       Read a description and a resume, propose edits.
tailor build         deterministic.   Apply them, compile a PDF, write it to disk.
tailor build --attach                 ...and make it that posting's resume.
```

## Switching it on

It is a plugin, and it ships off:

```bash
jobtracker plugins list                  # tailor [disabled] (task)
jobtracker plugins enable tailor
jobtracker work --task tailor
```

`plugins disable tailor` takes it back out of the queue entirely — not "unavailable", but
absent, because switching something off is a decision you made and there is nothing to go
and fix. There is no `plugins purge tailor`: purge removes postings a *feed* imported, and
a model role imports nothing.

## Your resume is LaTeX

`$JOBTRACKER_RESUME_TEX`, defaulting to `resume.tex` beside `answers.yaml`. Gitignored and
personal, like the answer bank — a file you wrote, that the pipeline only ever reads.

LaTeX rather than the PDF, and the reason is not aesthetic. `tailor` has to read the resume
**as text**, and there is no text extractor in this repo. A `.tex` file is already text:
no PDF parser, no new dependency, and no argument about which one. Two things follow that
matter more than the dependency:

- **The model quotes lines back at you.** A PDF extractor's idea of a line is an accident
  of column layout, so a quote grounded in extracted text is grounded in something you
  never wrote.
- **The output is a diff.** You can read what changed, which is the whole point.

A resume in a format nothing handles is reported as an absence — `tailor` says it is
unavailable and the rest of the queue runs.

## What the model may say

```json
{"edits":   [{"section": "experience|projects|skills|summary",
              "current_line": "a line copied verbatim from your resume",
              "suggestion":   "what to replace it with",
              "evidence":     "a phrase copied verbatim from the job description"}],
 "flagged": [{"term":     "a technology named in the job description",
              "evidence": "a phrase copied verbatim from the job description",
              "why":      "one sentence on what it would add"}]}
```

Seven refusals, applied per edit. An edit that fails any of them is dropped; if none
survive, nothing is written and the posting stays in the queue.

1. not JSON, not an object, or `edits` is not a list
2. a `section` outside the enum
3. **`evidence` does not appear in the job description, verbatim**
4. **`current_line` does not appear in the resume, verbatim**
5. a `suggestion` the format refuses to compile (see below)
6. a `suggestion` identical to the line it replaces
7. a `suggestion` carrying a technology you have **denied** (see below)

Rules 3 and 4 are the ones doing the work. They are `inbox`'s quote rule — which is
`repair`'s rule that a proposed slug must appear on the page it was read from — applied at
**both** ends. Rule 3 is what keeps an invented requirement off your resume. Rule 4 is what
keeps the page from telling you your resume says something it does not.

Grounding is checked whitespace-normalized, but `apply_edits` replaces exactly, so an edit
must pass both. One that passed the loose check and failed the exact one would render on
the page and then quietly do nothing when applied — a proposal you cannot accept, which is
worse than one that was never made.

`flagged` is the other half, and it has four refusals of its own: a term that is not a
term, a `term` or `evidence` that does not occur in the job description, a term already in
your resume or already ruled on, and a duplicate. `MAX_FLAGGED` is 4 — a page carrying
twelve new questions per job is one nobody answers.

**`MAX_EDITS` is 6**, and it is not a quality bound. A resume that moves twenty lines per
posting is not a tailored resume, it is a different resume each time — and every one of
those lines is a claim you have to stand behind in an interview.

## Keywords: what it may write, and what it may not

`tailor`'s grounding rules keep the *requirement* and the *resume line* honest and say
nothing at all about the technology names inside a suggestion. A description that says
"we use Kafka" is exactly the input that talks a model into writing Kafka onto your
resume, and the person defending that in an interview is you.

`keywords.yaml` is the answer, and it has two lists that work in opposite directions.

```yaml
allowed:            # goes into the prompt verbatim — the vocabulary it works in
  - PostgreSQL
  - Redis
denied:             # a refusal applied in Python, at every posting, forever
  - Kubernetes
```

**The asymmetry is the point**, and it is the one `overrides` has over `criteria.yaml`:
the list that *widens* what may be written is prose the model reads, and the list that
*narrows* it is code the model cannot argue with. A prompt is a request, not a bound.

**An empty `allowed` means unrestricted, not "nothing allowed."** Reading an absence as a
decision is the mistake this repo keeps naming, and here it would mean that installing the
feature silently switched tailoring off. The Settings card says which state you are in
rather than leaving you to infer it from an empty box.

Both lists are hashed into `tailor`'s `unit_key` alongside the resume hash, so ruling on a
term re-asks every posting — a proposal made under the old lists is not one made under the
new. Editing the file by hand works and so does the page; there is no separate reload.

### When it wants something you have not got

The model may not write a technology outside the list, so the honest answer to "we need
Kafka" is an empty edit and a question. That is `flagged`, and it is grounded exactly as an
edit is: the term and a phrase asking for it must both appear in the job description,
verbatim. An invented requirement cannot become a question about your resume.

Settings → **Resume tailor** shows every undecided term, grouped by term rather than by
posting — the decision is about the technology and you make it once, which is `split_gaps`'
argument about a question nine employers ask. Each carries the quote that asked for it,
because that quote is what makes Include a decision rather than a guess.

```
Include — I know this     the term joins `allowed`; every stored edit that was leaning
                          on it becomes compilable, with no model call
Exclude — I do not        the term joins `denied`; no suggestion carrying that word is
                          ever compiled again, whatever a posting says
×  (on a chip)            take the ruling back — the term becomes a question again
Finish                    rebuild the tailored PDFs your rulings just changed
```

**An undecided term holds an edit rather than dropping it.** The work already exists;
ruling on the term releases it, which is why Include costs nothing.

**Exclude blocks in two places, and both are needed.** Rule 7 drops a denied term out of
every *future* proposal. `split_edits` keeps it out of the ones already stored — and the
edit sitting in the table when you press Exclude is, by construction, the one that made
you press it. Without the second, "never write this again" would mean "never write this
again, except here". A held edit and an excluded one are labelled differently everywhere
they render, because one is a question and the other is a decision you already made.

A future proposal dropped by rule 7 is still written down as an empty one, because
otherwise the posting would be re-asked every night forever for the same answer.

**Finish is scoped to proposals that mentioned a flagged term**, because those are the only
documents a ruling can have changed; recompiling the corpus to prove the rest are identical
is minutes of TeX for nothing. It bounds itself at `FINISH_MAX` = 12 compilers per press and
names what it left, since a second press picks up the rest. Anything still undecided stays
held back and is named in the result.

Held edits are marked wherever they are visible: `/apply` renders them beside the diff with
no control (that page carries none, by design — the ruling is made on Settings), a posting
row whose every edit is held renders **held** instead of a build button, and `tailor build`
in the terminal says which terms it is waiting on.

## The LaTeX guard

A resume is compiled, so a suggestion is text about to be fed to a TeX engine, and TeX is a
programming language with filesystem access. `\input` reads a file into the document,
`\write`/`\openout` create one, `\catcode` and `\csname` rewrite what the rest of the source
means, and under `--shell-escape` `\write18` runs a shell command. Without a guard, "the
model writes a suggestion into a document we then compile" is "the model writes a program
we then run".

So `latex.sanitize` refuses any control sequence outside a small **allowlist** of the ones a
resume line actually needs. An allowlist rather than a blocklist because `\csname` composes
command names out of characters, so a blocklist is a guess and guessing wrong is quiet. It
also refuses unbalanced braces (they change where the enclosing group ends) and a bare `%`
(it comments out the rest of the physical line, including whatever the original line carried
after the part being replaced).

Three more things stand behind it, and none is redundant:

- `apply_edits` only replaces a line it was handed **verbatim**. It does not search, fuzzy
  match, or insert — which puts `\documentclass` and the packages out of reach by
  construction, and is why a document that compiled before compiles after.
- `assemble.py` runs the engine with `--untrusted`, in a scratch directory it is deleted
  from, under a timeout. TeX loops rather than erroring, and a nightly job that hangs is
  worse than one that fails.
- The compile is the only subprocess in this repo. There is a test asserting it stays that
  way.

## Assembling, and attaching

```bash
jobtracker tailor build                  # apply + compile, write to $JOBTRACKER_TAILORED
jobtracker tailor build --company Acme   # just one
jobtracker tailor build --attach         # ...and use each result as that posting's resume
jobtracker tailor dismiss --company Acme --job-id 42   # not these edits
```

`dismiss` keeps the row rather than deleting it, which is `mail_proposals`' rule: deleting
is what would let the next run propose exactly the same edits again. It comes back on its
own when the resume changes, because that is a different question.

`build` is **not a task**, and that is the `prefill` lesson rather than a naming choice:
`cmd_work` returns early when no router is configured, so a pass that needs no model would
silently do nothing on a night the GPU is down. It asks nothing of a model and always runs.

**Nothing writes to your resume source.** Edits are applied to a copy in memory, the result
is a new file under `$JOBTRACKER_TAILORED`, and `--attach` records it through the
per-posting override that already existed — `store.set_posting_resume`, exactly what the
dashboard's "Use for this posting" button writes.

It writes only that, and the rest is machinery that was already there. The stored prefill
plan goes stale on its own, because `matches_needing_prefill` compares `prefill_plans.
resume_key` against `posting_resumes.filename` and re-plans the row when they differ. And
at apply time `cmd_apply_to` and `server._api_apply_to` both run `prefill.retarget_resume`
over the stored plan, which is what stops `browser._plan_index` handing the browser the
resume the plan was built with. Attaching does not need to do either, and doing one of them
here would be a third place that has to agree with the other two about which file goes out
under your name.

Two outcomes that are reported rather than hidden:

- **A proposal whose lines have moved** applies nothing and says so. Your resume changed
  under a suggestion made against an older version of it; re-run `work` and it is re-asked,
  because the unit key is a hash of the resume text.
- **No toolchain** is named once, up front, and exits **0**. Every suggestion it holds is
  still good; a machine without TeX is a normal state, not a broken one.
- **"Nothing to assemble" says which nothing it means** — no proposals yet (go enable the
  plugin) or all of them dismissed (the system did what you told it). Saying the first
  about the second is the absence-read-as-a-cause mistake this pipeline keeps naming.

## Where it runs

Proposing runs anywhere — it is text. Compiling needs `tectonic`, which is in the **serve**
image and deliberately not the batch one: the nightly `check`/`work`/`prepare` never
compiles anything, and folding a toolchain into a 177MB image to carry something it never
runs multiplies the nightly pull for nothing. CI asserts the capability on the published
serve image, in the same shape it asserts Playwright, because a missing one otherwise looks
exactly like a working deployment until someone tries to use it.

## Getting a source in

`tailor` reads `$JOBTRACKER_RESUME_TEX` (default `resume.tex` beside the repo). Put one
there by hand, or use **Settings → Resume tailor → Source document**, which takes a `.tex`
upload and writes it to exactly that path.

That is a different field from **Resume** above it, and deliberately so: Resume holds the
PDF attached to an application, this holds the document tailoring reads and rewrites.

The upload is refused, with the reason named, if it is not `.tex`, is not UTF-8 text, or
carries no `\documentclass` — it is compiled standalone, so a fragment would fail much
later as a compile error. A PDF or DOCX renamed to `.tex` is called out as such, because
it is the likeliest mistake and the generic messages would send you to read your LaTeX.

Replacing the source re-asks every posting on its own: `tailor`'s unit key is a hash of
the resume text, so a changed file re-keys every unit at once, and a `dismissed` proposal
reopens because a ruling about the old wording is not a ruling about the new.

## Reading it

`/apply` renders the diff for the posting whose window is open: each edit as was/now, plus
the phrase from the job description it answers. That phrase is why the block is worth
rendering — it is a verbatim quote, so the page can say *why* an edit was proposed instead
of asking you to take it on trust. The Today card carries a count.

**Neither surface has a button, in either mode.** Accepting means attaching a document to an
application, and a control on either page would put a model-authored PDF one click from a
real application with the diff unread.

## Getting the PDF from the page

Every posting row outside the three picks — both tables on the All postings tab, and the
drawer under Today — carries a chip when `tailor` has proposed something, and beside it a
`↓`. Under `serve` only: the static dashboard renders no actions column, and the download is
a `/api/` href, which is as dead in a mailed file as a button would be.

The `↓` is a build button until the PDF exists and a download link afterwards, because
nothing stores the path — `resume_suggestions` has no path column, so the file's own presence
under `$JOBTRACKER_TAILORED` is what "built" means. Pressing it POSTs to `/api/tailor-build`,
which compiles on a daemon thread and answers `building` / `ready` / `error`; the page polls
the same endpoint, which starts at most one build per posting.

**This is not accepting.** A compile sends nothing to an employer and a downloaded PDF is a
document you then read. Attaching one to an application is still `tailor build --attach`,
after the diff has been read at `/apply`, which is where the reasoning is rendered.

Every refusal is named rather than generic, because the build runs where nothing can report
back to the click: no suggestions, dismissed ones, no resume source, no edit that still
applies, and — the one most people will meet — no TeX engine, since tectonic is in the serve
image only.

## Why `resume_suggestions` has exactly one reader

The table is read by the page that shows it to you and by nothing else. Nothing joins it
into prefill, ranking, or matching.

That is DESIGN.md §8.1 rather than an accident of there being nothing else yet. The role
removed there was **more** tightly bounded than this one — an enum of answer keys, no free
text at all — and was still wrong, because `apply` cached its choices onto
`form_fields.question_key` and the deterministic rules replayed them at every later company.
A model that writes into a cache the rules read back is not off the main loop; it is on it,
one night later. Ask where an answer is *stored*, not just where it is produced.

## Priority 50, last

`tailor` consumes what `judge` produces — it works down scored matches — and feeds nothing.
It goes behind `inbox` on the starvation argument, one notch up from `inbox`'s own: its unit
key is a hash of the resume text, so editing one line re-keys **every** posting at once.
Ahead of the mailbox, a single save would push it behind several hundred units.

## Known limits

- **It only edits lines.** It cannot add a bullet, reorder a section, or tell you the
  resume is missing something the posting asks for. That is a real limit and a deliberate
  one: insertion has no line to anchor to, and an anchor is the whole bound.
- **It cannot check that a suggestion is true.** The prompt says to keep your own facts and
  the grounding rules keep the *requirement* honest, but nothing here can verify that you
  did what a rewritten bullet says you did. Read the diff.
- **The keyword guard is not a hallucination detector.** `denied` is exact and reliable —
  a word on that list cannot reach a document. `allowed` is a prompt, so a model can still
  write a technology name that is on neither list; nothing here knows which strings are
  technologies, and a list of every one of them is not a thing this repo has. What the two
  lists give you is a growing, *decided* set: every term you have ever been shown becomes
  permanent, and the ones you rule out cannot come back. Read the diff.
- **A resume that is one line per paragraph** gives it little to work with; one that wraps
  a bullet across source lines gives it less, because `current_line` has to match text that
  exists. Both are formatting choices in your `.tex`, and neither is detected.
