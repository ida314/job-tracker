# Uncertain queue quality, and preferences that are not hardcoded

Spec from an interview on 2026-11-05. Four phases, shipped in order. A separate spec covers
auto apply (see *Handoff* at the end). This one is to be finished first.

> **To implement, use the chunks in `uncertain-queue/00-index.md`.** That is 25 worktree-sized
> tasks with dependencies. Where a chunk and this document differ, the chunk is newer:
> `description_source` and `Plugin.coverage()` land first (chunk 02), so §4.2's stand-in is
> not needed, and YC's listing paths are *already* a plugin setting (`paths`), so the second
> half of §7.4 needs no work.

---

## 1. The problem, measured

All numbers come from the live gx10 `serve` (`v0.4.0 · 86296248ad39`). They were parsed from
the read-only `/`, `/jobboards` and `/tuning` pages on 2026-11-05. The laptop's
`data/state.db` stops at 2026-07-23 and was not used for any number here.

| | Company pages | Job boards (Simplify) | Total |
|---|---:|---:|---:|
| Open uncertain | 1,773 | 1,403 | **3,176** |
| … no engineering signal in title | 1,708 (96%) | 519 (37%) | 2,227 |
| … engineering signal | 65 | 884 | 949 |
| … engineering signal, first seen ≤ 14 days, not obviously hardware | 7 | 127 | ~134 |
| Open matches | 162 | 766 | 928 |

- **One board is a third of the company-page queue.** The `Target Tech` slug
  `target/wd5/targetcareers` is all of Target's careers, so it includes Starbucks Barista,
  stocking and store security. It has 511 uncertain rows. Capital One has 170, Stripe 101
  and Palantir 90.
- **The queue is not used.** Only 2 of 34 applications are uncertain rows. `decisions` is
  empty (`/tuning`: "No judgments recorded yet"), so `eval` has no corpus.
- **The "Why uncertain" column says nothing.** All 3,176 rows read
  `no_level_token_in_title`.
- **Simplify rows have no prose.** `plugins/simplify.py::_description` builds the text
  from Category, Locations, Sponsorship and Degrees. `level` still reads these rows:
  **212 open Simplify matches are `override:llm:entry+…` pins** that rest on that
  metadata.
  - The model's evidence is never checked against the text.
  - An `entry` pin stores no quote at all, because its reason is `llm:entry+eng:generic`.
  - `not_entry` pins become rejects in the same way, and no surface shows them.
- **`level` does not record `unclear`.** An `unclear` answer counts as `no_answer`, so
  every unclear unit uses up three model calls and is then set aside as blocked. From then
  on it cannot be told apart from a transport failure.
- **There are title-rule gaps.** Each was found by replaying titles:
  - `grad` does not fire inside "Graduate". The boundary rule causes this, the same way
    `internship` misses "Internships".
  - "Software Engineer 1" (about 150 engineering rows) has no level token. Only the Roman
    `Software Engineer I` is listed.
  - "Full-stack Engineer 4" (36 rows) is not excluded the way `ii` and `iii` are.
  - `sr.` needs its dot, so "Sr Accounting Specialist" leaks (46 rows).
  - Electrical, firmware and design-verification families make up about 194 Simplify rows.
- **Preferences are hardcoded.**
  - `criteria.yaml` and `profile.yaml` are tracked and baked into the image, which means
    your backend, NYC and new-grad choices ship as everyone's defaults.
  - The judge schema field is named `backend_fit`.
  - The location key is `locations_nyc`.
  - The `level` prompt says "software job" and assumes a new-grad target.
  - The YC listing paths default to backend (they are already a configurable `paths` setting).

## 2. Goals and non-goals

**Goals**

1. Uncertain becomes **a short list of possible misses** that you can skim each week.
   Everything else stays stored and folded, never deleted.
2. A row can be **ruled on in one click**, and every ruling grows the `eval` corpus.
3. Model verdicts are **grounded** in text the model was actually shown.
4. **Nothing personal is hardcoded.** That covers role families, experience level, the
   field gate (engineering or otherwise) and location preference. Your own choices move
   into a gitignored overlay.

**Non-goals**

- Auto apply. It has its own spec.
- Splitting `companies.yaml` into shared and personal parts, or shipping source presets
  for other fields. §8.3 documents this limitation.
- Changing the standing rule "do not reject no-signal titles". This spec keeps it. Folding
  is a display decision, not a verdict.
- Fetch-side filtering (`appliedFacets` stays empty).

## 3. Decisions taken in the interview

| # | Question | Decision |
|---|---|---|
| D1 | What is uncertain for? | A short list of possible misses, skimmed weekly. |
| D2 | No-signal rows | Folded in the UI. Their verdicts stay `uncertain`. |
| D3 | Simplify rows with an engineering title and no level token | The feed counts as weak evidence. They stay uncertain, labelled and sorted first, and nothing is auto-promoted. |
| D4 | Mixed boards (Target Tech) | A per-company curated noise setting. |
| D5 | What the short list shows by default | Rows first seen within the window (14 days) first, with older rows folded below. |
| D6 | Ruling controls | "Worth a look" writes a match decision plus a pin. "Not for me" writes a reject decision plus a pin. |
| D7 | Title-rule fixes | `graduate`, numbered levels, bare `sr`, and hardware families. **Hardware goes through preferences rather than being hardcoded.** |
| D8 | Model pins | Ground the evidence, stop sending prose-less feed rows to `level`, and audit existing pins. |
| D9 | Auditing `entry` pins that have no stored quote | Keep a pin only if the title alone now passes the rules. Otherwise send the row back to uncertain, at the top of the list. |
| D10 | Prose-less rows (`judge`, `tailor`, `coverletter`) | Fetch real prose through the known ATS adapters. Label everything else "thin" and cap its score. |
| D11 | Where the short list lives | A new `/review` page under `serve`. The existing Uncertain tables stay as the full record. |
| D12 | What becomes configurable | Role families, experience level, the field gate (including non-engineering careers) and location. |
| D13 | What a family stance does | Three stances: `want`, `neutral`, `avoid`. `avoid` means REJECT. |
| D14 | Where preferences live | Mechanics and the catalogue stay in tracked `criteria.yaml`. Your stances go in a gitignored `preferences.yaml`, with a tracked `preferences.example.yaml`. |
| D15 | How a stance is changed | A `/settings` card that shows a dry-run diff before Apply. |
| D16 | How level works | Absolute bands. The model reports a posting's band, the reading is stored, and Python compares it with your target. |
| D17 | Field gate | A flat `field_terms` list with shipped presets. An empty list means no gate, logged at WARNING. |
| D18 | Sources | Feed plugins declare what they cover. YC's listing paths are already a plugin setting (`paths`). `companies.yaml` is out of scope. |
| D19 | Phasing | (1) grounding and the pin audit → (2) the queue → (3) preferences, families and location → (4) bands and `field_terms`. |
| D20 | `preferences.yaml` is missing | Refuse to run if preferences existed before. A fresh install runs neutral with a warning. |

---

## 4. Phase 1: ground the model, stop reading metadata, audit the pins

Ship this first. It is small, and it stops ungrounded verdicts from piling up while the rest
is built.

### 4.1 Grounding in `level`

- `parse_verdict` gets the text the model was shown (title plus description). A `usable`
  reading must have `evidence` that occurs in that text, whitespace-normalized and
  case-insensitive. This is the same normalization as `tailor`'s grounding check.
- Ungrounded readings are treated as no answer, the same as malformed JSON. Every failure
  path still leaves the posting UNCERTAIN.
- The `entry` reason stores the evidence as well:
  `llm:entry+eng:generic:<evidence[:80]>`, matching `not_entry`. A pin you cannot audit
  later is the problem this phase exists to fix.

### 4.2 Prose-less feed rows leave the `level` queue

- `LevelTask.pending()` skips a posting when its description came from a feed's metadata
  and not from prose. That needs a durable marker (`postings.description_source`, see
  §5.6). Until §5.6 lands, phase 1 can use `origin IS NOT NULL` as a stand-in, but only
  for feeds that declare `prose: False` (Simplify does; YC's `minExperience` prose and
  Discord message bodies are prose).
- `pending()` must only return work the task can actually do. Asking the model about
  `Category: Software` is not such work.

### 4.3 The audit: `jobtracker audit-pins`

The command is dry by default, and `--write` applies it. It only ever touches overrides with
`decided_by = 'llm'`. A human ruling is never moved, and neither are `decisions`,
`applications` or `rankings`.

| Pin | Test | If it fails |
|---|---|---|
| `entry` on a prose-less feed row | Does `match()` on the title alone, under the **phase 1 criteria with the §5.5 tokens applied**, give MATCH? | `clear_override`. The row re-derives on the next rematch (usually back to uncertain) and sorts into the `feed:<band>` lane (§5.1). |
| `not_entry` on a prose-less feed row | Does the stored evidence occur in the title or description? | `clear_override` and re-derive. |
| Any `llm` pin on a row with real prose | Does the stored evidence occur in that prose? An `entry` pin with no stored evidence passes on its prose and is left alone. | `clear_override` and re-derive. |

- The output gives counts per row, then up to 20 sample titles in each bucket ("kept",
  "back to uncertain", "back to reject"), like `rematch`'s delta.
- The `--write` run logs the moved count at INFO, under the rule *a migration that changes
  what the pages show says how many in the run log*.
- A cleared row that has an application keeps it. `rank.is_available` already excludes it
  on the presence of a status.
- The audit runs once, by hand. It is not a nightly step.

**Ordering constraint:** the §5.5 token fixes (`graduate`, numbered levels) must be in
`criteria.yaml` *before* `audit-pins --write`. If they are not, "Graduate Software Engineer"
pins are cleared and then fall to uncertain for no reason. In practice, phase 1 ships the
grounding and the queue skip, phase 2's token commit follows, and then the audit is run.

### 4.4 Record `unclear`

- A new `level_readings` table holds
  `(company, ats_job_id, band, evidence, read_at, source_hash)`. `level` writes a row for
  every answer, including `unclear`. An `unclear` answer counts as an `ok` attempt rather
  than `no_answer`, so it stops using three calls and being set aside as blocked.
- `pending()` excludes rows that already have a reading with the same `source_hash` (a
  hash of the text the model was shown). A description upgrade (§5.6) changes the hash,
  and the row is asked again.
- In phase 1, `band` is limited to the existing three values (`entry`, `not_entry`,
  `unclear`). Phase 4 widens it to the bands without changing the table's shape.

---

## 5. Phase 2: the queue becomes a short list

### 5.1 One classification, used everywhere

`queue.lane(row, criteria, companies, plugins) -> Lane` is pure, like `match()`. It is the
**single derivation**, called by `/review`, by the dashboard folds and by every count. Two
copies would come to disagree about what is on the list.

The lanes are listed in sort order:

1. `feed:<band>`. The row came from a feed whose declared coverage (§7.4) is one of your
   target bands, and its title has a field signal. Label: *"listed on a new-grad feed"*.
   (In phase 2 the only declaration is Simplify = `entry` and the only target is `entry`.)
2. `read:unclear`. `level` read real prose and said it does not state a level. Label:
   *"read — the description does not say"*.
3. `signal`. The title has a field signal and the row has not been read yet: no
   description cached, or the read is still waiting in the queue. Label: *"not read yet"*.
4. `no_signal`. There is no field signal in the title. The row is **folded**.
5. `board_noise`. This is `no_signal` on a board marked noisy (§5.3). It is **folded
   inside the fold** and counted separately.

- This label replaces the "Why uncertain" column wherever that column appears.
- `verdicts.reason` is unchanged. The lane is derived when the page is rendered and never
  stored.
- The verdict of every row stays `uncertain`. Nothing in this phase moves a verdict except
  the token fixes in §5.5, which are checked against a diff.

### 5.2 The `/review` page

It is a `serve`-only page, listed in `_NAV` as **Review**.

- **It is a read across the partition, not a third home.** It lists `uncertain` rows from
  both origins in one flat list. Employer is shown as a `.cobadge` via `_employer_of` and
  the board as a `srctag`, the same as the Today drawer. Rows still render in exactly one
  of Company pages or Job boards. The partition test does not change, and a new test
  asserts that every `/review` row is also in exactly one of the two.
- **Default view:**
  - Lanes 1–3 come first, filtered to `first_seen` within `review_window_days`. That
    setting is 14 by default and lives in `preferences.yaml` from phase 3; in phase 2 it
    is a constant.
  - Rows sort by lane, then location rank, then `first_seen` descending. From phase 3,
    `want` families sort ahead of `neutral` ones within a lane.
  - Below that is one closed group, *"N older"*, holding lanes 1–3 outside the window.
  - Below that is a second closed group, *"N with no field signal"*. Inside it,
    `board_noise` is its own closed sub-group: *"511 from boards marked noisy: Target
    Tech"*.
- **The page renders server-side, and JS only toggles `[hidden]` and `tbody.closed`**,
  following the existing dashboard rules. Filtering owns `hidden` and collapsing owns
  `closed`.
- It **must not be a `table[data-filterable]`** that shares a filter bar with any other
  panel. It gets its own `[data-filter-scope]` with a search box only.
- A ruled row leaves the list on its own, because it is no longer `uncertain`. This list
  has no "seen" flag, which follows the banner rule.
- **Today** gets one line under the picks: *"N to review → /review"*. The count is lanes
  1–3 within the window, from `queue.lane`. Under `serve` it is a link. The static file
  shows the count with no link.

### 5.3 The per-company noise flag

- Add `board_scope: mixed` to `companies.yaml`, as an optional key after `category`,
  keeping field order. It means the board serves many non-field roles. The value is a
  curated finding, and like every other field it has its reason in `notes:`.
- It affects **only** `queue.lane` (it sends `no_signal` rows to `board_noise`). It never
  affects `match()` or health, and it has nothing to do with `SUSPECT_EMPTY`.
- `curation.validate_new` accepts it, `load_companies` loads any value with a WARNING on an
  unknown one, and `/companies` offers it as a checkbox.
- Initial value: Target Tech. Decide on Capital One once `/review` exists.

### 5.4 Ruling controls

- Rows on `/review` get two buttons: `button.rv-yes` (**Worth a look**) and `button.rv-no`
  (**Not for me**). Neither carries a `data-act`, which keeps meaning the three disposition
  buttons only.
- Both POST to the existing **`/api/decision`**, `decision: match|reject`, which already
  records the `decisions` row, pins `override:manual` and writes the verdict. There is no
  new write path.
- The handler is delegated and swaps the row in place, replacing it with a *"✓ match ·
  undo"* / *"✓ not for me · undo"* chip. It does not reload. Undo calls `clear_override`
  through a new `/api/decision/clear` and deletes that `decisions` row. That is the only
  removal, and it is scoped to the posting that was just ruled.
- The handler lives in the script of the page that renders the buttons, and a parity test
  asserts that the button exists *and* that a listener for it exists.
- **A match ruling makes the row eligible for picks the next time `rank` runs.** `work`
  already rescores, and `prepare` rescores too. The row gets no special treatment in
  ranking.
- The existing Uncertain tables do not get these buttons. The short list is where rulings
  happen, and the tables stay a record.

### 5.5 Title-rule fixes, gated by a verdict diff

`decisions` is empty, so every edit follows the CLAUDE.md rule for that case. Replay
`match()` over **all stored postings** under the old and new criteria, and diff every
verdict. A new script, `jobtracker eval --replay-all`, prints the delta and 20 samples per
moved bucket and exits 1 when any `match` becomes `reject` without being listed as
intended. Each fix is its own commit, with its diff summary in the commit body.

| Fix | Token(s) | List | Expected effect |
|---|---|---|---|
| Graduate | `graduate` | `level_include` | "Graduate Software Engineer" → MATCH. "Commercial Graduate Trainee" → REJECT through the existing non-engineering gate. |
| Level 1 | `engineer 1`, `developer 1`, `engineer i` | `level_include` | "Software Engineer 1", "Software Development Engineer 1" → MATCH. Check what else they catch: `engineer i` must not fire on "Engineer II" (the boundary rule handles this; test it). |
| Level 2+ | `engineer 2` … `engineer 5`, `researcher 4`, `researcher 5` (only those seen in the corpus) | `exclude_titles` | "Full-stack Engineer 4" → REJECT. |
| Bare sr | `sr` | `exclude_titles` | "Sr Accounting Specialist" → REJECT. Check it does not fire inside `SRE` or `sre`, and that it does not fire on employer names in `Employer — Role` titles. |

- **Hardware is deliberately missing from this table.** It becomes the `hardware` family in
  phase 3, and *your* `avoid` stance reproduces today's behaviour. The existing hardware
  tokens stay where they are until then.
- In phase 4 the numbered tokens move into band definitions (`entry: [engineer 1, …]`,
  `mid: [engineer 2, …]`), so a mid-level user's target band admits them.

### 5.6 Prose-less rows: fetch where possible, label the rest

1. **Add the column `postings.description_source`**: `ats`, `feed`, or NULL for legacy
   rows. It goes in `_apply_column_migrations`, never in `_SCHEMA`.
   - Backfill: board rows are `ats`. Plugin rows are `feed`, except Discord and YC, whose
     descriptions are message or prose text, so they are `ats`-equivalent (call that
     value `prose`).
   - The final enum is `ats | prose | feed`.
2. **One permitted upgrade, `feed` → `ats`.** This is the only exception to the
   write-once rule, in the same way that Greenhouse's `first_published` upgrades
   `posted_on`.
   - Each night, `check` adds feed rows whose verdict is `match` or `uncertain` (lanes
     1–3), and which can be resolved to a known ATS, to a separate list named
     `wanted_upgrade`. They are **not** added to `wanted`, because its no-detail branch
     would erase the text.
   - `wanted_upgrade` is under its own budget (`--max-upgrades`, default 100), and counts
     against the same per-host limiter.
3. **Resolve the ATS from the URL, never from `dedupe_key`.** The key lowercases the slug,
   and Lever slugs are case-sensitive. Use the same path extractor `dedupe.py` uses, on
   `postings.url`, keeping its case.
   - Supported: Greenhouse (job detail endpoint), Lever (single posting endpoint) and
     Ashby (posting-api board fetch, cached per slug per run).
   - Anything else stays `feed`.
   - The slug comes from a link the feed published, so it counts as *provenance*, not
     verified identity, and is labelled that way.
4. **After an upgrade, stale downstream work is redone.**
   - `rankings` stores `description_hash`. `matches_needing_judgment` also selects rows
     whose current description hash differs.
   - `level_readings.source_hash` already does the same for `level` (§4.4).
   - `tailor` and `coverletter` units add the description hash to their unit key, beside
     `resume_hash` and `keywords_hash`.
5. **Label and cap.** A row whose source is still `feed` renders a `thin` chip on picks,
   matches and `/review`. `rank` caps its `fit` and `growth` terms at `moderate`, through
   the weights in Python, never by telling the model.
   - `prepare` names thin picks and does not count them against `ready`.
6. A description failure is invisible to board health, the same as today, and an upgrade
   failure never produces `EXIT_DEGRADED`.

---

## 6. Phase 3: preferences overlay, role families and location

### 6.1 Files

| File | Tracked | Holds |
|---|---|---|
| `criteria.yaml` | yes | Mechanics: seniority and shape tokens, `field_terms` default and presets, the **family catalogue** (name → tokens), location token lists by country. Nothing personal. |
| `preferences.yaml` | **gitignored** | Your stances, target bands (phase 4), field preset choice, location preference, `review_window_days`, and extra custom tokens per family. |
| `preferences.example.yaml` | yes | Documents the shape, with neutral defaults and no personal values. Like `answers.example.yaml`. |
| `profile.yaml` → gitignored, with `profile.example.yaml` tracked | | Your prose is personal too. Keep the bytes identical when moving it so that `prose_hash` does not move and no judgment is redone. |

- The Dockerfiles stop `COPY`ing `profile.yaml`. They copy the two `.example` files and
  `criteria.yaml`.
- gx10's wrappers mount `preferences.yaml` and `profile.yaml` `:ro`, beside
  `criteria.yaml`. This is a host-side change in `~/jobtracker/run-*.sh`, outside the repo.
  `docs/deployment.md` states the contract.
- `JOBTRACKER_PREFERENCES` and `JOBTRACKER_PROFILE` override the paths.

### 6.2 Schema (`preferences.yaml`)

```yaml
families:              # stance per catalogue family; absent = neutral
  backend: want
  infrastructure: want
  data-engineering: want
  sre: want
  frontend: avoid
  mobile: avoid
  ml-research: avoid
  hardware: avoid
  sales-engineering: avoid
custom:                # extra tokens appended to a catalogue family, or a new family
  backend: [storage engine]
location:
  preferred: {label: NYC, tokens: [New York, NYC, Manhattan, Brooklyn]}
  home_country: us     # selects criteria.yaml's country lists
review_window_days: 14
```

- **Loading is strict**, following `plugins.yaml`'s rule. An unknown key, an unknown
  family name, or a stance other than the three is an error that stops `check`/`work`,
  never a silent ignore.
- On `/settings` the page renders a banner instead, so that the page you open to fix it
  still works.

### 6.3 Merging into `Criteria`

- `load_criteria(criteria_path, preferences_path)` returns the **same `Criteria`
  dataclass**, so `match()` stays pure and does not change.
  - `want` families → `role_type_include`.
  - `avoid` families → `role_type_exclude`.
  - `neutral` families contribute nothing (a neutral token does not satisfy the field gate
    on its own, which matches today).
- **`avoid` wins over `want` in the same title** ("Mobile Backend Engineer" → REJECT), the
  same as today's step 2. A REJECT names the family: `excluded_role:mobile(ios)`.
- `locations_nyc` becomes `locations_preferred`, with the label carried along.
  `location_rank()` keeps its four ranks and its check order (preferred → non-home →
  home → unknown).
  - `LOCATION_RANKS[0]` is the preferred label, `data-loc` values become
    `preferred|home|unknown|abroad`, and the "NYC" pin renders the label.
  - Old `locations_nyc`/`locations_us` keys load with a WARNING naming the new key, for
    one release.
- **The zero-diff gate:** with your `preferences.yaml`, `eval --replay-all` on the phase 3
  commit must show **no verdict changes** against the phase 2 head. It is a CI test on a
  fixture corpus and a manual check against gx10's DB. Phase 3 is a refactor for you, and
  anything that moves is a bug.

### 6.4 The `backend_fit` rename

- The wire schema field becomes `fit`, and the prompt reads it as *"how well the actual
  work matches the candidate's target roles"*. The target roles come from `profile.yaml`
  prose, which is already the case.
- **The DB column `rankings.backend_fit` keeps its name.** Migrations are additive and
  rollback is by tag, so renaming the column would break an older image reading a newer
  DB. Python reads it as `fit` through one accessor, and a comment at the schema says why.
- A schema change is a change to the question, so the `prose_hash` input includes a schema
  version. This re-judges once, and that cost is accepted and named in the commit.

### 6.5 Missing-file guard

- `runs` (or a one-row `settings_state` table) stores `preferences_hash`, the hash of the
  merged preferences that the last successful `check` ran with.
- The cases:

  | State | Outcome |
  |---|---|
  | No file, no stored hash | Fresh install: neutral, WARNING, and the run goes ahead. |
  | No file, stored hash equal to the neutral hash | Neutral; the run goes ahead. |
  | No file, stored hash non-neutral | `check` and `work` **exit 1 ("could not run") before writing any verdict**. The message names the missing path and the env var. |
  | File present and malformed | Exit 1, the same as `plugins.yaml`. |
  | File present, hash differs from stored | The run goes ahead and logs at INFO *"preferences changed since last run"* with the rematch delta. |

- `serve` renders a banner on every page in the exit-1 states, and `/readyz` returns 503
  with `checks.preferences`. That is a readiness concern, because `serve` writes verdicts on
  rematch.
- A deliberate reset is `jobtracker preferences reset`, which records the neutral hash.

### 6.6 The Settings card

- One row per catalogue family: its name, its tokens (read-only, with custom tokens marked),
  and a three-way control.
- **Changing a control does not write.** It calls `POST /api/preferences/preview`, a pure
  read that merges the proposed stance, replays `match()` over all open postings, and
  returns the delta plus up to 10 sample titles per moved bucket, with any pinned rows
  counted separately as "held by your rulings". The card shows that preview inline.
- **Apply** POSTs `/api/preferences` with the full stance it wants, not "toggle", so a
  double click lands once. It writes `preferences.yaml` through `safewrite.py` as
  line-oriented text surgery, then runs `rematch` and shows the actual delta.
- One Apply per family, following the downloads card: four independent decisions are not
  one save.
- `preferences.yaml` joins the list of files `serve` may write on a click (see *Adding a
  company*). DESIGN.md §2.3 is intact, because no scheduled run writes it.
- Custom tokens are added through an add box per family, and **there is no delete
  control**, following `/tuning`'s rule. To remove one, edit the file.

---

## 7. Phase 4: level bands, stored readings, the field gate and feed declarations

### 7.1 Bands

- `criteria.yaml` defines bands with title tokens:
  - `intern: [intern, internship, internships, co-op]`
  - `entry: [new grad, graduate, entry level, junior, associate, engineer 1, …]`
  - `mid: [ii, engineer 2, …]`
  - `senior: [senior, sr, sr., iii, lead, …]`
  - `staff: [staff, principal, distinguished]`
  - `management: [manager, director, head of, vp]`
- Shape exclusions (`contractor`, `clearance`, `N+ years`) leave the seniority list.
  `clearance` and `contractor` become **constraints** in `preferences.yaml` (`constraints:
  [no-clearance, no-contract]`), because a person with a clearance wants those roles.
- `preferences.yaml`: `level.targets: [entry]` (a list, so "entry or mid" can be
  expressed).
- `match()`:
  1. Constraints → REJECT.
  2. Avoided family → REJECT.
  3. A band token in a target band, plus the field gate → MATCH. (Without the field gate it
     is a REJECT, `non_field_role`.)
  4. A band token only in non-target bands → REJECT `band:<name>`.
  5. Tokens from both a target and a non-target band ("Senior … New Grad Program") →
     UNCERTAIN, with reason `band_conflict`. Today `exclude_titles` runs first and rejects
     these.
  6. No band token → UNCERTAIN.
- **Zero-diff gate again:** with `targets: [entry]` and your constraints, the replay must
  match the phase 3 head except for rows the `band_conflict` rule moves. Those are listed
  and approved one by one in the commit body.
- `intern` is a band, so the `level` prompt's "an internship is not entry-level" becomes
  "report `intern`", and Python decides.

### 7.2 Stored readings replace model pins

- `level` reports an absolute band (`intern|entry|mid|senior|staff|management|unclear`)
  plus grounded evidence into `level_readings` (§4.4). **The prompt never mentions your
  target.**
- The caller path (`cmd_check`, `cmd_rematch`, `serve`) applies a reading where the title
  gave no band token. Precedence is human override > reading > rules, and this happens
  outside `match()`, the same way overrides are applied today.
- Changing `level.targets` re-derives every verdict **with zero model calls**. This is the
  same contract as weights against prose in `profile.yaml`.
- Migration: existing `decided_by='llm'` overrides that survived the phase 1 audit become
  `level_readings` rows (`entry`/`not_entry` → `entry`/`senior`, since `not_entry` meant
  "above new grad", recorded as `senior` with the reason `migrated`). Then the overrides
  are cleared. The count is logged.
- `entry_risk` in `judge` is renamed `level_risk`, "risk this is not in the candidate's
  target band". The prompt reads the target band names from preferences, which is allowed
  because `judge` already reads `profile.yaml` prose.

### 7.3 The field gate

- `engineering_terms` → `field_terms`. `criteria.yaml` ships `field_presets: {engineering:
  [...], data: [...], design: [...]}`, and `preferences.yaml` picks one with `field:
  engineering` or lists terms directly.
- **An empty or absent gate means no gate**, named at WARNING on every run, following the
  `keywords.yaml` rule that empty means unrestricted, never nothing allowed. With no gate,
  `queue.lane` never yields `no_signal`, and the fold is empty.
- `looks_engineering` → `has_field_signal`, the one function both `level.pending()` and
  `queue.lane` call.
- The `level` and `judge` prompts lose "software". They speak of "this job" and read the
  field name from preferences.

### 7.4 Feeds declare their coverage

- `Plugin.coverage() -> {"bands": [...], "prose": bool}`. Simplify returns
  `{"bands": ["entry"], "prose": False}`, YC returns `{"bands": [], "prose": True}`, and
  Discord returns empty bands with `prose: True`.
- The `feed:<band>` lane and its label fire only when the declared band is in
  `level.targets`. For a mid-level user, Simplify rows sort as ordinary `signal`.
- YC's listing paths are already a plugin setting (`paths` in `plugins/ycombinator.py::defaults`,
  capped at six). Nothing to build. Its default stays your current paths.

---

## 8. Cross-cutting

### 8.1 CLAUDE.md changes (in the same commit as the code)

- *Goals* stays, but is relabelled as the maintainer's own preferences, which live in
  `preferences.yaml` and `profile.yaml`, not in code.
- New rules:
  - `queue.lane` is the single derivation.
  - `/review` is a read across the partition.
  - `.rv-yes`/`.rv-no` carry no `data-act`.
  - The `feed` → `ats` upgrade is the only exception to write-once.
  - Resolve the ATS from the URL, never the key.
  - Preferences are loaded strictly, and a missing file is refused when one existed
    before.
  - The zero-diff gate applies to any preferences refactor.
  - The model reports bands and never targets.
- Fix the known doc bug about Member of Technical Staff. Under bands, `staff` is in the
  `staff` band, so the title is still rejected for an entry target. State that plainly in
  CLAUDE.md, `docs/llm.md` and `resolve.py`.

### 8.2 Tests (named after the failure they prevent)

- `test_ungrounded_level_reading_is_no_answer`
- `test_entry_reason_carries_evidence`
- `test_prose_less_feed_row_is_not_level_work`
- `test_unclear_is_recorded_and_not_retried`
- `test_audit_pins_never_touches_a_human_override`
- `test_audit_is_dry_by_default`
- `test_lane_is_the_only_derivation` (a source scan for lane strings outside `queue.py`)
- `test_review_rows_are_each_on_exactly_one_surface`
- `test_review_buttons_have_handlers_and_no_data_act`
- `test_board_scope_never_reaches_match`
- `test_grad_token_now_covers_graduate`
- `test_sr_does_not_fire_inside_sre`
- `test_engineer_i_does_not_fire_on_engineer_ii`
- `test_upgrade_resolves_lever_slug_from_url_case_preserved`
- `test_upgrade_never_enters_wanted`
- `test_description_upgrade_rejudges`
- `test_thin_row_score_is_capped`
- `test_missing_preferences_after_non_neutral_run_exits_1_before_any_verdict`
- `test_fresh_install_runs_neutral`
- `test_preview_is_a_pure_read`
- `test_apply_carries_desired_state`
- `test_avoid_beats_want`
- `test_maintainer_preferences_replay_zero_diff` (fixture corpus)
- `test_target_change_needs_no_model_call`
- `test_prompt_never_names_target`
- `test_empty_field_terms_means_no_gate_and_warns`
- `test_feed_lane_requires_target_band`

### 8.3 Known limitations, documented and not hidden

- `companies.yaml` tiers rank growth for *backend* work. A designer gets accurate matching
  over a source list curated for someone else. `docs/companies.md` says so.
- Feed rows whose link is a bespoke careers page stay `thin` indefinitely.
- A title with no field token that is genuinely in the field still folds. That is the
  existing blind spot, now folded rather than buried among 3,000 rows. It is visible and
  counted, but it is folded.

### 8.4 Rollout on gx10

The nightly runs from the image, and `serve` runs from the working tree.

1. Phase 1 image: run `audit-pins` dry, read the buckets, then `--write`, after the phase 2
   token commit (§4.3).
2. Phase 2: `git pull` plus `systemctl --user restart jobtracker-serve` for `/review`.
   Confirm the counts against §1's baseline.
3. Phase 3: add the `preferences.yaml` and `profile.yaml` mounts to both wrappers **before**
   pulling the image. The §6.5 guard catches a missed mount, but the first night needs the
   file there to record a non-neutral hash.

### 8.5 Success, re-measured four weeks after phase 2

- `/review` default view under about 150 rows, and the Today count under about 20 a day.
- `decisions` above 100 rows, covering both sides.
- Zero `llm` pins on rows whose `description_source = 'feed'`.
- Thin share of open matches falling month on month as upgrades land.
- At least one application whose row was in `/review` when you ruled on it, showing the
  list finds things.

---

## Handoff: the auto-apply spec (next interview)

Recorded now so the next interview starts from facts:

- **What you want:**
  - **One-click send** is the top priority.
  - **Overnight fill with morning approvals** exists to make one-click possible.
  - **Fully unattended** is wanted on the side.
- **Why it was switched off on 2026-09-10:** it was too unreliable, and setup friction
  (gx10 browser, noVNC, captchas).
- **What it is today:** `JOBTRACKER_PREFILL=1` brings back fill-then-gated-submit.
  `browser._submit` is the only click, and CLAUDE.md bans every programmatic submit.
  Unattended sending reverses a standing rule, and captcha workarounds stay off the table.
- **Drift found while interviewing, to fix whatever is decided:**
  - `server.record_submission` (`browser.on_submitted`) writes `applied` through
    `advance_application` but **does not call `submissions.freeze`**. So a fourth
    `applied` path bypasses the archive, against CLAUDE.md's "three callers".
  - `prefill.py`, `browser.py` and `live.py` never read `posting_answers`,
    `question_template`, `submissions.effective_resume`/`effective_letter` or
    `posting_letters`. They predate the posting page and would fill from `answers.yaml`
    only.
- **Dependency on this spec:** unattended sending needs grounded verdicts (phase 1),
  rulings in `decisions` (phase 2) and non-thin descriptions (§5.6). Do not let unattended
  submit act on a `thin` row or a model-only match.
