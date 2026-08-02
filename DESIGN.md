# Automated Job Discovery: Replacing an LLM Runtime with a Deterministic Pipeline

**Status:** Design complete, implementation in progress
**Author:** Dylan
**Last updated:** 2026-07-19

---

## 1. The problem

I track 82 companies for backend new-grad openings. Postings for a given cycle open and
close on the order of days, and the highest-signal ones (small infra companies, one or two
reqs) are gone fastest. Checking by hand does not scale past about a dozen companies, and
the ones I would drop first are exactly the ones I most want.

The requirement is narrow: **detect a matching posting within 24 hours of it appearing,
across 82 companies, without me doing anything.**

---

## 2. Version 1, and why it failed

The first version was a single markdown file — `backend-newgrad-2027-tracker.md` — holding
one `### Company` block per target with a flat `key: value` field list, plus a prose
runbook telling an LLM agent how to check each one. The agent fetched boards, applied
match criteria written as a YAML block, and edited `status` / `last_checked` /
`last_posting_seen` back into the file in place.

It worked well enough to be worth keeping the data. It failed as a system, in three
distinct ways.

### 2.1 Nothing was ever actually parsed

The match criteria lived in a fenced YAML block. It looked like configuration. It was
consumed by a language model reading it as prose, so no parser ever touched it — and it
turned out not to be valid YAML at all:

```yaml
level_include: [new grad, ..., "SDE I, "SWE I", "Software Engineer I", junior, grad]
                                ^ unterminated quote
```

`yaml.safe_load` throws on this. The bug had been present since the file was created and
was invisible for the file's entire life, because the only consumer was a model tolerant
enough to guess the intent. **A config format nothing validates is not configuration; it
is a comment.** This single line is the clearest statement of the problem with the
architecture: the system had no component that could be *wrong* in a detectable way.

### 2.2 The failure modes were all I/O failures, not reasoning failures

Over one audit pass I catalogued the ways the checker silently produced wrong results.
Every one is an infrastructure concern that an LLM is a poor place to handle:

| Observed failure | Root cause | Correct layer |
|---|---|---|
| Fetching ~12 boards in parallel returned `http=000` for *every* host, including ones not being hammered | Egress throttling, presenting as universal breakage | Concurrency cap + backoff |
| `greenhouse/hubspot` returns HTTP 200 and an empty array forever; the live board is `hubspotjobs` | Empty response conflated with "no openings" | Response invariant |
| `ashby/cedar` returns live postings belonging to an unrelated real-estate company | Slug reachability conflated with slug identity | Identity assertion |
| Full-content payloads blew a 20s timeout on large boards (Databricks carries ~787 reqs) | No pagination/field selection discipline | Fetch layer |

Fifteen of 56 API entries were wrong at audit time — roughly 27%. None of these are
judgment calls. They are retries, rate limits, and assertions.

### 2.3 State and configuration were the same mutable file

`slug` (curated, changes rarely, human-authored) and `last_checked` (rewritten every single
run, machine-authored) lived in the same git-tracked file. Consequences:

- Every daily run produced a large diff, so version history stopped being readable and the
  audit trail for *curation* decisions was buried under state churn.
- The only record of a posting was a title appended to a `last_posting_seen` string. There
  was no way to ask when a posting first appeared, whether it had closed, or whether it had
  been reposted — the questions that actually matter when deciding what to apply to today.
- A single bad in-place edit across 82 entries could corrupt curated data that took hours
  of manual verification to produce, with no clean way to distinguish the corruption from
  legitimate state updates in the diff.

---

## 3. Design principles

The rewrite is organized around one question asked of every component: **does this require
judgment?**

1. **Determinism by default.** Fetching, parsing, diffing, and storing are pure I/O and set
   arithmetic. They get ordinary code, with tests.
2. **The model handles ambiguity, not throughput.** A language model is invoked only where
   the input is genuinely fuzzy — unstructured job-description prose — and only on the small
   residual the deterministic layer could not classify.
3. **Separate curation from observation.** Human-authored target data and machine-authored
   run state are different files with different lifecycles and different writers.
4. **Absence of data is never evidence of absence.** Zero results and an error are distinct
   states, and both are distinct from a verified empty board.
5. **Every automated verdict is stored with its reason.** If the system decides a posting
   does not match, I can go read why. Silent filtering is indistinguishable from a bug.

---

## 4. Architecture

```
┌───────────────────┐
│  companies.yaml   │  curated · human-authored · git-tracked · never machine-written
└─────────┬─────────┘
          │
          ▼
   ┌─────────────┐     ┌──────────────────────────────────┐
   │   fetch     │────▶│ sources/  greenhouse · lever ·   │
   │             │     │           ashby · workday        │
   │ • conc. cap │     │ each normalizes to Posting       │
   │ • backoff   │     └──────────────────────────────────┘
   │ • per-host  │
   │   rate limit│
   └──────┬──────┘
          │  List[Posting]                    ┌──────────────┐
          ▼                                   │  health.py   │
   ┌─────────────┐                            │ invariants   │
   │   match     │  MATCH / REJECT / UNCERTAIN│ · identity   │
   │             │                            │ · empty-board│
   │ rules first │──── UNCERTAIN ────▶ LLM    │ · fetch fail │
   └──────┬──────┘      (batched, schema'd)   └──────┬───────┘
          │                                          │
          ▼                                          ▼
   ┌──────────────────────────────────────────────────────┐
   │  store.py  →  state.db  (SQLite)                     │
   │  postings · verdicts · runs · board_health           │
   └──────────────────────┬───────────────────────────────┘
                          ▼
                    ┌───────────┐
                    │  report   │  new · uncertain · failures · manual-check
                    └───────────┘
```

Package layout:

```
jobtracker/
  models.py     Posting, Verdict, BoardHealth — the shared vocabulary
  sources/      one adapter per ATS; the only place vendor JSON shapes are known
  fetch.py      HTTP concurrency, retry, rate limiting
  match.py      pure function: Posting -> Verdict. No I/O, fully unit-testable
  store.py      SQLite persistence, first_seen/last_seen computation
  health.py     board-level invariants and drift detection
  report.py     rendering
  cli.py        check · verify-slugs · report · add-company
```

---

## 5. Data model

### 5.1 `companies.yaml` — curation

```yaml
- name: Confluent
  ats: ashby
  slug: confluent
  tier: 1
  category: infra-devtools
  check_method: api
  expected_board_name: Confluent     # asserted on every run — see §7.2
  notes: Kafka company — strong distributed-systems exposure.
```

No `status`. No `last_checked`. Nothing in this file changes as a result of a run, so its
git history is a clean record of curation decisions only.

### 5.2 `state.db` — observation

```sql
CREATE TABLE postings (
    company        TEXT NOT NULL,
    ats_job_id     TEXT NOT NULL,      -- stable identifier from the ATS
    title          TEXT NOT NULL,
    location       TEXT,
    url            TEXT NOT NULL,
    posted_at      TEXT,               -- vendor-reported, often unreliable
    first_seen     TEXT NOT NULL,      -- our observation; trustworthy
    last_seen      TEXT NOT NULL,
    closed_at      TEXT,               -- set when absent from a healthy fetch
    PRIMARY KEY (company, ats_job_id)
);

CREATE TABLE verdicts (
    company     TEXT, ats_job_id TEXT,
    verdict     TEXT NOT NULL,         -- MATCH | REJECT | UNCERTAIN
    reason      TEXT NOT NULL,         -- which rule fired, or model rationale
    decided_by  TEXT NOT NULL,         -- 'rules' | 'llm'
    decided_at  TEXT NOT NULL,
    PRIMARY KEY (company, ats_job_id)
);

CREATE TABLE board_health (
    company        TEXT PRIMARY KEY,
    last_status    TEXT NOT NULL,      -- OK | SUSPECT_EMPTY | IDENTITY_DRIFT | FETCH_FAILED
    consecutive_empty_runs INTEGER NOT NULL DEFAULT 0,
    observed_board_name    TEXT,
    last_ok_at     TEXT
);
```

Giving postings a primary key of `(company, ats_job_id)` is the change that makes
everything downstream easy. Diffing becomes a set difference in SQL rather than a model
comparing prose against a free-text field. `first_seen` — *our* first observation, not the
vendor's `posted_at`, which is frequently the last-edited timestamp — makes "posted in the
last 48 hours" a real query. Reposts, closures, and staleness all fall out of the same
table for free.

---

## 6. Matching: a three-way verdict

The naive design is a boolean predicate. That is wrong, and the reason is worth stating.

ATS titles are more standardized than they appear. `Senior Staff Engineer, Platform` and
`Software Engineer, New Grad (2027)` are both trivially classifiable by rule. The
deterministic layer handles these, and it handles the large majority.

The residual is real, though: `Software Engineer, Distributed Systems` with no level in the
title, where eligibility is a sentence buried in the description body. A boolean forces
this case into a wrong answer in one of two directions — drop it (miss a good role) or
surface it (train myself to ignore the report). Both are worse than admitting uncertainty.

```
match(posting) -> Verdict:
    if hits exclude rule            -> REJECT(rule_name)
    if title carries explicit level -> MATCH | REJECT (rule_name)
    else                            -> UNCERTAIN
```

`UNCERTAIN` is where the language model belongs, and the constraints on that call are the
point:

- **Batched**, over the day's uncertain set, not one call per posting.
- **Schema-constrained** output: `{match: bool, reason: str, grad_year: int | null}`.
- **Bounded** — if the uncertain bucket is large, that is a signal my rules are bad, and it
  is visible as a number rather than absorbed as cost.
- **Persisted with rationale**, so model verdicts are auditable against my own judgment.

The intended sequencing is deliberate: ship with *no* model in the loop, run for a week,
and read the `UNCERTAIN` bucket by hand. That measures how much fuzzy judgment the problem
actually contains before any of it is automated.

---

## 7. Health invariants

This is the subsystem that exists specifically because of the §2.2 failure catalogue. Each
invariant encodes a bug that was observed in production.

### 7.1 Empty is not zero

A board returning zero postings enters `SUSPECT_EMPTY` rather than recording a clean run.
It escalates to an alert only after N consecutive empty runs *and* only if the board has
ever been non-empty.

This distinction is load-bearing. Two boards in my target list — `greenhouse/dbtlabsinc`
and `greenhouse/root` — are correct slugs with genuinely zero open reqs. The history in
`board_health` distinguishes "always been empty" from "was populated last Tuesday," which
is precisely the difference between a company with no openings and a company that migrated
ATS. A stateless checker cannot tell these apart; that is why the first version reported
migrations as "no matches."

### 7.2 Reachability is not identity

Before trusting a board's contents, assert it belongs to the right company:

- **Greenhouse:** `GET /v1/boards/{slug}` returns `.name`; compare against
  `expected_board_name`.
- **Ashby:** inspect `.jobs[0].jobUrl` host and sample titles.

Mismatch → `IDENTITY_DRIFT`, contents discarded rather than reported. This is the
`ashby/cedar` bug — a slug that returns HTTP 200 and a full page of real postings that
belong to an unrelated company with a similar name. Status codes cannot catch it; only an
identity assertion can.

A related trap: Ashby's `jobs.ashbyhq.com/{slug}` returns a 200 SPA shell for *any* string,
so it is useless for verification. Only the posting API or the GraphQL board endpoint
gives a real answer.

### 7.3 Failure is not absence

Non-200, timeout, and malformed JSON all produce `FETCH_FAILED`, are retried with backoff,
and appear in the report's failure section. A failed check never contributes to "no new
postings today."

---

## 8. Where the model still earns its place

The rewrite does not eliminate the language model. It relocates it from the runtime to
bounded roles — two when this was written, three now:

1. **Ambiguity resolution** (§6) — schema-constrained, on the residual only.
   **Implemented** as `jobtracker resolve`; see `docs/llm.md`. It came out narrower than
   this section anticipated in three ways worth recording:

   - **Local only.** The provider is an address you point at (vLLM first), not a hosted
     API. There is no key handling anywhere in `jobtracker/llm/`.
   - **Level extraction only.** It answers "what experience level does this description
     require" and nothing else. An `entry` reading still has to pass the *rules'* own
     engineering gate before it can produce a MATCH, so the `Finance Associate` guard
     holds against a model verdict exactly as it does against a rule one. The model
     supplies a missing fact; the criteria decide what to do with it.
   - **Failure is absence.** Unreachable, slow, malformed, or unsure all leave the
     posting UNCERTAIN — where it already was. The pass can add resolution; it cannot
     subtract correctness. This is the §7.3 principle applied to inference.

   Constrained decoding (`guided_json`) turned out to matter more than model choice:
   malformed output stops being a failure mode you parse around. The client validates
   anyway, since a server ignoring the field would otherwise let prose through as a
   verdict.
2. **Repair.** When `health.py` raises `IDENTITY_DRIFT` or persistent `FETCH_FAILED`, an
   agent is dispatched to read the company's careers page, locate the current board
   identifier — a Greenhouse `job_board?for=X` embed, or a link to `jobs.lever.co/X` — and
   open a pull request against `companies.yaml`.

   Recovering a slug from an arbitrary careers page is genuinely unstructured work that
   resists a scraper, and it is where an LLM has a real advantage. But it would run as an
   **exception handler, invoked by a deterministic detector**, against a human-reviewed
   diff — not as the main loop. The system decides *that* something broke; the model helps
   decide *what to do about it*. Still deferred.

3. **Ranking** (added 2026-08-02) — **implemented** as `jobtracker rank`; see
   `docs/ranking.md`. Matching answers "is this on-target"; this answers "which of the
   on-target ones should I do something about tomorrow", which is a question about the
   *candidate*, not the posting, and so has no rule form. Career goals do not reduce to
   tokens.

   It stays bounded the same three ways role 1 does, and one more:

   - **Local only**, same client, same providers, no key handling.
   - **One posting at a time.** The model returns three labelled ordinals and a sentence
     about a single posting. It never sees another, never returns a score, and never
     returns an order — deterministic Python composes the score from weights in
     `profile.yaml`. The model supplies facts; the arithmetic stays where it can be
     diffed and tested.
   - **Failure is absence.** Any failure leaves the posting *unjudged*, which means
     excluded from the picks and counted in a visible "N unranked" line — never scored
     wrongly, and never silently dropped (§7.3 again, and §3.4).
   - **Off the main loop entirely.** Ranking cannot change a verdict. It orders postings
     that matching already accepted, in a separate table, so a bad judgment costs you one
     misplaced row and never a wrong match.

   The general principle across all three: the model is allowed to *read*, never to
   *decide*. Level extraction reads a description for a fact the title omitted; ranking
   reads it for a fit judgment no rule could encode. In both cases deterministic code
   holds the verdict, the ordering, and the reason.

---

## 9. Coverage limits

Of 82 targets, 24 are `check_method: manual` — Workday tenants, bespoke portals, Gem — with
no keyless public JSON board. These are surfaced in a weekly "check by hand" section,
rate-limited per company.

Surfacing them is a deliberate choice over hiding them. A tracker that silently covers 71%
of its target list while presenting itself as complete is worse than one that reports its
own blind spots, because the failure is invisible at exactly the moment it matters.

Planned expansion, in descending value-per-hour:

- **Aggregator sources.** Community-maintained new-grad listing repositories are already
  structured, high-yield, and free to diff. Highest return of any item here.
- **Workday.** The `/wday/cxs/{tenant}/{site}/jobs` endpoint returns JSON without auth.
  Fragile and per-tenant, but it covers 8 of the 24 manual entries.
- **Headless browser** for the remaining bespoke portals. High maintenance, low yield;
  deferred indefinitely.

---

## 10. Implementation status

| Component | Status |
|---|---|
| Design (this document) | Complete |
| `models.py`, `fetch.py`, `match.py` | **Complete** — implemented + unit-tested |
| `sources/`, `store.py`, `health.py`, `report.py`, `cli.py` | **Complete** |
| `criteria.yaml` + validating loader (the §2.1 fix) | **Complete** |
| Markdown → `companies.yaml` migration | **Complete** — all 89 entries, 0 data loss |
| Container packaging (Dockerfile, volume-mounted `state.db`) | **Complete** |
| Test suite (`tests/`) | **Complete** — 127 passing |
| Grafana operational dashboard (`otel/grafana-dashboard.json`) | **Complete** — provisioned, 9 panels |
| Job-search dashboard (`jobtracker dashboard` → HTML) | **Complete** — self-contained, no network |
| First live run against the 56 API boards | **Complete** — see below |
| Curated target data (89 companies, 56 slugs hand-verified) | **Complete** — carries over unchanged |
| Unattended operation (exit codes + container contract) | **Complete** — `docs/deployment.md`; no orchestrator in-repo |
| Tuning loop (decisions, `eval`, suggestions, overrides) | **Complete** — `docs/tuning.md` |
| Tuning UI (`jobtracker serve`) | **Complete** — stdlib only, localhost, writes back |
| Ambiguity pass (§6) — local, provider-pluggable | **Complete** — `docs/llm.md`; vLLM first |
| Slug-repair agent (§8) | Deferred |
| Aggregator sources (§9) | Deferred — still never fetched |

The verified slug data is the asset worth preserving from version 1. The audit that
produced it — fetching every board, confirming identity against the company name, and
correcting 15 wrong entries — is the empirical work this design is built on, and it
migrates into the new schema intact.

**First live run (2026-07-20).** All 56 API boards resolved and passed identity
verification; `expected_board_name` was seeded for each via `verify-slugs --write`. 8,203
postings ingested across 54 healthy boards; the only two non-OK boards were `dbtlabsinc`
and `root` — the known valid-but-empty pair — correctly `suspect_empty` and not alerting.
Zero `fetch_failed`, zero `identity_drift`. The first matching pass exposed exactly the
kind of rule gap §6 predicts: "associate" alone matched business roles (Finance/Operations
Associate), so a MATCH now additionally requires an engineering signal — this dropped MATCH
from 129 (mostly noise) to 27 genuine SWE entry/new-grad roles, leaving 1,109 UNCERTAIN to
read by hand.

**Since that run.** The ambiguity pass (§6/§8) is implemented and local; the slug-repair
agent (§8) is still deferred. Two things the first live run could not have shown:

The engineering gate was necessary but not sufficient. Stripe's *"Seller Systems
Operations Associate (Night Shift)"* matched on `level:associate+role:systems` — the
`Finance Associate` bug again, arriving through `role_type_include` instead of through a
bare level token. Fixing one instance by hand-editing YAML is how the *first* one came
back, so the response was a tuning loop (`docs/tuning.md`): judgments are recorded as a
regression corpus, and `jobtracker eval` replays any proposed rule change against it
before it ships. The rule matters less than being able to tell whether the next rule
breaks something already decided.

The UNCERTAIN bucket is not what §6 assumed. Of 1,537 open uncertain postings, only 674
have any engineering signal in the title at all; the rest are `Field Marketer` and
`Talent Strategist`. The residual is dominated not by genuinely ambiguous levels but by
roles the tracker was never going to want — which is a scoping fact the design should
have predicted and did not.

---

## 11. What I would tell someone building this

The instinct to reach for a language model was not unreasonable — it produced a working
prototype fast, and the prototype is what surfaced the requirements. The mistake was
leaving it in the runtime after the requirements were known.

The clarifying question turned out to be *which parts of this genuinely require judgment* —
and the answer was: almost none of it. Fetching, parsing, diffing, and storing are solved
problems with well-understood failure modes. Those failure modes stayed invisible only
because the component handling them was one that fails softly, plausibly, and without a
stack trace.

The value of moving to deterministic code is not primarily correctness or cost. It is that
the system acquires the ability to be *detectably* wrong — to assert an invariant, fail a
test, and raise an error someone can read.
