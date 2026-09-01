# One req, one row, however it arrived

A job reaches this tracker by several roads. Stripe's own Greenhouse board publishes it,
the Simplify new-grad README lists it, and a Discord channel announces it. Those are three
rows in `postings` describing one application you can submit exactly once, and two of them
are noise on a page whose whole job is to be short.

So every posting now carries a `dedupe_key`, and a redundant copy is either never imported
or closed with its reason recorded.

```
key       derived from the URL, or from curated identity when we have it
rank      api (0)  <  aggregator (1)  <  plugin (2)
outcome   the best-ranked row stays open; the rest are closed as 'duplicate'
```

## The key is not the URL string

Three things break plain string equality, and the fix for each shapes `dedupe.py`:

**The identity is usually inside the path.** `jobs.lever.co/artera-2/eae88c70-...` names
the ATS, the board and the req. That triple is exactly what the tracker already stores as
`(ats, slug, ats_job_id)`, so reading it out of the link makes a Discord announcement meet
its own board's row even though the two URL strings differ.

**Query strings carry identity, and dropping them wholesale is a live hazard.** Two
separate cases. Greenhouse's `embed/job_app?for=X&token=Y` — the URL `browser.py` itself
builds — puts both halves in the query, so ATS extraction runs *before* the query is
dropped. And `gh_jid` is the Greenhouse job id: it appears on 6,019 stored URLs and equals
the row's `ats_job_id` on all 6,403 that carry it, none differing.

That second one is not a nicety. Betterment links all 41 of its live reqs to
`betterment.com/careers/current-openings/job`, distinguished by nothing but `gh_jid`.
Measured before the parameter was kept: **13 fallback keys covered 2,805 open postings** —
795 Databricks, 527 Stripe, 400 MongoDB, 213 Elastic — every one of them a distinct live
job wearing the same key. Tracking parameters (`utm_source`, and `t`, which holds
`gh_src`) are still dropped.

**Most stored URLs are not ATS URLs at all.** Measured over the live database: 9,150
postings, of which 2,342 are Greenhouse-hosted, 843 Ashby and 302 Lever. The other 5,663
are careers-site links, because 25 of 45 Greenhouse boards return one there and Stripe's
is a search page with no req id in it. A URL-only key would leave the majority of the
tracked corpus in a namespace no feed link could reach — which is why `key_from_identity`
exists and is preferred for any row that has curated identity.

Verified where both derivations apply: 3,487 rows agree, 0 disagree.

## Two normalization choices that look like bugs

- **The path is not lowercased.** Paths are case-sensitive on most servers, and this repo
  already has a live trap in that direction: Onehouse's board is `lever/Onehouse` and
  `lever/onehouse` 404s. Merging two genuinely distinct paths is worse than missing a
  duplicate, because a bad merge closes a real posting.
- **The slug and id *inside* an ATS key are lowercased**, because those are known
  case-insensitive identifiers within a vendor's namespace, and there the failure runs the
  other way — a case mismatch splitting one req into two rows.

`dedupe_key` returns None only for a non-http(s) URL. That totality is what makes
`backfill_dedupe_key` drain: it looks at `dedupe_key IS NULL`, and a URL it could decline
would be re-examined every night forever.

## Precedence, which is the entire safety argument

> **An `api` row is never closed in favour of a feed row.** A board row carries health,
> identity, a description and a prefillable form; a feed row is a pointer to it.
>
> **A row is closed by a peer only if it is a feed.** Two board rows sharing a key is a
> *finding* — almost certainly a key too coarse to tell two live reqs apart — and the
> honest response is to report it at WARNING and touch nothing.

The second rule is what makes it safe to run this across every source rather than only the
plugin, and its exact wording is load-bearing. It is **not** "unless it is `api`".

A company missing from `companies.yaml` has no `check_method` to rank by, and ranking it
below a feed would make *forgetting an entry* a way to close live postings. That is not
theoretical: running this pass against a deliberately empty companies file closed 795
Databricks rows in one go, because they lost their curated identity key and their rank in
the same step. So an unknown rank loses to a real board, still outranks every feed, and is
never closed by a peer. Only `aggregator` and `plugin` rows — redundant by construction —
can be.

Ties break on `first_seen`, then on the primary key, so the winner is stable across runs.

## Two mechanisms, because there are two situations

- **Never imported.** `append_postings` refuses at the door: a feed posting whose key
  already belongs to an open, better-ranked row is never written, so nothing enters
  `postings`, `verdicts`, the report or the ranking. This is where most of it happens,
  because the feeds are where the redundancy comes from.
- **Closed later.** `close_duplicates` runs once per `check`, **after every board has
  synced**, never inside the board loop. The winner of a shared key can be fetched later
  in the same run than the loser, and boards are fetched in `companies.yaml` order — so
  deciding per board would make which row survives depend on the ordering of a curated
  file. One pass over the whole open set is order-independent by construction.

## Why the reason lives in `closed_reason`

Not `verdicts`: `cmd_check` re-derives a verdict from the title for every posting it
fetches, which is the documented mechanism that erased 99 llm matches overnight on
2026-08-02. A reason written there lasts one night.

Not `overrides`: those survive rematch, but an override means *"I ruled on this role"*,
and a duplicate is a statement about the *row*. It would also collide with a genuine human
override on the same posting.

So: `closed_at` plus `closed_reason='duplicate'` plus `duplicate_of_url`. Closure is
already the vocabulary for "this row is no longer live", and it turns *"it vanished"* into
*"closed because this URL is the same req"* — DESIGN.md §3.5 with two columns and no new
table.

**And `sync_postings` must not undo it.** That function reopens any re-seen posting, which
is right for a board: a req that comes back is open again. But a duplicate's own feed still
lists it tomorrow, so an unconditional reset would undo the closure at 01:00 and remake it
at 01:05, every night, forever. The reopen is therefore conditional on the closure having
come from absence — a NULL `closed_reason`.

## Known blind spots

- **Simplify wrappers.** `simplify.jobs/p/<uuid>` shares no string and no job id with
  `jobs.lever.co/artera-2/eae88c70-...`. Resolving it means following a redirect per
  posting against a third party at ingest time, to save one duplicate row. Two Simplify
  links do dedupe each other; a Simplify link and its target do not. Pinned by a test that
  asserts the miss.
- **Greenhouse `absolute_url` is often not a Greenhouse URL** (25 of 45 boards). Mitigated
  for api rows by `key_from_identity`; unmitigated for a feed row that links to the
  employer's own careers page rather than the board.
- **Shorteners and tracking links** never match their target.
- **A careers-page link is not bridged to its board.** `betterment.com/...?gh_jid=X` keys
  as a URL while Betterment's own row keys as `greenhouse:betterment:X`, so a feed linking
  to the careers page rather than the board will not dedupe. Greenhouse ids look globally
  unique (8,005 ids across 47 boards, no collisions), which would make the bridge easy —
  and that is deliberately not built on, because "no collisions in one sample" is not an
  invariant, a miss costs one redundant row, and a wrong merge closes a real job.
- **Workday is deliberately not in the URL extractor.** Its human URL carries a locale
  segment and a title-derived path that does not follow a rename, and the req number is
  not always in it. `key_from_identity("workday", ...)` covers every Workday row we
  curate. A key that is merely missing costs one duplicate row; a key that is wrong closes
  a real posting.
- **A closed duplicate stops being rematched.** `cmd_rematch` filters `closed_at IS NULL`,
  so "a `criteria.yaml` edit reclassifies all of history" narrows by one row per
  duplicate. Correct — its twin is still open and still rematched — but worth saying.
- **The only ways back** from a dedupe closure are the winner closing or the key changing.
