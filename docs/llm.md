# The ambiguity pass (local LLM)

About 1,500 open postings sit in `uncertain`: their titles carry no level token, so the
deterministic matcher cannot honestly say yes or no. `"Backend Software Engineer"` might
be a new-grad role or might want eight years. The answer is in the job description,
which the nightly sweep does not read.

This pass reads it. It is **entirely optional** and **entirely local**, and it is now
the `level` task in the queue described in docs/tasks.md — same scope, same
guarantees, one commit per posting instead of one per run.

## Scope: level extraction, nothing else

The model answers exactly one question — *what experience level does this description
require* — and returns `entry`, `not_entry`, or `unclear`.

It does **not** decide whether a role is backend, whether the company is interesting, or
whether you want it. All of that stays in `criteria.yaml` where you can read it, diff
it, and test it against the corpus.

```
UNCERTAIN + engineering-looking title
   └─▶ description  (already cached by `check` — this pass opens no ATS connection)
        └─▶ local model: entry | not_entry | unclear
             ├─ entry     → re-apply the RULES' engineering gate → match or reject
             ├─ not_entry → reject
             └─ unclear   → stays uncertain
```

An `entry` reading does not by itself produce a match. It still has to pass the same
engineering gate `match.py` applies to a title carrying an explicit level token — so the
`Finance Associate` guard holds against an LLM verdict exactly as it does against a rule
one. The model supplies a missing fact; the criteria decide what to do with it.

Verdicts from this path are stored with `decided_by='llm'`, so they are always
distinguishable from rules verdicts, and the reason carries the evidence:

```
Asana    Backend Software Engineer     reject   llm:not_entry:3+ years
Discord  Software Engineer, Notifications  reject   llm:not_entry:Senior
```

## Failure is absence, never a wrong answer

No router configured, connection refused, timeout, malformed response, an unroutable
model, low confidence — **every one of these leaves the posting `uncertain`**, which is
where it already was.

That is the whole safety argument. The pass can only ever add resolution; it cannot
subtract correctness. Matching must not depend on an inference server being up, so
nothing here raises for an unreachable host.

With nothing configured, `work` reports what it *would* do and changes nothing:

```
$ jobtracker work
Task queue, in the order the scheduler considers it:

   10  level      674 pending
                  read descriptions to settle UNCERTAIN postings
   ...

No inference router configured — nothing was changed.
  Point at one with --llm-url http://HOST:PORT
  (or $JOBTRACKER_LLM_URL / $SIR_BASE_URL)
```

## Running it

```sh
jobtracker work --task level --llm-url http://192.168.1.50:8000
jobtracker work --task level --llm-url http://192.168.1.50:8000 --budget 50
jobtracker resolve --limit 50                    # the same thing, older spelling
```

Or via environment, following the same convention as `--telemetry`:

```sh
export JOBTRACKER_LLM_URL=http://192.168.1.50:8000
```

`$SIR_BASE_URL` and `$SIR_ENDPOINTS` — the SDK's own variables — are honoured too, so a
machine that already points other services at the router does not have to repeat itself.
`$SIR_ENDPOINTS` alone is enough: it routes per model rather than naming one address.

**There is no `--llm-provider` any more** (removed 2026-08-13). There is one transport,
and the router is the thing that knows which backend serves what. The configuration is
an address.

**Which model is out of scope.** Point it at an address; the client asks the router what
it is serving via `/v1/models` and uses that. `--llm-model` overrides if several are
routed.

`probe()` runs first and contacts the server *even when a model name is configured* —
short-circuiting there would report "ready" against a switched-off box, and the failure
would then surface as one silent miss per posting for the rest of the run.

## Why only 674 of 1,537

The queue is scoped to postings whose title carries some engineering signal. The other
863 are `Field Marketer`, `Talent Strategist`, `Data Analyst` — reading their
descriptions spends a rate-limited ATS request to learn what the title already said.

The cost is real and worth stating: a title with no matching token is genuinely
engineering and never read. It stays `uncertain` and visible in the queue for a human
rather than being silently rejected — the same tradeoff this project makes everywhere:
surface the blind spot, do not hide it. There is a test asserting the scope stays lossy
rather than regressing into a reject.

> **Correction (2026-08-02).** The example this section used for years —
> **"Member of Technical Staff"** — does not actually behave this way. `staff` is in
> `exclude_titles`, so `match()` rejects the title outright and `resolve` never sees it.
> The test only asserts `looks_engineering()` is False, which is true and a different
> claim. The blind spot itself is real; the illustration was wrong. Fixing it means
> deciding whether that title should be reachable, which is a `criteria.yaml` change and
> so belongs in the tuning loop behind `jobtracker eval` — not a bare YAML edit.

## Where descriptions come from

**Not from this pass.** Since 2026-08-02 `check` caches a description for every posting
whose verdict is `match` or `uncertain`, so the `level` task is a pure read of `state.db`
and the only socket it opens is to the router. It lost its `fetcher`, `store_mod`, and
`conn` parameters along with the lazy fetch path.

That matters beyond tidiness: a throttled board can no longer shrink the queue this pass
considers, and what is available to read no longer depends on which postings some earlier
run happened to visit.

It also changed what "pending" means, for the better. A posting with no cached
description used to be counted as considered-and-skipped; now it is simply **not queued**,
because with no fetching in this pass it is work that cannot be done rather than work
waiting its turn. The old shape overstated the backlog every night and sent a `--limit`
run to postings guaranteed to be no-ops.

| ATS | Cost to `check` |
|---|---|
| Ashby (13 boards) | **Free** — `descriptionPlain` is in the bulk payload |
| Lever (2 boards) | **Free** — `descriptionPlain` plus the requirement `lists` |
| Greenhouse (47 boards) | One request per posting, bounded by `--max-descriptions` |

Greenhouse is the exception because the bulk call deliberately drops `?content=true` —
full-content payloads blow the 20-second timeout on large boards (Databricks carries
~790 reqs). Its descriptions are fetched one at a time and cached in
`postings.description`, so a posting is fetched once ever. `NULL` means never fetched;
`''` means fetched and genuinely empty, and only `NULL` is retried.

Those fetches go through `Fetcher._request_json`, so they inherit the per-host rate
limiter, the retry policy, and the trace shape. **The ATS is the scarce resource here,
not the model** — a local GPU is not rate-limited, `boards-api.greenhouse.io` very much
is. Measured at ~0.6s per fetch, so the default 400-per-run budget is ~4 minutes and the
steady state is ~30-40 fetches a night.

One Greenhouse quirk worth knowing: `content` is HTML-escaped *inside* a JSON string, so
it arrives as `&lt;h2&gt;Who we are&lt;/h2&gt;`. Unescaping has to happen before
tag-stripping, or the model is handed a wall of `&lt;p&gt;`.

## The transport

`jobtracker/llm/` is two files:

| File | Role |
|---|---|
| `llm/wire.py` | The request body and how to read the answer out. Pure. |
| `llm/client.py` | The **only** module that opens a socket. |

Transport is the **`sir-client` SDK** from the inference router (`sir`), as of
2026-08-13. It is not on PyPI:

```sh
pip install -e ../stupid-inference-router/clients/python
# or, for a container:
pip install "git+ssh://git@github.com/ida314/stupid--inference-router.git#subdirectory=clients/python"
```

### Why the `Provider` registry was deleted, not kept

There used to be a `Provider` interface and a `_REGISTRY`, mirroring `sources/`, so a
second wire format could be slotted in without touching the client. The router *is* that
indirection now: it presents one OpenAI-compatible endpoint and decides which model is
resident. Keeping a second dispatch layer in front of one that already exists would have
been two abstractions doing one job.

What survived is the split that actually mattered — `wire.py` is pure and knows the body
shape, `client.py` owns every socket. That is deliberate on the router's side too: it
reads only `model` and `stream` and forwards the rest **exactly as written**, because the
extras backends accept (`guided_json` against `json_schema`, `ebnf` against
`guided_grammar`) drift every release and a translator in the middle would have to be
updated in lockstep with all of them. So the knowledge of what the backend accepts stays
in the service that already has it, which is this one.

There is still no API-key handling anywhere in this package, and there should not be.

### Async, and what that forced

The SDK is async-only — a sync wrapper you cannot call from inside a loop is a footgun.
So `tasks/runner.py` is async, which is also what let units run concurrently through the
router. `browser.py` is Playwright's *sync* API, which must **not** run inside an asyncio
loop; that is why the two are separate modules and why `serve` drives the browser on a
plain daemon thread.

The SDK also submits asynchronously (`Prefer: respond-async`) and polls a job, so a
request that waits minutes behind a model swap does not sit on an HTTP connection long
enough to meet every idle timeout between here and the GPU. None of that is visible above
`client.complete()`.

### Failure is still absence

Every SDK error — `ModelNotRouted`, `TransportError`, `JobLost`, `JobFailed`,
`JobCancelled`, `RequestTimeout` — plus any raw `httpx` exception, returns `None`. They
are all handled identically because they all mean "no answer", never "wrong answer".
Nothing here raises for a router that is down.

## Why `response_format`, and why it is still validated

- **Constrained decoding.** `response_format` restricts sampling so the server emits text
  conforming to the schema. Malformed output stops being a failure mode to parse around.
  The client validates anyway: a server that silently ignored the schema would otherwise
  let free-form prose through as a verdict.

  That validation earned its keep on 2026-07-24. The request used vLLM's older
  `guided_json` + `guided_decoding_backend` pair, both since dropped from the request
  schema. vLLM 0.23 does not reject a body carrying them — it **accepts the request,
  ignores the keys, and answers in prose**. Every response then failed the parser, so
  every posting stayed `uncertain`: the pass ran, spent a description fetch per posting,
  and resolved nothing. Failure-is-absence meant this cost accuracy nothing, which is
  also why nothing surfaced it — a no-op pass and a pass with no work to do look
  identical from the outside. If a task reports zero applied against a router that is
  demonstrably up, suspect the wire format before the model.

  **Routing through `sir` does not make this safer.** It forwards the body untouched, so
  a backend that ignores the key still answers in prose and the parsers are still the
  only thing between that and a fabricated verdict. Demonstrated on 2026-08-13 against
  the router's mock backend, which ignores the schema entirely: every prefill
  question-match came back unparseable and every field became a gap. Exactly right —
  no answer rather than a wrong one. **Check it against the server you actually run.**

- `temperature: 0`, so the same posting classifies the same way on a rerun. Otherwise
  `eval` scores noise instead of the model.

## Checking its work

The tuning corpus doubles as a scoring harness. Judge some postings by hand, run the
pass, then diff — no network, no re-fetching:

```sh
jobtracker eval
```

Verdicts the model produced that contradict yours show up as regressions like any other.
