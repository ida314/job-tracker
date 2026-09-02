# Plan: `tailor` — resume suggestions per posting

**Status: NOT STARTED (written 2026-09-02).**
**Depends on:** the task queue (`docs/tasks.md`), cached descriptions (`cmd_check`),
the per-posting resume override (`prefill_plans.resume_key`).

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
