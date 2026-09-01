# Reading the mailbox: what the employers said back

`applications` had exactly one writer: you. A reply saying *"we'd like to schedule a
screen"* changed nothing until you remembered to type it in, and `next_action` — the one
field whose entire job is to remind you — went stale first.

This reads your job-search mailbox, ties messages to applications you already recorded,
and **proposes** what each one means. Nothing it finds changes an application until you
accept it.

```
mail    a Maildir, read-only.  Narrow deterministically, cache what matched.
inbox   the model.             Read one message, propose one stage.
accept  you.                   The only thing that writes to `applications`.
```

**`jobtracker mail` is to `inbox` exactly what `check` is to `level`.** A deterministic
pass does the I/O and caches what the model needs into `state.db`; the task is then a pure
read whose only socket is the router. That analogy settles most of the design below, and
it is why this is two commands rather than one — an unmounted mail volume cannot silently
shrink a queue that was already recorded.

## Why a local Maildir, and not IMAP

The mail is already on disk. aerc syncs it, and pointing at that directory needs no
credential, no network at read time, and no new thing that can be down. An IMAP client
would put a password in the deployment contract for a capability the filesystem already
provides.

```bash
export JOBTRACKER_MAILDIR=~/Mail/jobs
jobtracker mail
```

**Nothing writes to that directory.** Not a flag, not a rename, not a file. Your mail
client owns it, and a tracker that moved a message from `new/` to `cur/` or set a Seen
flag would be a bug you debug in aerc rather than here. The read is `keys()` +
`get_bytes()` and nothing else, and there is a test that asserts it twice — once against
the source text, once against a byte-for-byte snapshot of the directory taken before and
after a full scan.

One argument in `maildir.py` carries more weight than the rest:

```python
mailbox.Maildir(path, factory=None, create=False)
```

`create` defaults to **True**. A typo'd `$JOBTRACKER_MAILDIR` would silently create
directories inside — or beside — your mail store. It is the most dangerous default in
this subsystem, and it has its own test.

## Narrowing, before the model

Every index the narrower matches against is built from rows that already exist in
`applications`. So **a message can never be a candidate for a company you never applied
to** — that is not a rule enforced somewhere, it is the shape of the data.

First hit wins, strongest first, and which one fired is recorded and shown on the card:

| kind | signal | resolves |
|---|---|---|
| `job_url` | a URL you applied at, in the body | company **and** job |
| `job_id` | a vendor job id, in the subject or body | company **and** job |
| `company_domain` | the sender's registrable domain, read off a URL you applied at | company |
| `company_name` | the company's name in the From **display name** | company |
| `company_subject` | its name in the subject, *and* an application-shaped word | company |

Four rules inside that table, each learned rather than assumed:

- **A domain is read, never synthesized.** Guessing `stripe.com` from "Stripe" is the
  `ashby/cedar` mistake with a new coat: a plausible string is not an identity.
- **An ATS relay identifies the ATS, never the company.** `no-reply@greenhouse.io` proves
  a hosted board sent the message and proves nothing about whose. §7.2's "reachability is
  not identity", one layer up. The display name — "Stripe Recruiting" — is what carries
  the identity, so a relay resolves through the name rules or not at all.
- **Whole tokens, never substrings.** "Ramp" must not fire on "Rampart".
- **The subject is the weak signal and needs corroborating.** *"This week in tech: Stripe,
  Ramp and more"* names two employers you applied to and is about neither of your
  applications. One application-shaped word (`application`, `interview`, `candidacy`, …)
  is the corroboration, and it is still deterministic. Measured against a real newsletter,
  not theorized. A name in the **body** alone is never enough at all.

Then, inside a matched company: a long job id, else a title occurring in the message, else
— if there is only one application there — that one. Otherwise the job stays **unresolved**
and the message is a candidate anyway. A rejection from a company where you have three
applications is exactly the message you most need to see; guessing which one it is about
would put a stage on the wrong job, so the review card asks.

## The two tables

`mail_candidates` — one row per message, keyed on **`Message-ID`**. The maildir filename
is not an identity: a client renames `1234.host` to `1234.host:2,S` the moment you read
the message, so keying on it would re-propose your whole inbox every time you opened your
mail. A message whose sender omitted the header gets `synth:<digest>` — the same fallback
`manual_job_id` uses for a title that slugs to nothing.

- **`read_at IS NULL` is the queue.** Non-NULL with no proposal means "read, and it is not
  application news" — the same NULL-vs-`''` distinction `postings.description` draws
  between never-fetched and fetched-and-genuinely-empty. It is never cleared.
- **Messages the narrower rejected are not stored.** Re-narrowing is free, local and
  deterministic; storing rejections would freeze them. Not storing is what lets a message
  that arrived *before* you recorded the application become a candidate on the next scan.
- **`sent_at` is the raw Date header; `sent_on` is it normalized.** Date headers arrive in
  a dozen timezone spellings and collate wrongly as text — the `posted_at`/`posted_on`
  split again, for the same reason. An unparseable one is admitted anyway and sorted last;
  dropping mail over a bad header is failure-as-absence in the feature whose whole job is
  to catch what you missed.

`mail_proposals` — what the model made of one message, and what you did about it.
`resolution` is set, never deleted, so a dismissal sticks: the scanner skips any id
already in `mail_candidates`.

Together these are the idempotency key `application_events` deliberately lacks. That table
has no unique constraint because repeats are the point there (`interview ×3`), so anything
ingesting an external stream nightly has to carry its own — and without one, a nightly
scan writes seven identical `rejected` events.

**`state.db` now holds the text of personal mail.** It is gitignored, it lives in `./data`,
nothing ships it anywhere, and no log line or span attribute may carry a subject or a body.

## What the model may say

The fourth bounded role in DESIGN.md §8, and bounded twice: the narrower has already
decided *that* the message is about something you applied to, and this decides only *what
it means*.

```json
{"status": "applied|oa|screen|interview|offer|rejected|withdrawn|none",
 "application": "<one of the ids it was offered> | none",
 "quote": "one sentence, copied from the email"}
```

Four refusals, and the fourth is the one that matters:

1. not JSON, or not an object → no answer
2. a status outside the enum → no answer
3. an application id it was not offered → no answer
4. **a quote that does not appear in the message, verbatim** → no answer

The quote is the only free text the model produces here, so it is *grounded*: it must
occur in the message it was shown. That is `repair`'s rule that a proposed slug must
appear on the page it was read from, and it is what keeps a fabricated rejection off the
review list.

**`status: "none"` is an answer, not a failure**, and it is written — `read_at` is set and
the message is never asked about again. This diverges from `level`, where `unclear`
returns nothing and is retried three times, and the divergence is deliberate: copying it
would spend three model calls on every newsletter that squeaked through the narrower, and
would fill the blocked-unit count — which exists to signal breakage — with perfectly
healthy readings. Nothing else will ever resolve a message that is simply not about an
application. Only a transport failure returns nothing and leaves the candidate unread.

`unit_key` is the **message id**, because the message *is* the question. It also fixes a
real collision the obvious mapping would ship: two ambiguous messages at one company both
carry `ats_job_id=''`, so without it they share an `ident`, `task_attempts` charges one's
failures to the other, and the router collapses two distinct questions onto one answer.

Priority is **40**, last. `level → judge` is a dependency chain and this is not in it —
it consumes nothing they produce. The honest reason it goes last is starvation:
the mail queue refills from an external stream on a schedule nothing here controls, and
ahead of `level` a chatty inbox would keep the pipeline's own stages permanently waiting.

## Reviewing, and accepting

The `/applications` page grows a section **above** the add form — work the system is asking
you to confirm outranks a blank form. Each card carries the proposed stage, the company,
the sender, the subject, the grounded quote, a snippet, and — when the job is unresolved —
a dropdown of that company's applications.

- **Accept** is the only path from mail into `applications`. It calls
  `advance_application`, so the stage moves *and* the history gains exactly one event.
- **The event note is composed by Python**, from the subject and the date. The model's
  quote stays in `mail_proposals.evidence` as the evidence on the card and never reaches
  an application. DESIGN.md §8.4's rule that no sentence the model composed reaches a
  field, with your own history as the field.
- **A refused accept writes nothing** — missing id, unknown proposal, already resolved, an
  unresolved job accepted without choosing one, a chosen id with no application row. All
  `{"ok": false}` at HTTP 200, both tables untouched.
- **Not this** marks the row dismissed. It is never deleted, because deleting it is what
  would let the next scan propose the same message all over again.

**There is no scan endpoint.** Walking a mailbox on a single-threaded `HTTPServer` would
block every other request for the length of the walk, and it would make a page render a
writer. There is a test asserting `server.py` imports neither `maildir` nor `mailbox`.

### The banner, and why there is no "seen" flag

The dashboard shows a count of pending proposals and nothing when there are none. It is
**derived**, not stored. A seen flag would have to be written by a `GET`, and rendering a
page must not mutate what it renders (`test_the_page_is_a_pure_read`); a flag written by
JS would never be written at all with JS off.

So the acknowledgement is accepting or dismissing, not glancing. A proposal you looked at
and ignored is still pending — which is the correct behaviour for the one feature whose
job is to catch what you missed. The static dashboard shows the count and names
`jobtracker mail`; only the served page links to `/applications`, because a `file://` page
pointing at a server that may not be running is worse than no link.

## Running it

```bash
jobtracker mail                          # scan, narrow, record. No model.
jobtracker work --task inbox             # read the candidates. The model.
jobtracker mail --list                   # pending proposals; reads no mail
jobtracker mail --accept '<id>' [--job ATS_JOB_ID]
jobtracker mail --dismiss '<id>'
```

`--list`, `--accept` and `--dismiss` never touch the maildir, so the whole loop works from
a machine with no mail on it — the terminal-mirror rule `jobtracker applications` follows.

Exit codes:

- **0** — scanned. Zero candidates is a healthy Tuesday.
- **1** — cannot run: no maildir configured, the path is missing, or it is not a Maildir.
  Printing "0 messages" for an unmounted volume is the `greenhouse/hubspot` failure with a
  filesystem instead of a board.
- **2** — degraded: at least one message could not be read or parsed. A failed read must
  never contribute to "no new mail today".

In a container, mount the maildir **read-only** (`:ro`) — belt to the code's braces — and
set `TZ`, because `sent_on` is a normalized day and a UTC container at 21:00 local stamps
tomorrow onto it.

## Known blind spots

Stated rather than hidden, as the rest of this repo does:

- **Mail whose identity is in an image.** A rendered-image newsletter or a heavily
  templated recruiter mail with no text part gives the narrower nothing to match.
- **A company that recruits from a domain you never applied at.** Domains are read off
  your own applications and off `careers_page`; an agency or a subsidiary sending from
  somewhere else resolves only if its name is in the display name or subject.
- **A forwarded thread** where the identity lives in the quoted part below the fold.
- **A company name containing a comma** would split the `seen_on` list — the same
  pre-existing schema limitation the gap classifier documents.
