# Plan: `tailor` — resume suggestions per posting

**Status: IMPLEMENTED 2026-09-01.** Shipped as the `tailor` task
(`jobtracker/tasks/tailor.py`) plus `jobtracker/resume/`, gated by a plugin switch and
read-only on both surfaces. **Guide:** `docs/tailor.md`. **Role 5 in DESIGN.md §8.**
**Depends on:** the task queue (`docs/tasks.md`), cached descriptions (`cmd_check`),
the per-posting resume override (`prefill_plans.resume_key`).

This plan was written on 2026-09-02 against a local `main` that was one commit behind;
the work had already landed upstream the day before. The prompt below is left exactly as
written so the corrections have something to point at.

## What the plan got wrong

Seven things, all confirmed against the shipped code rather than the commit messages.

1. **`unit_key` was over-specified.** The plan said posting id + a hash of the resume
   text. It is `ctx.resume_hash` alone: the posting is already in `TaskUnit.ident`, so
   folding it in again only duplicates it inside the attempts key. The property the plan
   was reaching for survives — change the resume and every unit is new with a clean retry
   count, exactly as editing `profile.yaml`'s prose re-asks `judge`.

2. **The PDF/DOCX dependency decision was dissolved, not made.** The plan asked for the
   choice to be stated explicitly rather than slipped in. The answer was to move the
   resume's source of truth to **LaTeX**, which is text and needs no parser at all. The
   dependency that did arrive is at the other end: `tectonic`, to compile. It is
   deliberately optional — `TailorTask.unavailable_reason` does **not** report a missing
   TeX toolchain, because suggestions are text and only assembly needs an engine.
   Reporting it would withhold the whole feature for want of its last step.

3. **"Never a file / no code path may write bytes to a resume" was too absolute to
   ship.** `jobtracker tailor build` writes a PDF. The narrower rule is the real one and
   it holds: it never touches the resume *source*, it applies edits to an in-memory copy,
   it writes a **new** file under `TAILORED_DIR`, and attaching one to an application is
   a separate act (`--attach`).

4. **The plan had no security argument, and the writing end needed one.** Its grounding
   rule (`evidence` quoted verbatim) protects truthfulness; it says nothing about the
   compiler. A model-composed suggestion goes into a document this repo then *runs* —
   TeX has filesystem access, and `\write18` under shell-escape is a shell command. So
   `latex.sanitize()` is a control-sequence **allowlist** (a blocklist is a guess, and
   `\csname` composes new names out of characters), and `assemble.py` compiles with
   `--untrusted`, no shell, a scratch directory, and a timeout. Belt and braces, because
   either alone would only *probably* do.

5. **"Proposal-only" needed a third state.** `dismissed` is reachable and the row is
   **kept, never deleted** — the `mail_proposals` rule. Deleting is what would let the
   next run propose the same edits again; they come back on their own when the resume
   changes, because that is a different question.

6. **It is not a "fifth bounded model role" in the way the plan assumed.** It is a plain
   task module registered by one import line, as planned — but gated by a plugin switch
   (`jobtracker/plugins/roles.py`) and shipping **off** by default. `level`, `judge` and
   `inbox` default on, so a box that never opens `plugins.yaml` runs exactly the queue it
   ran yesterday. The plan had it simply join the queue and start working.

7. **The surfaces carry no control at all.** The plan said "surface on `/apply` and the
   Today card, read-only in the static dashboard" — which reads as though the live pages
   would get a button. Neither does, in either mode, with tests asserting the absence:
   accepting means compiling, so a button would put a model-authored PDF one click from a
   real application with the diff unread. It would also widen what `.pick [data-act]` and
   `.lf` select, which the disposition handler and the parity tests depend on. Priority
   50 and the starvation argument were right.

## The plan as written (2026-09-02, unedited)


Nothing in the repo reads a resume today. `resumes.py` treats it as opaque bytes —
suffix and magic-byte validation, a collision-safe name, and "which file does this
posting attach". There is no PDF/DOCX text extraction anywhere in the package, and none
of the four bounded model roles reads a description for this purpose.

## The prompt

```
Add a fifth bounded model role: `tailor` — resume suggestions per posting, proposal-only.

Follow docs/tasks.md: one pure module in jobtracker/tasks/, one import line, runner.py owns
every socket, transaction and clock. Priority 50 (outside the level→judge dependency chain,
last on the same starvation argument as `inbox`).

- Input: postings.description (already cached by `check`) + the resume's text.
  There is no PDF/DOCX parser in this repo — adding one is a dependency decision, so state
  the choice explicitly rather than slipping it in.
- unit_key = posting id + a hash of the resume text. Change the resume and every unit is new.
- pending(): scored matches with a non-NULL description and a resume on file. Never return
  work the task cannot do.
- Output: schema-constrained. A list of {section, current_line, suggestion, evidence} where
  `evidence` is a phrase quoted VERBATIM from the description — the `inbox` grounding rule,
  and the only thing keeping a fabricated requirement off a real application.
- Writes to its own table (`resume_suggestions`), never verdicts (rewritten nightly) and
  never a file. It PROPOSES; the user edits and uploads through the existing per-posting
  override (prefill_plans.resume_key). No code path may write bytes to a resume.
- Every failure path leaves the posting with no suggestion. Nothing raises for a down model.
- Surface on /apply and the Today card, read-only in the static dashboard. Handlers live in
  the file that renders the button.
- Update CLAUDE.md and DESIGN.md §8 in the same commit; docs/tailor.md for the guide.
```

## Why proposal-only is the whole design

Generating prose that goes out under the user's name is what `prefill`'s model pass was
removed for on 2026-08-25 (DESIGN.md §8.1). The surviving rule has no exception: a value
reaches an application because a canonical name matched or because the user attached that
wording on purpose. A suggester that rewrites and attaches breaks it; one that proposes a
diff the user accepts by hand is `inbox`'s shape, one loop out.
