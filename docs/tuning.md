# Tuning the matcher

`criteria.yaml` is easy to edit and hard to edit *safely*. A token added to stop one
bad match silently changes the verdict on thousands of postings — including ones you
already looked at and approved. This is the machinery that makes a rule change
measurable instead of a guess.

The loop:

```
judge a posting  →  a phrase gets suggested  →  eval says what it would cost
                 →  apply  →  rematch
```

## The corpus

Every judgment you record goes into a `decisions` table with the posting's title:

```sh
jobtracker decide Stripe 7966029 reject --note "operations, not engineering"
jobtracker decide Palantir 4abf26b4-... match
```

The title is stored **in** that table rather than joined from `postings`. Reqs close
and get pruned; the judgment "a title shaped like this is not what I want" stays true
forever. Joining would shrink the corpus exactly when the evidence matters most.

`--pin` additionally sets a per-posting override, so no later rule change can quietly
re-open that specific posting. The tuning UI pins automatically — a call you made by
hand should not be undone by a rule you wrote later.

## `jobtracker eval`

Replays the current criteria against every recorded judgment.

```
$ jobtracker eval
10 decisions · agree 3 · regressions 7 · unresolved 0

REGRESSIONS (7) — rules contradict your judgment:
  Palantir   Forward Deployed Software Engineer, New Grad     you:reject  rules:match
             └─ fired: level:new grad+eng:generic
  Stripe     Seller Systems Operations Associate (Night Shift) you:reject rules:match
             └─ fired: level:associate+role:systems
```

Exits **1** when a regression exists, so it composes into a gate rather than being
something you have to remember to read.

### Regression vs unresolved

These are different and the distinction is load-bearing.

A **regression** is the rules *actively contradicting* you: `reject` where you said
`match`, or `match` where you said `reject`. That is a real defect.

**Unresolved** means the rules said `uncertain` where you made a call. That is not a
failure — `uncertain` is the honest answer for a title with no level token, and
resolving it is the description-reading pass's job (see `docs/llm.md`). If `eval`
counted those against you, it would push you toward writing rules that guess level
from titles, which is exactly the over-fitting this whole mechanism exists to prevent.
Only regressions block.

## When there is no corpus: `eval --replay-all`

`eval` needs recorded judgments. With `decisions` empty it has nothing to replay, and a
criteria edit would ship with no evidence at all. `--replay-all` is the fallback: it
replays `match()` over **every stored posting** — open and closed, every origin — under
the old criteria and the new, and diffs every verdict.

```sh
git show HEAD:criteria.yaml > /tmp/criteria.old.yaml
$EDITOR criteria.yaml
jobtracker eval --replay-all --against /tmp/criteria.old.yaml
```

```
replayed 9150 postings, open and closed (0 override(s), 0 held)
  old: /tmp/criteria.old.yaml
  new: criteria.yaml
  match       33 → 14  (-19)
  reject      7513 → 7512  (-1)
  uncertain   1604 → 1624  (+20)

transitions:
  match → uncertain         19  BLOCKS
  match → reject             0
  uncertain → match          0
  ...

match → uncertain:
  Notion             Software Engineer, New Grad
                     └─ level:new grad+eng:generic  →  no_level_token_in_title
```

The delta table is `rematch`'s; the six transition lines always print, so "nothing
moved" reads as six zeros rather than as silence. Up to `--samples N` (default 20)
examples follow per transition, each with the reason the old rules gave and the reason
the new ones give.

- **`--against` is required.** The old file is named, not fetched: running `git` here
  would make this the second module that shells out, and `assemble.py` is the only one
  (a test in `tests/test_resume.py` keeps it that way).
- **It gates only on a lost match.** Exit **1** when a posting goes `match → reject` or
  `match → uncertain` and that transition is not declared intended; `uncertain → match`
  and `uncertain → reject` are counted and printed and never block, for the reason in
  *Regression vs unresolved* above. Exit **2** when it could not run — either criteria
  file, the `--intended` file or the database did not load.
- **`--intended PATH`** is a YAML mapping of the transitions this change means to make,
  each with its reason, which is printed beside the count:

  ```yaml
  "match->uncertain": "bare 'grad' moves to the level task"
  ```

- **Overrides are applied to both sides.** A pinned posting cannot move on the pages, so
  it is never a transition; when the rules under it *did* move, it is reported as
  **held**, with its own samples. Those are the rows a criteria edit may need
  `store.clear_override` for — only where a model guess contradicts the new rules.
- **It is read-only.** The database is opened through a `mode=ro` URI, never through
  `store.connect()`, which migrates on open, so it is safe against a copy of production
  or a file you cannot write. It writes nothing anywhere and needs no `decisions`.
  A WAL database in a directory you cannot write either will not open: SQLite needs to
  create the `-shm` file beside it. Copy it somewhere writable.

## Suggestions

`eval` also proposes phrases that appear in several postings you rejected and in **no**
posting you kept:

```
Suggested rules — phrases in ≥3 rejects, never in an accept:
  'forward deployed'    5 rejects   e.g. 'Forward Deployed Software Engineer, New Grad'
```

Pure string counting; no model involved. Three properties worth knowing:

- **The zero-in-accepted test does the real work.** `engineer` and `software` appear in
  titles you kept, so they can never be suggested. No hand-maintained blocklist needed.
- **Bigrams do not cross punctuation.** "Software Engineer, New Grad **- Commercial**"
  would otherwise yield `grad commercial`, a phrase that appears nowhere in the title,
  and it would get confidently proposed as a rule.
- **The list is self-terminating.** Suggestions are computed only over rejects the
  rules *do not already handle*, so it describes the remaining gap and empties as you
  close it. Computed over all rejects, accepting `forward deployed` would merely
  promote the overlapping bigram `deployed software`, forever.

## Applying a change

```sh
$EDITOR criteria.yaml          # add the phrase
jobtracker eval                # what does it cost? exits 1 on a regression
jobtracker rematch             # re-apply to every open posting
```

`rematch` prints the delta rather than just totals, because after a rule change the
question is always "what moved":

```
rematched 8637 open postings (2 override(s) applied)
  match       33 → 26  (-7)
  reject      7522 → 7614  (+92)
  uncertain   1595 → 1510  (-85)
```

Overrides are re-applied on every rematch, so pinned postings keep your verdict.

## Auditing the model's pins: `jobtracker audit-pins`

The `level` task pins every verdict it reaches, because `check` would otherwise
re-derive the title's verdict overnight and erase it. Pins written before readings were
grounded have nothing behind them, and the worst of them are invisible: an `entry` pin
on a job board row whose "description" is employer, location and a date, and a
`not_entry` reject, which no page renders. `audit-pins` decides which stay.

| Pin | Kept if | Otherwise |
|---|---|---|
| `entry`, `description_source = 'feed'` | the rules, from the title alone, reach the pinned verdict | cleared |
| `not_entry`, any source | its quote (after `llm:not_entry:`) occurs in the title or description | cleared |
| `entry`, `ats`/`prose`, with a quote | its quote occurs in the title or description | cleared |
| `entry`, `ats`/`prose`, no quote | always — real prose was read, before quotes were stored | — |

"Occurs" is `level.grounded`, the check the reading passed when it was made:
whitespace-normalized and casefolded. Two cases the table does not name are **kept**,
because the side to err on is the one that removes nothing on a guess: an `entry` pin
with no quote and no recorded source, and a reason in a shape the audit does not
recognise.

```sh
jobtracker audit-pins            # dry: counts per bucket and up to 20 titles each
jobtracker audit-pins --write    # clear, then re-derive each cleared row with match()
```

- **It can only see `llm` pins.** `decided_by = 'llm'` is in the SQL, so a human
  ruling is never in the list it chooses from. It is the only bulk remover of model
  pins.
- **A cleared pin re-derives from the rules, not the model.** `match()` writes a
  `decided_by = 'rules'` verdict for each cleared row and nothing else; the run logs
  how many moved.
- **It leaves the outer loop alone.** `decisions`, `applications`, `rankings` and
  `level_readings` are untouched; a cleared posting you applied to is still an
  application.
- **By hand only.** Nothing in `check`, `work` or `prepare` calls it.

## Doing it in a browser

```sh
jobtracker serve            # http://127.0.0.1:8765
```

- `/` is the dashboard, regenerated live.
- `/tuning` shows corpus health, suggestions, and every open match **with the rule that
  produced it** — so a bad match points straight at the rule responsible.

"not for me" records a decision and pins it. "add to exclude_titles" writes the rule
and rematches in one step.

Rule writes go to a candidate file which is parsed *before* it replaces the real one;
an invalid edit is refused rather than left on disk for the next run to hit. A `.bak`
is kept regardless.

`serve` binds `127.0.0.1` by default. It has no authentication and can edit your
criteria file — `--host` exists, but you have to mean it.

## A worked example

The Stripe leak, start to finish. `"Seller Systems Operations Associate (Night Shift)"`
was matching because `systems` in `role_type_include` satisfied the engineering gate
for a pure operations title — the `Finance Associate` bug from DESIGN.md §10 through a
different door.

```sh
jobtracker decide Stripe 7966029 reject --note "operations, not engineering"
jobtracker decide Twilio 8048659 reject --note "support-shaped"
# ...and the five Palantir Forward Deployed roles

jobtracker eval --min-count 2
#  → 7 regressions, each naming the rule that fired
#  → suggests 'forward deployed' (5 rejects)

# add forward deployed / operations associate / application engineer to exclude_titles
jobtracker eval        # 10 decisions · agree 10 · regressions 0   (exit 0)
jobtracker rematch     # match 33 → 26
```

The point is the middle step. Without it, adding three tokens to a YAML file and
watching a number go down is indistinguishable from breaking something you had already
decided was correct.
