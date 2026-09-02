# Slug repair

`jobtracker repair` — the second of the model's five bounded roles (DESIGN.md §8), and
the only one where the model is a fallback rather than the mechanism.

Boards move. Mercury and Vercel migrated Ashby → Greenhouse and left dead Ashby boards
behind; HubSpot's live board is `hubspotjobs`, not `hubspot`. `health.py` already detects
all of that and then stops — somebody opens the careers page by hand, finds the new
identifier, and edits `companies.yaml`. That was fifteen entries of manual work in the
2026-07-09 audit. This command does the finding and the verifying; a human still does the
approving.

```
jobtracker repair                            # detect, verify, propose. Writes nothing.
jobtracker repair --company Vercel           # one board, bypassing the detector
jobtracker repair --write                    # apply the verified proposals
jobtracker repair --llm-provider vllm --llm-url http://box:8000
```

---

## What triggers it

A deterministic detector over `board_health` in `state.db`. Three triggers, and the third
is an addition to what DESIGN.md §8 anticipated:

| Trigger | When |
|---|---|
| `IDENTITY_DRIFT` | immediately |
| `FETCH_FAILED` | after `REPAIR_FAILURE_THRESHOLD` (2) consecutive nights |
| `SUSPECT_EMPTY` | **only** when `alerting` |

Drift needs one observation because the identity assertion is deterministic over two
stable strings — a second look tells you nothing the first did not.

`FETCH_FAILED` needs persistence because `fetch.py` has already burned `MAX_RETRIES` with
backoff inside a single run, so anything transient was absorbed a layer down before the
counter moved at all. Rewriting a hand-verified slug because a CDN hiccuped once is the
expensive mistake here. The counter is `board_health.consecutive_failures`, added for
this; `consecutive_empty_runs` next door counts a different thing (a board that answers
with *nothing* versus one that does not answer) and `last_ok_at` is not a substitute
either — it is NULL forever for a board that was never healthy, so "failed for three
nights" and "failed since day one" read identically.

`SUSPECT_EMPTY`-when-alerting covers the failure mode this repo cites most. A dead board
never presents as `FETCH_FAILED`: `greenhouse/hubspot` is real, reachable, and answers 200
with an empty array forever. `alerting` requires the board to have been populated at some
point, which is exactly what excludes **dbt Labs and Root Insurance** — correct slugs with
genuinely zero reqs, `SUSPECT_EMPTY` every night, and "repairing" them would corrupt
hand-verified data to fix nothing.

`check_method: manual` companies are never targets (a careers page is a scrape, and the
standing rule in CLAUDE.md is that they are never scraped). `aggregator` feeds are
excluded too — they have no slug to repair.

## Regex first, model second

The regexes in `repair.extract_candidates` read the careers page for a board URL:

```
boards-api.greenhouse.io/v1/boards/SLUG   job_board?for=SLUG   job_board/js?for=SLUG
job-boards.greenhouse.io/SLUG             boards.greenhouse.io/SLUG
jobs.lever.co/SLUG                        api.lever.co/v0/postings/SLUG
jobs.ashbyhq.com/SLUG                     api.ashbyhq.com/posting-api/job-board/SLUG
```

DESIGN.md §8 called slug recovery "genuinely unstructured work that resists a scraper."
Mostly it is not: a page that embeds a hosted board contains the board URL literally,
because that is how embedding works. Measured across eight live careers pages, the regexes
resolved three (Ramp, Vercel, Mercury) and found nothing on five.

Three rules are baked into the extractor and each has a test:

- **Case is never touched.** `lever/Onehouse` resolves and `lever/onehouse` 404s.
- **The first path segment only.** `jobs.ashbyhq.com/ramp/8f3c-uuid` is `ramp`.
- **URL structure is not a slug.** `embed`, `job_board`, `js`, `posting-api` and friends
  are on a denylist, because `boards.greenhouse.io/embed` is a substring of every
  Greenhouse embed URL ever written, and a board "repaired" to `embed` would look like a
  successful fix right up until the next fetch.

The model is asked only when the regexes produced no candidate that verified. It gets the
page condensed to its links plus visible text and answers `{ats, slug, evidence}` or
`"none"`. Then two gates:

1. **Grounding.** The slug must appear on the page it was shown, after a `/` or an `=`,
   case-sensitively. A bare substring test is not enough — the company name is all over
   its own careers page (`acme` occurs in `acme.example` on every line), so every invented
   slug would ground. Deriving an identifier from the company name is precisely the guess
   CLAUDE.md forbids and the most likely way an obliging model answers a page with no
   board on it. The prompt says so; this enforces it, because a prompt is not an
   enforcement mechanism.
2. **The same verification a regex candidate faces**, below.

So the model cannot propose anything a regex could not have proposed. It can only find one
on a page the regexes could not parse.

## Verification: nothing is proposed on the strength of a string

Every candidate is fetched through `Fetcher.fetch_company` on a
`dataclasses.replace(company, ats=…, slug=…)` — the real adapter for that ATS, under the
same per-host limiter, retries and trace shape as a nightly board. `repair.judge_candidate`
then decides, in this order:

| Outcome | Meaning |
|---|---|
| `unchanged` | The page still advertises the board that is failing. Not a rejection — a finding: the board did not move, it is down. |
| `unreachable` | Failure is not evidence (§7.3). |
| `zero_jobs` | **The `greenhouse/hubspot` rule.** A real-but-dead board answers 200 with `[]`. |
| `no_identity` | The identity endpoint gave us nothing. |
| `wrong_company` | **The `ashby/cedar` rule.** |
| `ok` | Proposed. |

Two of these deserve their reasoning spelled out.

**`zero_jobs` is deliberately stricter than `health.evaluate`,** which tolerates an empty
board. An established board has history in `board_health` separating "always empty" from
"emptied on Tuesday"; a candidate has none. Adopting an empty one would also convert a loud
`FETCH_FAILED` into a quiet `SUSPECT_EMPTY` — trading a visible break for an invisible one,
which is the §7.1 bug wearing a new hat.

**`no_identity` must be checked before `wrong_company`.** `health.identity_matches` returns
`True` when either side is empty — "don't cry drift on missing data" — which is right for
the nightly loop and catastrophic here, where it would silently turn "the identity endpoint
500'd" into "verified".

### The Ashby/Lever asymmetry

Only Greenhouse has an `identity_url`: a *different endpoint* from the one the slug was
used on, returning a board name. Agreement there is real evidence, and a proposal from it
is recorded as `evidence_kind: identity`.

Ashby and Lever derive identity by reading the org slug back out of the first job URL,
which for a *candidate* slug simply restates the candidate. Comparing them is a tautology —
`ashby/cedar` sails straight through it. For those two the evidence is **provenance**: the
link was read out of HTML served by the company's own curated `careers_page`, never
constructed from its name. That is a genuine claim and a weaker one, so it is labelled
`evidence_kind: provenance` rather than dressed up, and every rendering of such a proposal
carries a warning plus three sample titles — so the reviewer can do in two seconds what
DESIGN.md §7.2 asks for: read a few job titles.

Ashby's `ApiJobBoardWithTeams` GraphQL operation would give a real board name and close
this gap. Adding it is a follow-up, not a prerequisite.

## Propose, then apply

`repair` writes a `repair_proposals` row and prints a unified diff. It does **not** touch
`companies.yaml` without `--write`. That separation is the point: no scheduled run may
write curated data (DESIGN.md §2.3 is an entire section on why mixing curated slugs with
machine state was v1's third failure).

DESIGN.md §8 said "open a pull request". It does not — a PR needs a git identity and push
credentials the container does not have and should not have, and this repo knows nothing
about orchestrators. The reviewable-diff property survives intact, one `git diff` later.

**`--write` changes three fields: `ats`, `slug`, and `expected_board_name`.** The third is
not optional — leaving the dead board's name behind would make the *repaired* board fail
identity on the very next run, turning a fix into a new `IDENTITY_DRIFT`.

**The writer is line-oriented, and that is load-bearing.** Round-tripping the document
through `safe_load`/`safe_dump` re-folds every long string to PyYAML's width, so changing
one `slug:` also re-wraps the hand-written `notes:` prose on unrelated entries — measured
on the real file, a one-line repair produced a diff touching ten other companies. A diff
you have to search for the change in is one nobody reads, and the whole deliverable here is
a diff somebody reads. `--write` also refuses if the file has grown `#` comments outside
the header, since those would not survive any YAML round-trip.

Exit codes: `0` when nothing needs repair or every detected board got a verified proposal;
`2` when boards are still broken with no verified fix — which is exactly what a human needs
to look at.

## Where proposals show up

- The daily report's **Board failures** section, indented under the board, with the apply
  command.
- The dashboard's **Boards** tab, in a "Proposed fix" column. Displayed, never actionable:
  applying rewrites curated data and belongs behind a command whose diff you read, not
  behind a click — so there is no button even under `serve`.

## The blind spot

A careers page that renders its board link in JavaScript cannot be repaired by anything
here, and sometimes not by anything at all. Measured: HubSpot's careers page is 519 KB of
shell containing neither the string `greenhouse` nor `hubspotjobs` anywhere — there is no
identifier on the page for a regex or a model to find. Stripe's page carries a
`greenhouseId` per posting but no board slug.

Those boards come back as `no_candidates` and stay visible in the report. That is the
tradeoff this project makes everywhere else too: surface the blind spot, do not hide it.
A headless browser would close it and is deferred indefinitely for the reason DESIGN.md §9
gives — high maintenance, low yield.

## Testing

`tests/test_repair.py` — no network anywhere. `repair.py` is pure and `repair_boards`
takes its page reader and verifier as arguments, so the whole decision path runs against
recorded HTML and recorded `FetchResult`s. The tests that matter most are the rejections:
`test_hubspot_a_real_but_dead_board_is_rejected` and
`test_cedar_a_live_board_belonging_to_someone_else_is_rejected` are named after the bugs
they exist to prevent and must stay red-on-regression forever.
