# The review queue: which lane an uncertain row is in

An uncertain posting is one the rules could not decide. On the live corpus most of them are
not engineering at all — 2,227 of 3,176 on 2026-11-05 had no field word in the title — and
the decision was to **fold** that part, not reject it. It stays on the list as a short set
of possible misses, sorted after everything more promising.

`jobtracker/queue.py` is where that split is made, and the only place:

```
lane      queue.lane(row, criteria, companies_by_name, coverage_by_origin, reading, targets)
label     queue.label(lane, coverage_band)       the words a page shows
order     queue.sort_key(row, lane, criteria)    (lane, location_rank, newest first)
rows      store.uncertain_with_context(conn)     open uncertain rows + origin, employer,
                                                 first_seen and the level reading
```

## The five lanes, in queue order

| Lane | When | Label |
|---|---|---|
| `feed` | field signal, and the row's job board declares a band in `targets` | listed on a new-grad feed |
| `read` | field signal, and `level` read the prose and found no level (`unclear`) | read — the description does not say |
| `signal` | field signal, not read yet | not read yet |
| `no_signal` | no field signal — **folded** | no field signal in the title |
| `noise` | no field signal on a `board_scope: mixed` company — folded inside the fold | board lists many non-field roles |

The order of the checks is the definition:

1. **Field signal first, and it is `level.looks_engineering` and nothing else** — the same
   test that scopes the `level` queue, so "not read yet" never names a row `level` would not
   read. No signal is `no_signal`, or `noise` when the company's curation says the board is
   mixed.
2. With a signal, a **job board curated to a band you target** wins over anything the model
   said: Simplify's listing is a new-grad list, and that is a stronger statement than "the
   description does not say". The band comes from `Plugin.coverage()["bands"]`, keyed by
   `postings.origin`; the wording comes from `queue.BAND_WORDS`, the one table that turns a
   band name into words.
3. Then an `unclear` reading, then the plain signal.

`targets` defaults to `("entry",)` and is a parameter, never read from config inside the
module, so preferences can feed it without `queue.py` knowing they exist. With
`targets=("mid",)` a Simplify row is `signal`.

## Rules

- **`queue.lane` is the single derivation.** `/review`, the dashboard tables, the Today
  count and the folds all call it. Two copies are how two pages come to disagree about what
  is on a list, and a test greps the package for the label strings.
- **A lane is derived when a page renders, and is never stored.** It reads the title, the
  criteria, the curation and the reading as they are now; a stored lane would be a snapshot
  of all four that nothing invalidates.
- **Folding is display, never a verdict.** No lane moves a posting out of `uncertain`, and
  nothing in `queue.py` writes. A folded row is still on the list.
- **`board_scope` is read here only.** It folds; it never gates `match()`, health or a fetch.
- **Pure.** No SQLite, no files, no clock (`test_lane_is_pure`). The caller builds
  `companies_by_name` from `load_companies` and `coverage_by_origin` from the import
  plugins; a plugin's group is never curation, so the lookup is `.get` and its absence reads
  as an ordinary board.
- **An unreadable `first_seen` sorts last** within its lane and location, never as today.

## The dashboard's Uncertain tables

The company pages and job boards each keep their Uncertain table as the full record of the
list. Two things changed on both, identically in the static file and under `serve`:

- **Status, not "Why uncertain".** The rules' reason is `no_level_token_in_title` on nearly
  every row, so as text it said nothing. The cell shows `queue.label(lane)` — the feed band's
  wording through `queue.feed_band` — and keeps the raw reason in its `title`, one hover away.
- **A trailing fold.** `no_signal` and `noise` rows render in one last group, "No field
  signal — N rows", `noise` after `no_signal`. It is a `tbody.grp` like a company's: rendered
  open, collapsed by the script on load, force-expanded by any active filter. On the company
  pages a folded row carries its company as a `.cobadge`, because no company head sits above
  it.

Nothing is removed and the partition does not move. The heading count, "N of M shown" and the
**Uncertain** tile are the whole list; the tile's sub-line reads "M worth a read" — lanes
`feed`, `read` and `signal`, which is `dashboard._WORTH_A_READ`, the complement of
`queue.FOLDED`. The lanes come from `dashboard._uncertain_lanes`, which feeds
`store.uncertain_with_context` rows to `queue.lane` (each row is its own reading) and takes
coverage from the import plugins. Without criteria there is no field signal to read, so the
raw reason shows and nothing folds.
