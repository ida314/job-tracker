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
