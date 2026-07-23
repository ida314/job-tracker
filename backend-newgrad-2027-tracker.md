# Backend New-Grad 2027 — Target & Tracker

> A machine-parseable target list for automated daily job-checking. An agent (Claude Code
> or similar) reads this file, checks each company for matching openings, and updates the
> `status` / `last_checked` / `last_posting_seen` fields in place.
>
> **How companies are checked:** where possible via each company's public ATS JSON API
> (Greenhouse / Lever / Ashby), which returns every open role in one keyless request.
> Companies on Workday or bespoke portals can't be reliably auto-checked and are marked
> `check_method: manual` — the agent should flag these for the user, not silently skip them.

---

## Runbook — instructions for the checking agent

Run this once per day. For each company entry below:

1. **If `check_method: api`** — fetch `board_url` and parse the JSON.
   - Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`
     → roles are in `.jobs[]`, title in `.title`, location in `.location.name`, link in `.absolute_url`, posted date in `.updated_at`.
   - Lever: `https://api.lever.co/v0/postings/{slug}?mode=json`
     → array of postings, title in `.text`, location in `.categories.location`, link in `.hostedUrl`, date in `.createdAt`.
   - Ashby: `POST https://api.ashbyhq.com/posting-api/job-board/{slug}` (or the public GraphQL board endpoint)
     → postings with `.title`, `.locationName`, `.jobUrl`, `.publishedAt`.
2. **Apply the Match Criteria** (below) to every returned role. A role matches if title/level pass the include rules AND don't hit `exclude_titles`.
3. **Diff against `last_posting_seen`** for that company. Only report roles not already recorded.
4. **If `check_method: manual`** — do NOT attempt to scrape. Add the company to a "check these by hand" list in the daily report, at most once per week per company (use `last_checked` to rate-limit).
5. **If `check_method: aggregator`** — fetch the source and diff its contents; these are highest-yield for new-grad specifically.
6. **Update in place:** set `last_checked` to today's date for every company checked. When a matching role is found, append its title+date to `last_posting_seen` and set `status: open`.
7. **Report format:** output only what changed today — new matching roles (company, title, location, apply link) grouped by tier, plus the manual-check list. Don't re-list unchanged companies.

**On failure:** if an `api` fetch returns empty or errors, the company most likely migrated ATS. Note it in the report as `ATS-check-failed` so the user can update the slug — do not treat empty as "no jobs."

---

## Match Criteria

```yaml
role_type_include: [backend, full-stack, fullstack, platform, infrastructure, distributed systems, systems, API, server, data engineering, site reliability, SRE]
level_include: [new grad, new graduate, entry level, entry-level, associate, university grad, early career, "2027", "L3", "E3", "SDE I, "SWE I", "Software Engineer I", junior, grad]
exclude_titles: [senior, sr., staff, principal, lead, manager, director, head of, vp, intern, internship, ii, iii, "2026 grad", contractor, "5+ years", "3+ years"]
graduation_eligibility: 2027 or open "new grad" (exclude roles explicitly requiring 2025/2026 graduation)
locations_preferred: [New York, NYC, Remote US, Remote, Hybrid NYC]
locations_acceptable: [Boston, Austin, Seattle, San Francisco, Bay Area, Chicago, Atlanta, Washington DC]
citizenship_note: exclude roles requiring active security clearance unless user updates this
keywords_bonus: [Go, Golang, Rust, Java, Kotlin, distributed systems, Kafka, gRPC, microservices, PostgreSQL, databases, Kubernetes]
```

> Priority ordering reflects the user's stated goals: **skill/career growth over pay or
> prestige**, **backend primary**. Tiers weight match + safety buckets heavily; reach tier
> is applied-to but not anchored on.

---

## Tier 1 — Backend & infra scale-ups  *(primary target: real distributed-systems work early)*

### Stripe
- ats: greenhouse
- slug: stripe
- board_url: https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true
- careers_page: https://stripe.com/jobs/search
- category: backend-scaleup
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: New-grad roles typically open late summer/fall. High bar; apply early.

### Databricks
- ats: greenhouse
- slug: databricks
- board_url: https://boards-api.greenhouse.io/v1/boards/databricks/jobs?content=true
- careers_page: https://www.databricks.com/company/careers
- category: data-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Confirmed ~755 open reqs mid-2026. Strong data/backend learning.

### Ramp
- ats: ashby
- slug: ramp
- board_url: https://api.ashbyhq.com/posting-api/job-board/ramp
- careers_page: https://ramp.com/careers
- category: fintech-scaleup
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Ashby AI-scores resumes — keep resume clean/scannable, skills listed explicitly.

### Datadog
- ats: greenhouse
- slug: datadog
- board_url: https://boards-api.greenhouse.io/v1/boards/datadog/jobs?content=true
- careers_page: https://careers.datadoghq.com
- category: observability-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Heavy backend/data-pipeline work. Known for structured eng.

### Confluent
- ats: ashby
- slug: confluent
- board_url: https://api.ashbyhq.com/posting-api/job-board/confluent
- careers_page: https://www.confluent.io/careers
- category: infra-devtools
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Kafka company — excellent distributed-systems learning.

### MongoDB
- ats: greenhouse
- slug: mongodb
- board_url: https://boards-api.greenhouse.io/v1/boards/mongodb/jobs?content=true
- careers_page: https://www.mongodb.com/careers
- category: database-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Database internals exposure. NYC HQ — good for NYC preference.

### Cloudflare
- ats: greenhouse
- slug: cloudflare
- board_url: https://boards-api.greenhouse.io/v1/boards/cloudflare/jobs?content=true
- careers_page: https://www.cloudflare.com/careers
- category: infra-networking
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Systems/networking depth. Runs a defined new-grad program most years.

### Airtable
- ats: greenhouse
- slug: airtable
- board_url: https://boards-api.greenhouse.io/v1/boards/airtable/jobs?content=true
- careers_page: https://airtable.com/careers
- category: saas-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Notion
- ats: ashby
- slug: notion
- board_url: https://api.ashbyhq.com/posting-api/job-board/notion
- careers_page: https://www.notion.so/careers
- category: saas-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Figma
- ats: greenhouse
- slug: figma
- board_url: https://boards-api.greenhouse.io/v1/boards/figma/jobs?content=true
- careers_page: https://www.figma.com/careers
- category: saas-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Retool
- ats: gem
- slug: retool
- board_url: https://jobs.gem.com/retool
- careers_page: https://retool.com/careers
- category: devtools
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-09 — not on Greenhouse (404). Real board is Gem (jobs.gem.com/retool). Gem exposes no keyless public JSON API — every path returns the SPA shell — so this is manual until a feed is found.

### Brex
- ats: greenhouse
- slug: brex
- board_url: https://boards-api.greenhouse.io/v1/boards/brex/jobs?content=true
- careers_page: https://www.brex.com/careers
- category: fintech-scaleup
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Plaid
- ats: ashby
- slug: plaid
- board_url: https://api.ashbyhq.com/posting-api/job-board/plaid
- careers_page: https://plaid.com/careers
- category: fintech-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-09 — migrated Greenhouse → Ashby. Slug `plaid` on Ashby returns 106 roles. A stale empty Lever board (lever/plaid) also exists; ignore it.

### Rippling
- ats: bespoke
- slug:
- board_url:
- careers_page: https://www.rippling.com/careers/open-roles
- category: saas-backend
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-09 — not on Greenhouse/Lever/Ashby (all 404). Rippling runs its own ATS product; ats.rippling.com/rippling/jobs 307-redirects to their site. No public JSON endpoint found.

### Mercury
- ats: greenhouse
- slug: mercury
- board_url: https://boards-api.greenhouse.io/v1/boards/mercury/jobs?content=true
- careers_page: https://mercury.com/jobs
- category: fintech-scaleup
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-09 — migrated Ashby → Greenhouse. The old Ashby board still resolves but returns zero jobs, so it read as "no openings" rather than an error. Greenhouse board returns 57 roles.

### Modern Treasury
- ats: ashby
- slug: moderntreasury
- board_url: https://api.ashbyhq.com/posting-api/job-board/moderntreasury
- careers_page: https://www.moderntreasury.com/careers
- category: fintech-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Payments-rails backend. Confirm slug.

### Temporal
- ats: ashby
- slug: temporal
- board_url: https://api.ashbyhq.com/posting-api/job-board/temporal
- careers_page: https://temporal.io/careers
- category: infra-devtools
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Durable-execution/distributed-systems. Confirm ATS+slug.

### Cockroach Labs
- ats: greenhouse
- slug: cockroachlabs
- board_url: https://boards-api.greenhouse.io/v1/boards/cockroachlabs/jobs?content=true
- careers_page: https://www.cockroachlabs.com/careers
- category: database-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Distributed SQL database internals. NYC HQ. Confirm slug on first run.

### Fivetran
- ats: greenhouse
- slug: fivetran
- board_url: https://boards-api.greenhouse.io/v1/boards/fivetran/jobs?content=true
- careers_page: https://www.fivetran.com/careers
- category: data-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### dbt Labs
- ats: greenhouse
- slug: dbtlabsinc
- board_url: https://boards-api.greenhouse.io/v1/boards/dbtlabsinc/jobs?content=true
- careers_page: https://www.getdbt.com/about-us/careers
- category: data-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-09 — slug was wrong (`dbtlabs` 404s). Correct slug is `dbtlabsinc`, board name "dbt Labs". Board is valid but currently returns 0 open reqs — that's a real empty board, not a broken slug.

### Vercel
- ats: greenhouse
- slug: vercel
- board_url: https://boards-api.greenhouse.io/v1/boards/vercel/jobs?content=true
- careers_page: https://vercel.com/careers
- category: devtools-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-09 — migrated Ashby → Greenhouse. Old Ashby board still resolves with zero jobs. Greenhouse board returns 67 roles.

### Linear
- ats: ashby
- slug: linear
- board_url: https://api.ashbyhq.com/posting-api/job-board/linear
- careers_page: https://linear.app/careers
- category: devtools
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Small team, high bar. Backend roles rare but high-quality.

### Snowflake
- ats: unknown
- slug:
- board_url:
- careers_page: https://careers.snowflake.com
- category: data-infra
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Likely Workday/bespoke — verify. Strong data-platform learning if reachable.

### HashiCorp
- ats: bespoke
- slug:
- board_url:
- careers_page: https://www.hashicorp.com/en/careers/open-positions
- category: infra-devtools
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-09 — the predicted IBM migration happened. Gone from Greenhouse, Lever and Ashby (all 404). Careers page is now powered by IBM Careers. Manual until/unless an IBM feed is wired up.

### Samsara
- ats: greenhouse
- slug: samsara
- board_url: https://boards-api.greenhouse.io/v1/boards/samsara/jobs?content=true
- careers_page: https://www.samsara.com/company/careers
- category: iot-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Flexport
- ats: greenhouse
- slug: flexport
- board_url: https://boards-api.greenhouse.io/v1/boards/flexport/jobs?content=true
- careers_page: https://www.flexport.com/careers
- category: logistics-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

---

## Tier 2 — Infrastructure / developer tools  *(backend IS the product)*

### Elastic
- ats: greenhouse
- slug: elastic
- board_url: https://boards-api.greenhouse.io/v1/boards/elastic/jobs?content=true
- careers_page: https://www.elastic.co/careers
- category: infra-search
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Twilio
- ats: greenhouse
- slug: twilio
- board_url: https://boards-api.greenhouse.io/v1/boards/twilio/jobs?content=true
- careers_page: https://www.twilio.com/en-us/company/jobs
- category: infra-comms
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Confirm slug on first run.

### Sentry
- ats: ashby
- slug: sentry
- board_url: https://api.ashbyhq.com/posting-api/job-board/sentry
- careers_page: https://sentry.io/careers
- category: observability-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-09 — migrated Greenhouse → Ashby (slug unchanged).

### Grafana Labs
- ats: greenhouse
- slug: grafanalabs
- board_url: https://boards-api.greenhouse.io/v1/boards/grafanalabs/jobs?content=true
- careers_page: https://grafana.com/about/careers
- category: observability-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Confirm slug. Remote-heavy.

### LaunchDarkly
- ats: greenhouse
- slug: launchdarkly
- board_url: https://boards-api.greenhouse.io/v1/boards/launchdarkly/jobs?content=true
- careers_page: https://launchdarkly.com/careers
- category: devtools-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Postman
- ats: greenhouse
- slug: postman
- board_url: https://boards-api.greenhouse.io/v1/boards/postman/jobs?content=true
- careers_page: https://www.postman.com/company/careers
- category: devtools
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Airbyte
- ats: ashby
- slug: airbyte
- board_url: https://api.ashbyhq.com/posting-api/job-board/airbyte
- careers_page: https://airbyte.com/company/careers
- category: data-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-09 — migrated Greenhouse → Ashby (slug unchanged). Small board (~13 roles).

### Okta / Auth0
- ats: greenhouse
- slug: okta
- board_url: https://boards-api.greenhouse.io/v1/boards/okta/jobs?content=true
- careers_page: https://www.okta.com/company/careers
- category: infra-identity
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Identity/auth backend. Confirm slug.

### Vanta
- ats: ashby
- slug: vanta
- board_url: https://api.ashbyhq.com/posting-api/job-board/vanta
- careers_page: https://www.vanta.com/careers
- category: security-saas
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Redis
- ats: ashby
- slug: redis
- board_url: https://api.ashbyhq.com/posting-api/job-board/redis
- careers_page: https://redis.io/company/careers/
- category: database-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-09 — resolved: migrated Greenhouse → Ashby, and the slug is `redis`, not `redislabs`. Returns 58 roles.

### Kong
- ats: ashby
- slug: kong
- board_url: https://api.ashbyhq.com/posting-api/job-board/kong
- careers_page: https://konghq.com/company/careers
- category: infra-apigateway
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: API gateway. 2026-07-09 — resolved: migrated Greenhouse → Ashby, slug is `kong`, not `konginc`.

### Coralogix
- ats: bespoke
- slug:
- board_url:
- careers_page: https://coralogix.com/careers/
- category: observability-infra
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-22 — added. Runs Comeet; greenhouse/lever/ashby `coralogix` all 404. Comeet's careers-api needs a company uid plus a page-embedded token, so there is no keyless JSON board — manual by rule. Israeli HQ; US roles are Boston-area and remote, so check locations by hand.

---

## Tier 3 — Regulated industry (fintech / healthcare / insurance)  *(underrated: correctness + scale, less competition)*

> Most large banks use Workday or bespoke portals → `check_method: manual`. These are still
> high-value for backend fundamentals; the agent flags them weekly rather than scraping.

### Bloomberg
- ats: bespoke
- slug:
- board_url:
- careers_page: https://careers.bloomberg.com
- category: fintech-eng
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Excellent eng culture, strong new-grad program, NYC. Check the campus/new-grad section directly.

### Capital One
- ats: workday
- slug:
- board_url:
- careers_page: https://www.capitalonecareers.com
- category: fintech-eng
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Large structured new-grad TDP (Technology Development Program). Real backend/AWS learning.

### JPMorgan Chase
- ats: workday
- slug:
- board_url:
- careers_page: https://careers.jpmorgan.com
- category: fintech-eng
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Software Engineer Program (SEP) — big new-grad cohort. Apply early, fills fast.

### Goldman Sachs
- ats: bespoke
- slug:
- board_url:
- careers_page: https://www.goldmansachs.com/careers
- category: fintech-eng
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Engineering new-analyst program. NYC. Very early deadlines.

### Two Sigma
- ats: bespoke
- slug:
- board_url:
- careers_page: https://careers.twosigma.com
- category: quant-eng
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: NYC. Strong systems work. 2026-07-09 — not on Greenhouse/Lever/Ashby (all 404); own portal at careers.twosigma.com.

### Jane Street
- ats: bespoke
- slug:
- board_url:
- careers_page: https://www.janestreet.com/join-jane-street
- category: quant-eng
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: OCaml, but elite training. NYC. Own application portal.

### Fidelity
- ats: workday
- slug:
- board_url:
- careers_page: https://jobs.fidelity.com
- category: fintech-eng
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:

### Epic Systems
- ats: bespoke
- slug:
- board_url:
- careers_page: https://careers.epic.com
- category: healthtech-eng
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Wisconsin (relocation). Large new-grad SWE hirer, strong training.

### Oscar Health
- ats: greenhouse
- slug: oscar
- board_url: https://boards-api.greenhouse.io/v1/boards/oscar/jobs?content=true
- careers_page: https://www.hioscar.com/careers
- category: healthtech-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: NYC. Confirm slug.

### Cedar
- ats: greenhouse
- slug: careportalinc
- board_url: https://boards-api.greenhouse.io/v1/boards/careportalinc/jobs?content=true
- careers_page: https://www.cedar.com/careers/open-roles
- category: healthtech-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: NYC healthtech. 2026-07-09 — real slug is the legal entity `careportalinc` (board name "Cedar", 15 roles), read from the Greenhouse embed on their open-roles page. WARNING — do NOT use `cedar`: `ashby/cedar` is a live board belonging to an unrelated mortgage/real-estate Cedar and would silently feed wrong roles into this tracker.

### SoFi
- ats: greenhouse
- slug: sofi
- board_url: https://boards-api.greenhouse.io/v1/boards/sofi/jobs?content=true
- careers_page: https://www.sofi.com/careers
- category: fintech-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Affirm
- ats: greenhouse
- slug: affirm
- board_url: https://boards-api.greenhouse.io/v1/boards/affirm/jobs?content=true
- careers_page: https://www.affirm.com/careers
- category: fintech-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Chime
- ats: greenhouse
- slug: chime
- board_url: https://boards-api.greenhouse.io/v1/boards/chime/jobs?content=true
- careers_page: https://careers.chime.com
- category: fintech-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Betterment
- ats: greenhouse
- slug: betterment
- board_url: https://boards-api.greenhouse.io/v1/boards/betterment/jobs?content=true
- careers_page: https://www.betterment.com/careers
- category: fintech-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: NYC.

### Root Insurance
- ats: greenhouse
- slug: root
- board_url: https://boards-api.greenhouse.io/v1/boards/root/jobs?content=true
- careers_page: https://inc.joinroot.com/careers/
- category: insurtech-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-09 — slug was wrong (`rootinsurance` 404s). Correct slug is `root`, board name "Root". Board is valid but currently returns 0 open reqs — a real empty board, not a broken slug.

---

## Tier 4 — Big Tech  *(training + brand; apply, don't over-anchor)*

> All bespoke/custom portals → `check_method: manual`. Large new-grad backend cohorts with
> structured onboarding. Deadlines cluster Aug–Oct; some roll year-round.

### Amazon
- ats: bespoke
- careers_page: https://www.amazon.jobs/en/teams/internships-for-students
- category: bigtech
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: SDE I new-grad, huge cohort, structured onboarding. Applies year-round.

### Microsoft
- ats: bespoke
- careers_page: https://careers.microsoft.com/students
- category: bigtech
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: New-grad SWE, strong backend/cloud (Azure) training.

### Google
- ats: bespoke
- careers_page: https://www.google.com/about/careers/applications/students
- category: bigtech
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:

### Meta
- ats: bespoke
- careers_page: https://www.metacareers.com/students-and-grads
- category: bigtech
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:

### Uber
- ats: bespoke
- careers_page: https://www.uber.com/us/en/careers/teams/university
- category: bigtech-backend
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Strong backend/distributed-systems. Verify portal type on first run.

### Airbnb
- ats: greenhouse
- slug: airbnb
- board_url: https://boards-api.greenhouse.io/v1/boards/airbnb/jobs?content=true
- careers_page: https://careers.airbnb.com
- category: bigtech-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Confirmed Greenhouse — auto-checkable despite being large.

### LinkedIn
- ats: bespoke
- careers_page: https://careers.linkedin.com/students
- category: bigtech-backend
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:

### Palantir
- ats: lever
- slug: palantir
- board_url: https://api.lever.co/v0/postings/palantir?mode=json
- careers_page: https://www.palantir.com/careers
- category: bigtech-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Confirmed Lever (~220 reqs mid-2026). Auto-checkable.

### Coinbase
- ats: greenhouse
- slug: coinbase
- board_url: https://boards-api.greenhouse.io/v1/boards/coinbase/jobs?content=true
- careers_page: https://www.coinbase.com/careers
- category: crypto-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Confirm slug.

### Nvidia
- ats: workday
- slug: nvidia / wd5 / NVIDIAExternalCareerSite
- board_url:
- careers_page: https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite
- category: bigtech
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Workday tenant known (nvidia/wd5). Auto-check only if agent supports Workday facet API; else manual.

---

## Tier 5 — Overlooked reliable hirers  *(where prestige-fragile applicants don't apply → better odds)*

> Mix of ATS types. Enterprise SaaS often Greenhouse-checkable; large non-tech/telecom
> usually Workday → manual. All hire real backend engineers with structured programs.

### Dropbox
- ats: greenhouse
- slug: dropbox
- board_url: https://boards-api.greenhouse.io/v1/boards/dropbox/jobs?content=true
- careers_page: https://jobs.dropbox.com
- category: saas-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### HubSpot
- ats: greenhouse
- slug: hubspotjobs
- board_url: https://boards-api.greenhouse.io/v1/boards/hubspotjobs/jobs?content=true
- careers_page: https://www.hubspot.com/careers
- category: saas-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Strong eng culture, backend in Java/Kotlin. 2026-07-09 — `hubspot` is a decoy: it's a real board named "HubSpot Product" that returns 0 jobs, so it never 404s. The live board is `hubspotjobs` (175 roles).

### Asana
- ats: greenhouse
- slug: asana
- board_url: https://boards-api.greenhouse.io/v1/boards/asana/jobs?content=true
- careers_page: https://asana.com/jobs
- category: saas-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Instacart
- ats: greenhouse
- slug: instacart
- board_url: https://boards-api.greenhouse.io/v1/boards/instacart/jobs?content=true
- careers_page: https://instacart.careers
- category: marketplace-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Reddit
- ats: greenhouse
- slug: reddit
- board_url: https://boards-api.greenhouse.io/v1/boards/reddit/jobs?content=true
- careers_page: https://www.redditinc.com/careers
- category: consumer-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Discord
- ats: greenhouse
- slug: discord
- board_url: https://boards-api.greenhouse.io/v1/boards/discord/jobs?content=true
- careers_page: https://discord.com/careers
- category: consumer-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Elixir/Rust/Python backend. Real-time systems.

### Lyft
- ats: greenhouse
- slug: lyft
- board_url: https://boards-api.greenhouse.io/v1/boards/lyft/jobs?content=true
- careers_page: https://www.lyft.com/careers
- category: marketplace-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Pinterest
- ats: greenhouse
- slug: pinterest
- board_url: https://boards-api.greenhouse.io/v1/boards/pinterest/jobs?content=true
- careers_page: https://www.pinterestcareers.com
- category: consumer-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Robinhood
- ats: greenhouse
- slug: robinhood
- board_url: https://boards-api.greenhouse.io/v1/boards/robinhood/jobs?content=true
- careers_page: https://careers.robinhood.com
- category: fintech-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### GitLab
- ats: greenhouse
- slug: gitlab
- board_url: https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true
- careers_page: https://about.gitlab.com/jobs
- category: devtools-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Fully remote. Confirm slug.

### Gusto
- ats: greenhouse
- slug: gusto
- board_url: https://boards-api.greenhouse.io/v1/boards/gusto/jobs?content=true
- careers_page: https://gusto.com/about/careers
- category: fintech-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Amplitude
- ats: greenhouse
- slug: amplitude
- board_url: https://boards-api.greenhouse.io/v1/boards/amplitude/jobs?content=true
- careers_page: https://amplitude.com/careers
- category: data-saas
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Faire
- ats: greenhouse
- slug: faire
- board_url: https://boards-api.greenhouse.io/v1/boards/faire/jobs?content=true
- careers_page: https://www.faire.com/careers
- category: marketplace-backend
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:

### Workday
- ats: workday
- careers_page: https://www.workday.com/en-us/company/careers.html
- category: enterprise-saas
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Ironically Workday itself. Large new-grad program.

### Intuit
- ats: workday
- careers_page: https://www.intuit.com/careers/students
- category: enterprise-saas
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Structured new-grad program, backend in Java.

### Walmart Global Tech
- ats: workday
- careers_page: https://careers.walmart.com/technology
- category: enterprise-backend
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Large new-grad SWE, real scale. Often overlooked.

### Target Tech
- ats: workday
- careers_page: https://tech.target.com/job-search
- category: enterprise-backend
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:

### Five9
- ats: greenhouse
- slug: five9
- board_url: https://boards-api.greenhouse.io/v1/boards/five9/jobs?content=true
- careers_page: https://www.five9.com/about/careers
- category: enterprise-saas
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-22 — added. Slug confirmed via board identity (name "Five9"), 160 open reqs. Cloud contact-center SaaS — real telephony/streaming scale behind it, but the product is an application, not infra, hence tier 5 rather than tier 2. Remote-friendly.

### SAP
- ats: bespoke
- slug:
- board_url:
- careers_page: https://jobs.sap.com
- category: enterprise-saas
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-22 — added. Runs its own SuccessFactors career site (jobs.sap.com); greenhouse/lever/ashby all 404. No keyless JSON board — manual by rule. Large, reliable US new-grad hiring (Palo Alto, Newtown Square, NYC).

### SolarWinds
- ats: greenhouse
- slug: solarwinds
- board_url: https://boards-api.greenhouse.io/v1/boards/solarwinds/jobs?content=true
- careers_page: https://www.solarwinds.com/careers
- category: observability-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-22 — added. Identity confirmed (name "SolarWinds"), 95 reqs. Austin HQ. Observability is the product, which is why this was first filed at tier 2 — but that tiered on product category, and this list ranks growth opportunity. Taken private by Turn/River (2025): cost discipline over growth investment, no liquid equity, heavy engineering offshoring, and a legacy product surface that is more maintenance than frontier work. Honest "no" as a FIRST job.

### Sumo Logic
- ats: greenhouse
- slug: sumologic
- board_url: https://boards-api.greenhouse.io/v1/boards/sumologic/jobs?content=true
- careers_page: https://www.sumologic.com/careers/
- category: observability-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-22 — added. Identity confirmed (name "Sumo Logic"). Cloud log analytics — good technology, but PE-owned by Francisco Partners since the 2023 take-private, meaning illiquid/immaterial equity and thin new-grad hiring. The 16-req board (vs SolarWinds 95, Five9 160) is a real signal of hiring appetite, not a broken slug — do not flag SUSPECT_EMPTY. Honest "no" as a FIRST job.

---

## Tier 6 — Chinese frontier AI labs  *(reach: frontier research + training infra; see caveats)*

> **Read before checking.** Every entry here is `check_method: manual` — none of the seven
> has a Greenhouse/Lever/Ashby-style keyless JSON board. Three (MiniMax, Zhipu, Moonshot)
> run on Feishu/Lark boards that return HTTP 405 on unauthenticated API reads; the rest are
> bespoke SPAs. Verified 2026-07-20.
>
> **These entries do not satisfy the Match Criteria as written.** `locations_preferred` is
> NYC/Remote US; most roles here are Beijing/Hangzhou/Shanghai and would need relocation and
> China work authorization. Only ByteDance Seed and Alibaba have confirmed US hiring. Treat
> the mainland-only entries as research-interest targets, not as new-grad pipeline.
>
> **Known slug trap:** `ashby/moonshot-ai` returns live jobs belonging to an unrelated US
> startup in NYC, not 月之暗面. Same failure mode as `ashby/cedar`. Do not add it.

### ByteDance Seed
- ats: bespoke
- careers_page: https://seed.bytedance.com/en/career
- category: chinese-frontier-ai
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Best US bridge on this tier. Seed's own page is marketing only — every "Apply" links
  to jobs.bytedance.com/experienced/position/{id}/detail. Confirmed roles in San Jose and
  Seattle plus Singapore; distributed training, RL infra, inference, compilers, agent systems.
  Has listed BS/MS LLM internships in Hong Kong. Main API returns 405 on POST.

### Alibaba (Qwen / Tongyi Lab)
- ats: bespoke
- careers_page: https://talent.alibaba.com/en/home
- category: chinese-frontier-ai
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: careers.alibaba.com 302s here — this is the canonical portal. No separate Qwen
  careers site; Tongyi Lab hires through Alibaba Cloud and Group channels, so filter within
  the central portal. Confirmed non-China roles in Singapore, Seattle, Frankfurt.

### DeepSeek
- ats: bespoke
- careers_page: https://talent.deepseek.com/
- category: chinese-frontier-ai
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Purest frontier-lab target on this tier. Self-hosted React SPA; JS bundle grepped,
  no API endpoint exposed. Beijing and Hangzhou only — no overseas roles found. Openings span
  model research, pretraining data, AI search, backend systems, agents, distributed infra,
  training frameworks.

### MiniMax
- ats: bespoke
- careers_page: https://www.minimax.io/careers
- category: chinese-frontier-ai
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Feishu board at vrfi1sk8a0.jobs.feishu.cn, entity confirmed as MiniMax; 405 on
  unauthenticated API. CN page is minimaxi.com/careers. Runs internships, graduate
  recruitment, experienced hiring, and a "Top Talent" track that explicitly waives degree and
  graduation-date restrictions — the one entry here where a 2027 date may not disqualify.
  Overseas locations unverified (SPA, listings not readable without JS).

### Zhipu AI / Z.ai
- ats: bespoke
- careers_page: https://zhipu-ai.jobs.feishu.cn/
- category: chinese-frontier-ai
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Feishu board, entity confirmed as 智谱华章科技股份有限公司; 405 on unauthenticated API.
  Campus recruiting for algorithms, RL frameworks, and engineering. Locations advertised:
  Beijing, Shanghai, Hangzhou, Shenzhen, Kuala Lumpur.

### Moonshot AI (Kimi)
- ats: bespoke
- careers_page: https://careers.kimi.com/
- category: chinese-frontier-ai
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Also hosts a Feishu board at moonshot.jobs.feishu.cn, entity confirmed as
  月之暗面科技有限公司; 405 on unauthenticated API. Emphasizes frontier AGI research and
  open-source work. Overseas locations unverified. See the `ashby/moonshot-ai` trap above.

### Shanghai AI Laboratory
- ats: bespoke
- careers_page: https://www.shlab.org.cn/joinus
- category: chinese-frontier-ai
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: Application flow splits into /joinus/social and /joinus/campus with an email fallback
  to hr@pjlab.org.cn. Shanghai and Beijing only. Strong on open-source models, multimodality,
  evaluation, embodied AI, safety, AI for science, training systems. Advertises daily
  internships and joint PhD programs with major Chinese universities.

---

## Tier 7 — Data infrastructure / lakehouse  *(research interest: strong tech, thin new-grad hiring)*

> **Read before checking.** These are data-versioning and lakehouse companies. The
> engineering is genuinely distributed-systems-shaped — table formats, query engines, object
> storage — but every entry here is **small and none runs a new-grad program**. Dremio has
> 6 open reqs (1 engineering); LanceDB's postings are 100% `Senior`, so it matches zero roles
> against `exclude_titles`; Onehouse puts its backend/infra reqs in Bangalore and only
> senior/staff reqs in the US; lakeFS is Tel Aviv with no listed US roles.
>
> Like Tier 6, these are tracked as **research interest, not new-grad pipeline** — a
> near-empty result here is the expected state, not a broken slug. `tier` is organizational
> only (`report.py` groups by it; nothing filters on it), so the `api` entries below are
> still fetched on every daily run.
>
> **Not separate companies — do not add:** *Delta Lake* is a Databricks project and
> Databricks is already Tier 1. *DVC* was acquired by lakeFS and is covered by that entry.
> DVC's original company, Iterative, rebranded to **DataChain**; `iterative.ai/careers` now
> 302s to a 404 and it has no careers presence.

### Dremio
- ats: greenhouse
- slug: dremio
- board_url: https://boards-api.greenhouse.io/v1/boards/dremio/jobs?content=true
- careers_page: https://www.dremio.com/careers/
- category: data-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-22 — added. Identity confirmed (name "Dremio"). Lakehouse query engine (Iceberg / Arrow / Nessie) — best pure distributed-systems work on this tier, and 3 of 6 reqs are New York. But only 6 reqs with ONE engineering role. Real board, company not in growth mode — do not flag SUSPECT_EMPTY.

### Onehouse
- ats: lever
- slug: Onehouse
- board_url: https://api.lever.co/v0/postings/Onehouse?mode=json
- careers_page: https://www.onehouse.ai
- category: data-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-22 — added. Slug is capital-O `Onehouse`; `lever/onehouse` 404s — the Lever API is case-sensitive. 12 reqs. Managed Apache Hudi lakehouse. Offshoring signal: Backend/Distributed-Systems and Kubernetes-Infra reqs are all Bangalore; US (Sunnyvale) reqs are senior/staff/open-source only.

### LanceDB
- ats: ashby
- slug: lancedb
- board_url: https://api.ashbyhq.com/posting-api/job-board/lancedb
- careers_page: https://lancedb.com/careers
- category: data-infra
- check_method: api
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-22 — added. Identity confirmed via jobUrl path. 9 reqs, but every engineering posting is titled "Senior ..." and several are Solutions/CS/RevOps — matches zero roles today. Kept because it is cheap to poll and would surface a junior req. Remote ("Americas timezones").

### lakeFS (Treeverse)
- ats: bespoke
- slug:
- board_url:
- careers_page: https://lakefs.io/careers/
- category: data-infra
- check_method: manual
- status: not-open
- last_checked:
- last_posting_seen:
- notes: 2026-07-22 — added. **Carries DVC**, which lakeFS acquired — do not add DVC separately. Self-hosts postings at lakefs.io/careers/<role>/ with no JSON board, so manual by rule. Tel Aviv HQ, ~3 open roles, no listed US locations — fails the location filter; research-interest only.

---

## Aggregator sources  *(check daily — highest yield for new-grad specifically)*

> These are the single best signal for 2027 new-grad roles across ALL companies, including
> ones not on this list. Diff the file/feed daily and surface any backend/new-grad matches.

### Simplify New-Grad-Positions
- ats: aggregator
- board_url: https://github.com/SimplifyJobs/New-Grad-Positions
- check_method: aggregator
- last_checked:
- last_posting_seen:
- notes: README table of 2026/2027 new-grad roles; updates via commits. Diff the raw README.

### Ouckah / CVrve New-Grad list
- ats: aggregator
- board_url: https://github.com/Ouckah/Summer2026-Internships
- check_method: aggregator
- last_checked:
- last_posting_seen:
- notes: Verify current repo name/URL on first run — these repos rename by cycle year.

### YC Work at a Startup
- ats: aggregator
- board_url: https://www.workatastartup.com/jobs?role=eng&expertise=backend
- check_method: manual
- last_checked:
- last_posting_seen:
- notes: Filter backend + entry-level. Requires login; agent flags for manual check.

---

## Maintenance notes

- **Slugs marked "confirm on first run":** the agent's first fetch is the test — if the API
  returns roles, the slug is right; if empty/error, try the company careers page URL to read
  the real slug (`boards.greenhouse.io/X`, `jobs.lever.co/X`, `jobs.ashbyhq.com/X`).
- **Companies migrate ATS.** When IBM-acquired HashiCorp or any acquired company stops
  returning results, re-check the live careers page and update `ats` + `slug` + `board_url`.
- **Add new companies** in the same field format so the agent parses them identically. The
  only fields the agent strictly needs are `ats`, `slug`/`board_url`, and `check_method`.
- **Coverage reality:** roughly the Tier 1–2 and Greenhouse/Lever/Ashby entries (~40+ here)
  auto-check reliably. Workday/bespoke (most of Tier 3–4 banks + Big Tech) are `manual` — the
  agent surfaces them for you rather than pretending to check them.
