# Where saved documents go

Four destinations, one per document this repo compiles for a posting, every one
defaulting to `~/Downloads` and every one set on its own.

```
↓      the tailored resume, compiled          →  resume
✉      the cover letter, compiled             →  letter
tex    the LaTeX the ↓ is compiled from       →  resume_tex
tex✉   the LaTeX the ✉ is compiled from       →  letter_tex
```

Those four controls sit in the actions cell on every posting row, and on a Today card's
documents line. Under `serve` they are the whole feature; there is nothing to configure
for the static dashboard, which renders no controls at all.

## Why the server writes the file

A web page cannot aim a browser download. `↓` and `✉` used to be `<a download>` links,
so the PDF landed wherever the browser had been told to put things and nothing here had
a say in it; `tex` and `tex✉` put the LaTeX on the clipboard, which is not a place you
can open later. The File System Access API would let a page ask — and needs a secure
context, which `serve` over plain http on a tailnet address is not.

`serve` runs on your machine as you. So the honest form of "save this where I said" is
for the server to write the file and tell you the path, which is what `POST /api/download`
does. It is a POST and not a GET because it writes to your filesystem, and a link that
did that on navigation is one a prefetch could fire.

Clicking a control disables it, swaps the label to `saved`, and puts the full path in the
button's `title`. It does not reload: a reload would discard the filter you typed and
where you had scrolled to, which on a table thousands of rows long is the whole morning.
The path goes in the title rather than the label because a folder name is not the width
of a glyph.

## Setting a destination

**Settings → Where saved documents go.** Four fields, each with its own Save. A shared
Save would let a typo in the third box discard three good paths, and these are four
independent decisions that happen to be rendered together.

An empty field means "not set" — the placeholder shows what it falls back to, and the
line under it shows where that expands to. A fallback typed into the box would come back
on the next save as a decision you never made.

```yaml
# downloads.yaml
resume:     '~/Downloads'
letter:     '~/work/applications/letters'
resume_tex: '~/src/resume/out'
letter_tex: '~/src/resume/out'
```

**Precedence is the file, then the environment, then `~/Downloads`.**

| Layer | What it is for |
|---|---|
| `downloads.yaml` | What you typed on a page you opened. Wins, because a setting the page cannot change is one the page should not be showing you. |
| `$JOBTRACKER_DOWNLOAD_RESUME`, `_LETTER`, `_RESUME_TEX`, `_LETTER_TEX` | A deployment where `~/Downloads` is meaningless — a container, a service account — getting a default worth having without four clicks. |
| `~/Downloads` | The built-in. |

A path is stored **as you wrote it**, `~` and all, and expanded when a file is saved.
Expanding before the write would bake one machine's home directory into a curated file,
and the expansion is a question about the machine reading it rather than about the
decision you made.

`downloads.yaml` is gitignored — four absolute paths on one machine is personal config
like `plugins.yaml`, not portable curation like `keywords.yaml`. Absent is a normal
state. `--downloads` points `serve` at a different one, the way `--plugins` does.

## What is checked, and what deliberately is not

**The destination gets almost no checking, and that is the feature.** It is a directory
you typed on a page you opened; `downloads.resolve_dir` asks only that it is absolute
after `~` expansion and does not name an existing file. A relative path is refused
because it would mean "wherever `serve` happened to be started from", which is a
different folder depending on how you launched it — the one property a destination must
not have.

**The filename is checked, because nothing types it.** `resume.tailored_stem` and
`letter.letter_stem` run the company and the job id through `resumes.stored_name`, which
slugs to `[a-z0-9_]`. That is what makes a name safe to join onto a directory somebody
typed: nothing from a posting can contribute a separator or a `..`.

**A missing directory is created.** A destination you have named and not used yet is the
ordinary case, and refusing over it would be this feature failing at the only moment it
is asked to do anything.

**An existing file of the same name is replaced.** Deliberate: the name is minted from
the company and the job id, so the only thing it can land on is an earlier copy of the
same document for the same posting. A browser's `resume (3).pdf` habit is exactly what
makes you attach the wrong one.

**A `downloads.yaml` that will not parse refuses the save.** It does not fall back to
`~/Downloads` — putting a document somewhere other than where you said is the one
failure this feature has. `/settings` still renders, because it is the page you would
open to fix it, and it offers no one-click fix: the writer splices into the text it was
handed, so a write over something nothing can read would take the rest of the file with
it. Fix it in your editor.

## What it does not do

**Saving is not accepting.** A copy in a folder of yours is not this posting's resume.
`tailor build --attach` is still the only thing that writes a tailored PDF into
`posting_resumes`, and there is no attach control anywhere near these four.

**It does not compile anything.** `↓` and `✉` are save buttons only once the PDF exists;
before that the same glyph is the build button, and `/api/tailor-build` is what runs.
`tex` and `tex✉` need no TeX engine at all — the source is derived on request from
`_tailored_source` / `_letter_source`, the same functions the builds compile from, which
is what lets them answer on a machine without tectonic.

**It writes nothing you did not click.** No scheduled run touches `downloads.yaml`, and
nothing saves a document on its own.

## The routes

| Route | |
|---|---|
| `POST /api/download` | `{company, ats_job_id, kind}` → `{ok, kind, path, name}`. Writes the document and names where it went. |
| `POST /api/download-dir` | `{kind, path}` → `{ok, kind, path, expanded}`. Sets one destination. A refused path writes nothing. |
| `GET /api/tailored-tex` | Still the pure read of the resume source, needing no engine. Nothing in the UI calls it. |
| `GET /api/coverletter-tex` | The same, for the letter. |
| `GET /api/tailored`, `GET /api/coverletter` | Still hand over the built PDF as an HTTP attachment, for `curl`. |
