# Adding a company

`companies.yaml` is the curated target list: which employers we watch, which board each
one publishes, and how it may be checked. This is the guide to putting something new in
it — from the terminal with `add-company`, or from the `/companies` page under `serve`.

Both go through the same writer and the same validator. That is deliberate: two
implementations of "append a curated entry" is how the button and the terminal end up
disagreeing about the file they both own.

---

## The two doors

```
jobtracker add-company --name OpenRouter --ats ashby --slug openrouter \
    --tier 2 --category ai-infra --check-method api \
    --careers-page https://openrouter.ai/careers
```

or `jobtracker serve` → **Companies** → fill the form → **Verify and add**.

The page does one thing the command does not: it fetches the board and checks it before
writing. The command's guarantee stops at *"this entry is coherent and the name is not
already taken"*.

---

## What gets validated

`curation.validate_new` is pure, shared by both doors, and deliberately **stricter than
`config.load_companies`**. The loader has to keep loading whatever is already on disk; a
*new* entry gets the strict pass, because every rule below catches a failure that is
otherwise silent.

| Rule | Why it is not just fussiness |
|---|---|
| `name` is required, unique, and unique case-insensitively | The name is the key postings, verdicts and applications are all stored under. Two spellings split one company's history in half. |
| `name` has no line break | The writer finds entries by their `- name:` line. |
| `ats` is one of the eight the file uses | A typo'd ats has no adapter, so the board is skipped behind a single log line — which reads exactly like a board with nothing open. |
| `check_method: api` needs an ats that *has* an adapter | Same failure, louder: `workday` and `bespoke` have no keyless JSON board and never will. |
| `check_method: api` needs a slug | `fetch_company` reports `empty slug` and the board is never checked. |
| An `api` slug has no space, `/` or `:` | `greenhouse/stripe` in the slug field means `ats: greenhouse, slug: stripe`. On a `manual` entry the slug is documentation, not an identifier — Red Hat's is a Workday tenant triple — so the rule does not apply there. |
| `(ats, slug)` is not already tracked | Two names on one board store every req twice and close it twice. |
| `careers_page` / `board_url` are `http(s)` | A `javascript:` value would land in an href on the dashboard. |
| `tier` is 1–7 | The bands are three; the numbers are seven. |
| `notes` is under 4000 characters | The server's JSON body cap is 64 KB, and an over-cap body reads as `{}` — which would surface as "name is required", a correct-looking error for the wrong reason. |

A validation failure has **no escape hatch**. It is about whether the entry is coherent;
only *verification* — whether the world agrees — gets one.

`test_every_entry_in_the_real_companies_yaml_passes_validation` keeps these rules honest.
A rule stricter than the file it validates is a rule that gets deleted the first time it
fires; two were already wrong when they were written.

---

## What gets verified

Only on `/companies`, only for `check_method: api`, and only when you did not press
*Add without verifying*. `manual` entries are **never fetched** — that rule predates this
page.

The board is fetched through the real `Source` adapter and handed to
`repair.judge_board`, the same ordered rule `repair` applies to a candidate slug:

- **`unreachable`** — a failure is not evidence.
- **`zero_jobs`** — the `greenhouse/hubspot` rule. A real-but-dead board answers 200 with
  an empty array forever, and a brand-new entry has no history to tell that from
  "emptied on Tuesday".
- **`no_identity`** — the identity endpoint gave nothing. Checked *before* the next one,
  because `identity_matches` returns True when either side is empty.
- **`wrong_company`** — the `ashby/cedar` rule. `ashby/cedar` returns live postings
  belonging to an unrelated mortgage company.

### `identity` vs `reachable`

**Only Greenhouse publishes a board name**, from a different endpoint than the one the
slug was used on. Agreement there is real evidence, and it is labelled `identity`.

Ashby and Lever derive identity by reading the org slug back out of the first job URL —
which for a slug you just typed simply restates what you typed. `repair` accepts those on
**provenance**: the link came off the company's own careers page, which is a genuine if
weaker claim. **A slug typed into a form has no provenance at all** — no page served it —
so the label here is `reachable`, and it means exactly one thing:

> the board answered and it is not empty.

The page says so every time, and prints sample job titles, because reading a few titles
is the only check left (DESIGN.md §7.2). This is why `judge_board` takes the weak label as
a parameter rather than hard-coding it: the two callers are making different claims.

### `expected_board_name`

On a verified save it is seeded from **the name the ATS returned**, not the one you typed.
The fuzzy comparison then happens exactly once, under your eyes; every nightly check
afterwards compares against the exact string the board gave us.

On an unverified save — `manual`, or *Add without verifying* — it is written as `null`.
Writing the typed name would make the first nightly run either alert on a name nobody
checked, or quietly "verify" it, since `identity_matches` returns True on an empty side.
`null` is the honest state, and `repair` picks it up from there.

---

## What gets written

The new block is rendered on its own and spliced into the text. The other hundred entries
are never parsed and never re-dumped, so **not one of their lines changes** — a PyYAML
round trip re-folds `notes:` prose to its own width, and a one-line change then produces a
diff touching ten companies nobody edited.

Placement is **before the first entry that sorts after you**: the first higher tier, or
the first untiered entry, whichever comes first. Not "after the last entry with my tier,
else end of file" — that has no last entry for a tier the file does not use yet, and a
tier-4 company would fall past everything and land under the aggregator feeds.

The write itself is `safewrite.write_text`: candidate → parse it with the real loader →
`.bak` → atomic swap. A file that will not load is never left on disk, and the page shows
you the unified diff computed from the same string that was written.

---

## Why a click may write this file

The invariant is **no scheduled run writes curated data** (DESIGN.md §2.3). `serve` is a
foreground process you started and the write happens on a click you made, so that holds.

The repair proposal on the dashboard still has no apply button, and the difference is what
the click does. Adding a company **appends an entry that did not exist**, from values you
typed, and shows the diff afterwards. Applying a repair **rewrites a hand-verified slug**
on the machine's say-so — that is the case where the diff has to come *before* the write,
which is what `repair --write` is for.

---

## The socket

`/api/company` is the only endpoint on this server that opens one. It cannot go on a
daemon thread the way `apply-to` does, because the answer decides whether the write
happens and nothing on a thread can answer the click that started it.

So it is bounded instead: `Fetcher(max_workers=1, timeout=8, max_retries=1)` — one attempt
against the nightly Fetcher's three, and at most two requests. Per-host pacing is left at
its default; politeness to the ATS is not something a waiting page gets to skip. A second
verification while one is in flight is **refused, not queued** — queuing would stack a
second freeze behind the first on a server that handles one request at a time.

There is a test pinning that bound. If you widen it, widen it there first.
