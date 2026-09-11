# Cover letters, without letting a model write one from nothing

`coverletter` reads a posting's description and your resume, and writes the body
paragraphs of a letter into the slots **your own template** marks out. `jobtracker
coverletter build` compiles the result to a PDF. Nothing it produces reaches an employer
until you download it and attach it yourself.

It is the **sixth bounded model role** (DESIGN.md §8) and the second that composes prose,
after `tailor`. It composes considerably more of a document than `tailor` does, so most
of this page is about what it cannot do.

```
work --task coverletter   the model.       Read a description and a resume, write paragraphs.
coverletter build         deterministic.   Splice them into the template, compile a PDF.
```

## Switching it on

It is a plugin, and it ships off:

```bash
jobtracker plugins list                   # coverletter [disabled] (task)
jobtracker plugins enable coverletter
jobtracker work --task coverletter
```

`plugins disable coverletter` takes it out of the queue entirely — absent, not
"unavailable", because switching something off is a decision you made and there is
nothing to go and fix.

## Your template is the interface

`$JOBTRACKER_COVERLETTER_TEX`, defaulting to `coverletter.tex` beside the resume; on a
deployed box it is `/data/coverletter.tex`. Gitignored and personal, like `answers.yaml`
and `resume.tex`. `coverletter.example.tex` is the tracked file that documents its shape.

**A slot is what the template already looks like.** There is nothing to mark up:

```latex
% Paragraph 2:
% Use the strongest and most directly relevant professional experience.
% Select one or two concrete accomplishments from the resume.
In my work at MOST RELEVANT COMPANY OR EXPERIENCE, I MOST RELEVANT ACCOMPLISHMENT.
```

A run of `%` comment lines is the **brief**, handed to the model verbatim. The prose
beneath it, carrying a PLACEHOLDER in capitals, is what gets replaced. So you steer the
letter by editing the template, in LaTeX, and there is no prompt in this repo to go and
find — rewriting a brief is how you change what that paragraph argues.

Three rules fall out of that, and they are the ones worth knowing before you edit:

- **Prose with no placeholder is not a slot.** The shipped template's closing line —
  *"Thank you for your consideration"* — carries a comment and no capitals, so it is fixed
  text and comes through byte for byte. That is how you pin a sentence you always want
  said exactly one way.
- **Everything outside `\begin{document}` is out of reach.** Your name, your contact
  line, the packages and the page geometry cannot be addressed by any slot, by
  construction — the same safety `apply_edits` gets from refusing to insert.
- **A brief is half the question.** The template's hash is in the task's `unit_key`
  alongside the resume's, so editing one comment re-asks every posting with a clean retry
  count. That is not a cost to avoid; it is the mechanism.

`\begin{document}` is located in **code, never in a comment**. A template that explains
itself — *"everything outside `\begin{document}` is out of reach"* written at the top — would
otherwise have that sentence taken as the document start, dragging the preamble into slot
range. The tracked example did exactly that on its first parse, and there is a test named
after it.

### The engine

Compilation is tectonic, through the same `resume/assemble.py` that builds a tailored
resume — one subprocess in this repo, with a scratch directory, a timeout, `--untrusted`
and no shell.

`\input{glyphtounicode}` and `\pdfgentounicode=1` are **pdfTeX** primitives that XeTeX
does not have, so a template carrying them unguarded dies at
`glyphtounicode:7: Undefined control sequence` on *every* build, with no PDF. Guard them:

```latex
\ifdefined\pdfgentounicode\input{glyphtounicode}\fi
\ifdefined\pdfgentounicode\pdfgentounicode=1\fi
```

Text still extracts from a tectonic build without them, so ATS parsability is not lost.
This is the same fix `data/resume.tex` already carries, found the same way.

## What the model may say

```json
{"paragraphs": [{"key":      "p1",
                 "text":     "the paragraph, as plain text",
                 "evidence": "a phrase copied verbatim from the description or resume"}]}
```

And that is all of it. Three bounds hold it:

### The model never writes LaTeX

Not "writes it under an allowlist" — never. `letter.escape_text` turns every reserved
character into its escape, **the backslash first**, so `\input{/etc/passwd}` arrives in
the document as the literal characters `\input{/etc/passwd}` and there is no control
sequence left to permit or refuse.

This is a *stronger* guarantee than `resume/latex.py` gives `tailor`, and it can afford to
be. `tailor` edits lines of a document you wrote, so it has to let `\resumeItem{...}`
through and therefore has to reason about commands — hence the allowlist, the
context-widening, and `NEVER_ALLOWED` inside it. Nothing here needs a macro to pass
through, so nothing here has a list to keep current and no `\csname` can compose a command
out of characters.

### The facts are not the model's to state

`COMPANY`, `JOB TITLE` and `DATE` are substituted by `letter.fill`, from the posting row
and the run's `today`. The model is told not to write them and is never asked for them. A
letter addressed to the wrong employer is the one mistake a cover letter does not survive,
and it is not a thing to ask a language model to get right when the database already knows.

The substitution runs on **the template's own text only**, never on a composed paragraph.
`COMPANY` is an ordinary English word: walking the whole document once after splicing —
the obvious way to write it — would let a letter saying *"we discussed COMPANY at length"*
have that word rewritten into the employer's name by a step the model is not part of.

### Every paragraph points at a real sentence

`evidence` must occur verbatim in the job description **or** in the resume — the `inbox`
grounding rule, widened by one document because a paragraph about your own experience is
grounded in the resume and a paragraph about the role is grounded in the posting. Which
one it was found in is stored as `source`.

Plus the refusal `tailor` already applies: a paragraph carrying a term on
`keywords.yaml`'s `denied` list is refused in Python, where a prompt cannot argue with it.

## A letter is all-or-nothing

`parse_edits` keeps the edits that survive and drops the rest. `parse_letter` does the
opposite: **either every slot came back grounded, or nothing is written.**

The reason is `fill`. An unanswered slot keeps its placeholder prose, so a letter missing
paragraph 3 compiles to a PDF with `RELEVANT ROLE THEME` printed in the middle of it.
Writing that row down would put a download button on the page over a document nobody can
send — and the posting would leave the queue, so it would never be asked again either.
Returning None keeps it pending, and three failed nights set it aside through the ledger
like any other unit.

The same asymmetry explains the denied-term refusal. In `tailor` a denied term drops one
edit and the others still compile, so the refusal is *written* as an empty proposal to
drain the unit. Here there is no rest: one bad paragraph is a letter that cannot be built,
and re-asking is the only way to get one that can.

## Where it lands

`jobtracker coverletter build` writes `$JOBTRACKER_LETTERS/<stem>_letter.pdf`, alongside
the tailored resumes. The stem is `letter.letter_stem`, derived and never stored — the
file's own existence is what "this posting has a letter" means, which is why the four
callers that reach for it share one expression. The `_letter` suffix is what keeps it off
the tailored resume for the same posting; both are minted from the same
`resumes.stored_name` pair.

Under `serve`, every posting row that has a letter carries an **✉** in its actions cell —
a build button until the PDF exists, a download link afterwards. It sits beside the
tailored resume's **↓** and follows the same two rules: absent entirely when nothing has
been written, and interactive-only, because a dead button in a mailed file is worse than
no button.

**The date on the letter is the day it was compiled**, not the day you send it — `fill`
takes `today` from the run that built the PDF, and a PDF that is already current is not
rebuilt. So a letter built tonight still says tonight's date when you download it next
week. That is usually right (it is when the letter was written) and occasionally not;
`jobtracker coverletter build --rebuild` recompiles everything and re-dates it.

There is nothing to *accept*. `resume_suggestions` has a `resolution` because a suggestion
is a proposal about a document you already wrote, so accepting and dismissing are real
states. A letter is a draft written for one posting and has no meaning at another: there
is nothing to re-propose, so `cover_letters` has no `resolution`, no dismiss command and
no attach path. Deleting the row is what "no thanks" means, and the next run writes
another.

## Cost, and where it sits in the queue

Priority **60**, behind `tailor` at 50 — last. Two reasons, both the argument `tailor`
already makes about itself, turned up:

- It is re-keyed by **two** documents you edit, the template and the resume, so there are
  two ways to re-key the whole queue at once.
- A unit is a page of composed prose against a resume and a description, where `tailor`'s
  is a handful of lines. It is the most expensive answer in the queue to spend a budget on.

It consumes nothing `tailor` produces — both read the same scored matches, neither waits
on the other — so the order between those two is a cost argument and nothing else.

## What this does not do

- **It does not check facts.** Grounding proves each paragraph can quote a real sentence
  from a real document; it does not prove the paragraph's claims follow from it. The
  prompt is emphatic that every employer, project, metric and date must already be in the
  resume, and a prompt is a request. `denied` is the only part of this that is a bound.
  **Read the letter.**
- **It does not send anything.** Building and downloading are not accepting, which is the
  whole of why they are allowed from the actions cell.
- **It never writes to your template.** Paragraphs are spliced into a copy in memory.
