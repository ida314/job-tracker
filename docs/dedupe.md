# One req, one row, however it arrived

A job reaches this tracker by several roads. Stripe's own Greenhouse board publishes it,
the Simplify listings file names it, and a Discord channel announces it. Those are three
rows in `postings` describing one application you can submit exactly once.

Every posting carries a `dedupe_key`, and knowing that is what lets the pages **say** so.

```
key       derived from the URL, or from curated identity when we have it
outcome   nothing is closed, hidden or refused for being a copy
          a shared key renders as "on company page"
          a key matching an application renders as "applied · <status>"
          the picks show one row per key (rank.one_row_per_req)
```

**This used to close things, and the change is the point.** Postings live on two pages now —
company career pages and job boards — and the same req legitimately appears on both. The
job board's copy is not noise: it is what that board published, and it is the row you would
read on that board. So `close_duplicates` and `dedupe.preferred` are gone,
`append_postings` no longer refuses an announcement of a job already tracked, and rows
closed by the old rule are reopened once on connect with the count logged.

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

## What replaced precedence

Ranking sources existed to decide which row to close. Nothing is closed, so what is left is
narrower and lives in two places.

**Answering "have I applied to this?" about the req, not the row.** `applications` carries
its own `dedupe_key`, derived from the URL you applied at and following it exactly —
absent leaves it, `""` clears it, and a URL yielding no key stores `''` rather than leaving
yesterday's key attached to today's link. `open_postings_by_verdict` and `ranked_matches`
resolve `applied_status` through the row's own application *or* through any application
sharing its key. It is a correlated subquery and not a second join: two applications can
share a key, and a join would multiply the posting row rather than answering once.

That is also why `rank.is_available` needed no change. It already excluded on the presence
of a status, so a job board's copy of something you applied to leaves the picks for free.

**Showing one pick per req.** `rank.one_row_per_req` collapses rows sharing a key, keeping
the company-board row even when the feed row scores higher — it is the one carrying health,
identity, a description and a form, and the feed row is a pointer to it. Among rows of
equal standing the first wins, and callers pass score order. Three picks that are two jobs
is a worse answer than three picks that are three.

## The one thing still worth reporting

Two **board** rows sharing a key is a *finding*, not a duplicate: almost certainly a key
too coarse to tell two live reqs apart. `store.board_key_conflicts` returns those groups
and touches nothing; `cmd_check` logs them at WARNING.

`dedupe._is_feed` still asks "is this row redundant by construction?" rather than "is this
`api`?", and the wording stays load-bearing. A company missing from `companies.yaml` has no
`check_method`, and reading it as a feed would *hide* a real collision between live reqs
rather than report it. Running the old pass against a deliberately empty companies file
closed 795 Databricks rows in one go; the same misreading now costs a silence instead.

## Closure still exists, for reasons absence did not cause

`closed_reason` is NULL for the ordinary case — closed by absence from its board's fetch —
and carries a value when something else closed the row:

- `'aged_out'`: a feed announces and never retracts, so age is the only signal it has.
- `'feed_inactive'`: a snapshot board published `active: false`. It *told* us, which beats
  waiting ninety days.

`'duplicate'` is no longer written by anything, and the migration that reopens the rows
carrying it is self-draining.

**And `sync_postings` must not undo either of those.** It reopens any re-seen posting,
which is right for a board: a req that comes back is open again. But a feed still lists an
aged-out row tomorrow, so the reopen is conditional on the closure having come from
absence — a NULL `closed_reason`.

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
- **A too-coarse key now costs a wrong chip rather than a closed row.** If two live reqs
  share a key, one may render "on company page" pointing at the other, or inherit an
  "applied" flag that belongs to its twin. That is the trade the rewrite makes deliberately:
  the failure is visible on the page instead of being a row that silently vanished.
- **Simplify's careers-site links still do not bridge to their board.** The listing links at
  `employer.com/...?gh_jid=X` while the employer's own row keys as `greenhouse:slug:X`, so
  the "on company page" chip fires less often than the corpus would allow. Unchanged by the
  rewrite, and the reason not to bridge it is unchanged too.
