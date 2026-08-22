"""`jobtracker serve` — a local tuning surface over state.db.

The static dashboard (`jobtracker dashboard`) is a snapshot you can mail to yourself
and open offline in five years. That property is worth keeping, so this does not
replace it: `dashboard` still emits the same self-contained file, and this adds a
second, *live* surface for the one thing a static file cannot do — write back.

Design constraints, all inherited from dashboard.py and all still load-bearing:

* **stdlib only.** `http.server`, nothing else. A tuning UI is not worth a web
  framework, and the no-new-dependencies property is what keeps this repo installable
  from a two-line requirements file.
* **127.0.0.1 by default.** This is a local tool with no authentication, and it writes
  to your criteria file. Binding it to a routable address would be handing a stranger
  an editor. `--host` exists, but you have to mean it.
* **Escape everything.** Titles and locations come from third-party ATS APIs.
* **GET never mutates.** The dashboard has a test asserting it never writes; the same
  rule holds here. Only POST changes anything.
* **Rows render server-side.** JS submits actions; it does not build the page.

`render_tuning()` is a module-level connection-in/string-out function, mirroring
`dashboard.build_dashboard`, so the page — and especially its escaping — can be tested
against a fixture database with no socket and no browser.

criteria.yaml is validated by parsing a *candidate* file before it replaces the real
one. A malformed write would break every subsequent run, and `criteria.py` exists
precisely so that config errors are loud rather than silent.
"""

from __future__ import annotations

import html
import json
import logging
import signal
import sqlite3
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import yaml

from . import (
    answers as answers_mod,
    applications as apps_mod,
    config,
    curation,
    dashboard as dashboard_mod,
    live,
    rank as rank_mod,
    resumes,
    safewrite,
    store,
    tuning,
)
from .criteria import _LIST_KEYS, load_criteria
from .match import location_label, location_rank, match
from .migrate import _HEADER
from .models import Company, Decision, Posting, Verdict

log = logging.getLogger("jobtracker.serve")

MAX_BODY = 64 * 1024  # a decision payload is a few hundred bytes

# The body cap for routes that carry a file. It is a *set*, not one path: a second upload
# route added without joining this set silently reads its body as `{}` and reports "no
# file" — a correct-looking error for entirely the wrong reason.
_UPLOAD_ROUTES = {"/api/resume", "/api/posting-resume", "/api/session/file"}
MAX_UPLOAD = resumes.MAX_UPLOAD

# No external anything, matching the static dashboard's guarantee.
#
# `connect-src 'self'` is required, not optional: every write on this server goes out as
# a fetch() to /api/..., and connect-src falls back to default-src when unset — which is
# 'none' here. Without it the browser blocks the request and the buttons silently do
# nothing.
#
# `img-src 'self'` is required for exactly the same reason and fails exactly the same
# way. The apply page's preview is a same-origin JPEG this server renders; under
# default-src 'none' it is blocked with no error anywhere, and the page shows a broken
# image over a browser that is working fine.
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "img-src 'self'; connect-src 'self'; form-action 'none'"
)

# Held for as long as a prefilled window is open. One browser profile directory, which
# Chromium locks, so two at once is not a thing that can work — and the second failure
# would happen on a worker thread where nothing can report it back to the click.
_APPLY_LOCK = threading.Lock()

# Held while the add-a-company form verifies a slug against the live board. That check is
# the one place this server opens a socket, and it is bounded rather than threaded (see
# `_api_company`) — so a second click, or a second tab, is REFUSED rather than queued.
# Queuing would stack a second multi-second freeze behind the first on a server that
# handles one request at a time, which is the shape this whole file avoids.
_VERIFY_LOCK = threading.Lock()


# -- rendering (pure reads, testable without a server) ------------------------------
# The lists `match()` actually gates on, in the order it consults them. Location lists
# are deliberately absent: they RANK, they never gate (see match.location_rank), so
# putting them in an editor headed "rules" would misdescribe what they do.
#
# `role_type_exclude` is the one most worth reaching, and the reason this section
# exists: it is checked at step 2, *before* the level gate, so it applies to every
# title whether or not the title names a level. That makes it the only list that can
# clear a non-engineering role out of the UNCERTAIN queue — a title with no level
# token can never be rejected by `exclude_titles` alone.
_GATING_LISTS = (
    ("exclude_titles", "reject", "Seniority and shape disqualifiers. Step 1."),
    (
        "role_type_exclude",
        "reject",
        "Off-target role families. Step 2 — before the level gate, so it applies to "
        "every title. This is what empties non-engineering roles out of Uncertain.",
    ),
    (
        "level_include",
        "gate",
        "The entry-level signal. No hit here leaves the title UNCERTAIN, never rejected.",
    ),
    ("role_type_include", "accept", "Backend-specific signal. Labels a match role:<token>."),
    (
        "engineering_terms",
        "accept",
        "Any engineering signal. Required alongside a level hit — it is what keeps "
        "'Finance Associate' from matching a backend tracker.",
    ),
)

# A suggestion is a phrase drawn from titles you REJECTED, so the only sane targets are
# the two reject lists. Offering the include lists here would let one click invert the
# meaning of the evidence — adding a reject phrase to `engineering_terms` would widen
# matching on exactly the titles you were trying to remove.
_SUGGEST_TARGETS = ("exclude_titles", "role_type_exclude")


def _list_picker(selected: str) -> str:
    """The target-list dropdown beside a suggestion."""
    opts = "".join(
        f'<option value="{k}"{" selected" if k == selected else ""}>{k}</option>'
        for k in _SUGGEST_TARGETS
    )
    return f"<select class=sugg-list>{opts}</select>"


def _rules_section(criteria) -> list[str]:
    """The current criteria lists, each with an add box.

    Read-and-add only. There is no delete control, deliberately: removing a token can
    silently re-admit thousands of postings already judged against it, and the safe
    path for that is an edit plus `jobtracker eval`, not a button that skips the
    regression replay this page exists to run.
    """
    out = ["<h2>Rules</h2>", "<p class=note>The lists <code>match()</code> gates on, in "
           "the order it consults them. Adding a token rematches every stored posting "
           "immediately — the counts in the alert are the blast radius.</p>"]
    for key, kind, why in _GATING_LISTS:
        tokens = list(getattr(criteria, key, []) or [])
        out.append(f'<div class="rules {kind}">')
        out.append(
            f"<h3><code>{key}</code> <small>{len(tokens)}</small></h3>"
            f"<p class=note>{html.escape(why)}</p>"
        )
        out.append("<div class=chips>")
        out.extend(f"<span class=chip>{html.escape(t)}</span>" for t in tokens)
        if not tokens:
            out.append("<span class=note>empty</span>")
        out.append("</div>")
        out.append(
            f'<div class=addbox><input class=token data-list="{key}" '
            f'placeholder="add a token to {key}" '
            f'aria-label="add a token to {key}">'
            f'<button class=add-token data-list="{key}">add</button></div>'
        )
        out.append("</div>")
    return out


def render_tuning(conn: sqlite3.Connection, criteria) -> str:
    """The tuning page as a string. Pure read — never writes to `conn`."""
    matches = store.open_postings_by_verdict(conn, "match")
    decisions = store.all_decisions(conn)
    report = tuning.evaluate(decisions, criteria) if decisions else None
    suggestions = tuning.suggest_rules(decisions, criteria) if decisions else []
    overrides = store.load_overrides(conn)

    p: list[str] = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Tuning — job tracker</title>",
        f"<style>{dashboard_mod._CSS}{_EXTRA_CSS}</style></head><body><div class=wrap>",
        f"<h1>Tuning {dashboard_mod.version_chip()}</h1>",
        _NAV,
    ]

    # Corpus health first: it is what makes editing rules safe rather than a guess.
    if report is None:
        p.append(
            "<p class=note>No judgments recorded yet. Reject something below and this "
            "becomes a regression corpus — every later rule change gets replayed "
            "against it before it ships.</p>"
        )
    else:
        cls = "ok" if report.ok else "bad"
        p.append(f'<p class="banner {cls}">{html.escape(report.summary())}</p>')
        for c in report.regressions:
            p.append(
                f"<p class=regression><b>{html.escape(c.company)}</b> "
                f"{html.escape(c.title)}<br><small>you said <b>{html.escape(c.yours)}</b>, "
                f"rules say <b>{html.escape(c.rules)}</b> via "
                f"<code>{html.escape(c.rule_reason)}</code></small></p>"
            )

    if suggestions:
        p.append("<h2>Suggested rules</h2>")
        p.append(
            "<p class=note>Phrases common to postings you rejected and absent from "
            "every posting you kept. This list empties as you apply them.</p>"
        )
        for s in suggestions:
            eg = html.escape(s.examples[0]) if s.examples else ""
            p.append(
                f"<div class=sugg><code>{html.escape(s.phrase)}</code> "
                f"<small>{s.rejected} rejects · e.g. {eg}</small> "
                f"{_list_picker(s.target_list)} "
                f"<button data-phrase=\"{html.escape(s.phrase, quote=True)}\" "
                "class=add-rule>add</button></div>"
            )

    p.extend(_rules_section(criteria))

    p.append(f"<h2>Open matches ({len(matches)})</h2>")
    p.append(
        "<p class=note>Every row shows the rule that produced it, so a bad match "
        "points straight at the rule responsible.</p>"
    )
    p.append(
        "<table><thead><tr><th>Company</th><th>Title</th><th>Location</th>"
        "<th>Fired</th><th></th></tr></thead><tbody>"
    )
    for row in matches:
        pinned = " ✓" if (row["company"], row["ats_job_id"]) in overrides else ""
        rank = location_rank(row["location"], criteria)
        p.append(
            "<tr>"
            f"<td>{html.escape(row['company'])}</td>"
            f'<td><a href="{dashboard_mod._safe_url(row["url"])}" rel=noreferrer>'
            f"{html.escape(row['title'])}</a></td>"
            f"<td><small>{html.escape(row['location'] or '—')} "
            f"<i>{html.escape(location_label(rank))}</i></small></td>"
            f"<td><code>{html.escape(row['reason'])}</code></td>"
            f'<td><button class=reject data-company="{html.escape(row["company"], quote=True)}"'
            f' data-job="{html.escape(row["ats_job_id"], quote=True)}">'
            f"not for me{pinned}</button></td>"
            "</tr>"
        )
    p.append("</tbody></table>")
    p.append(f"<script>{_JS}</script></div></body></html>")
    return "\n".join(p)


def render_settings(conn: sqlite3.Connection, answers_path: Path) -> str:
    """The answer bank and everything still unanswered. Pure read — never writes.

    The gap list is the machine's half of the conversation: every question an
    application form asked that nothing in `answers.yaml` could answer, deduplicated
    across the companies that asked it. Answering one here writes it into the file and
    invalidates the prefill plans that needed it, so the next `work --task prefill` run
    fills that field everywhere.
    """
    gaps = store.open_gaps(conn)
    answers, error = _load_answers_quietly(answers_path)

    p = [
        "<!doctype html><meta charset=utf-8><title>Settings</title>",
        f"<style>{dashboard_mod._CSS}{_EXTRA_CSS}{_SETTINGS_CSS}</style>",
        "<body><div class=wrap>"
        f"<h1>Settings {dashboard_mod.version_chip()}</h1>", _NAV,
        f"<p class=note>Answer bank: <code>{html.escape(str(answers_path))}</code>"
        " — git-ignored, and the source of truth. This page edits it in place.</p>",
    ]

    if error:
        p.append(f"<p class='banner bad'>{html.escape(error)}</p>")
    elif answers is None:
        p.append(
            "<p class='banner bad'>No answer bank yet — filling in your name and email "
            "below creates one.</p>"
        )

    p.extend(_identity_card(answers))
    p.extend(_resume_card(answers))

    from .tasks.prefill import gap_ask_count, split_gaps

    p.append(f"<h2>Unanswered questions ({len(gaps)})</h2>")
    if not gaps:
        p.append("<p class=note>Nothing outstanding. Every field prefill has seen so "
                 "far has an answer.</p>")

    # Two lists, because they are worth two different amounts of your time. A question
    # nine employers ask is answered once and fills nine forms forever; a question only
    # Stripe asks is worth exactly one application. Sorting the first by how many ask is
    # the whole point — that is the order in which answering pays.
    generic, per_company = split_gaps(gaps)
    if generic:
        p.append(f"<h3>Asked everywhere <span class=count>{len(generic)}</span></h3>")
        p.append("<p class=note>Answer one of these once and every employer that asks "
                 "it is filled in from then on. Most-asked first.</p>")
        for gap in generic:
            p.extend(_gap_card(gap, gap_ask_count(gap)))
    for company, rows in per_company:
        p.append(f"<h3>Only {html.escape(company)} asks "
                 f"<span class=count>{len(rows)}</span></h3>")
        for gap in rows:
            p.extend(_gap_card(gap, 1))

    if answers is not None:
        p.append(f"<h2>Answers you have written ({len(answers.answerable)})</h2>")
        p.append("<table><thead><tr><th>Key</th><th>Answer</th></tr></thead><tbody>")
        for key in answers.answerable:
            value = answers.get(key) or ""
            p.append(
                f"<tr><td><code>{html.escape(key)}</code></td>"
                f"<td>{html.escape(value[:160])}</td></tr>"
            )
        p.append("</tbody></table>")
        p.append(
            "<p class=note>Edit these in the file directly; this page only adds "
            "answers to questions it asked you.</p>"
        )

    p.append(f"<script>{_JS}</script></div></body></html>")
    return "\n".join(p)


def render_applications(conn: sqlite3.Connection, companies, today: str) -> str:
    """Everything you applied to, and every control for changing it. Pure read.

    Connection-in / string-out like `render_tuning` and `render_settings`, so it is
    testable against a fixture DB with no socket in sight.

    This is the editable half of the applications surface; the static dashboard renders
    the same data read-only through `dashboard._applications`. The split is the rule
    this repo already follows for the picks: buttons exist only where a live process can
    answer them, and a button's handler ships in the file that renders the button — so
    every control here has its branch in `_JS` below, not in `dashboard._JS`.
    """
    by_name = {c.name: c for c in (companies or [])}
    apps = store.all_applications(conn)
    events_by = store.events_by_application(conn)

    p = [
        "<!doctype html><meta charset=utf-8><title>Applications</title>",
        f"<style>{dashboard_mod._CSS}{_EXTRA_CSS}{_APPS_CSS}</style>",
        "<body><div class=wrap>"
        f"<h1>Applications {dashboard_mod.version_chip()}</h1>", _NAV,
    ]
    # Above the blank form on purpose: work the system is asking you to confirm outranks
    # a form asking you to type something in from scratch.
    p.extend(_mail_proposals_card(conn))
    p.extend(_add_application_card(today))

    if not apps:
        p.append(
            "<div class=empty>Nothing recorded yet. Add one above, or press "
            "<strong>I applied</strong> on a pick on the dashboard.</div>"
        )
    else:
        stats = apps_mod.summary(apps, events_by)
        p.append('<div class="tiles">')
        for k, v, n in (
            ("Total", str(stats["total"]), "applications recorded"),
            ("Active", str(stats["active"]), "still in play"),
            ("Interviewing", str(stats["interviewing"]), "at an interview stage"),
            ("Offers", str(stats["offers"]), "reached an offer"),
            ("Response rate", f'{stats["response_rate"]}%',
             f'{stats["responded"]} ever replied'),
        ):
            p.append(
                f'<div class="tile"><div class="k">{html.escape(k)}</div>'
                f'<div class="v">{html.escape(v)}</div>'
                f'<div class="n">{html.escape(n)}</div></div>'
            )
        p.append("</div>")

        groups = apps_mod.group(apps, events_by, today)
        for key, heading, blurb in (
            ("needs_action", "Needs action",
             "a date has come due, or nobody has moved in "
             f"{store.STALE_AFTER_DAYS} days"),
            ("active", "Active", "applied, waiting"),
            ("closed", "Closed", "offer, rejection, or withdrawn"),
        ):
            rows = groups[key]
            if not rows:
                continue
            p.append(
                f"<h2>{html.escape(heading)} <span class=count>{len(rows)}</span>"
                f'<span class="sub">{html.escape(blurb)}</span></h2>'
            )
            p.append('<div class="apps">')
            for app in rows:
                p.extend(_application_card(app, events_by, today, by_name))
            p.append("</div>")

    p.append(f"<script>{_JS}</script></div></body></html>")
    return "\n".join(p)



_COMPANIES_CSS = """
.cocard{border:1px solid var(--line);border-radius:10px;padding:1rem;margin:1rem 0;
        background:var(--card)}
.cocard .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
        gap:.6rem .9rem;margin-top:.7rem}
.cocard label{display:flex;flex-direction:column;gap:.25rem;font-size:.8rem;
        color:var(--muted)}
.cocard input,.cocard select,.cocard textarea{font:inherit;font-size:.85rem;padding:.35rem;
        border:1px solid var(--line);border-radius:6px;background:var(--bg);
        color:var(--fg)}
.cocard textarea{min-height:3.4rem;resize:vertical}
.cocard .wide{grid-column:1/-1}
.hint{font-size:.75rem;color:var(--muted);margin:.15rem 0 0}
.coresult{margin-top:.9rem;padding:.7rem;border-radius:8px;border:1px solid var(--line)}
.coresult pre{overflow-x:auto;font-size:.75rem;margin:.5rem 0 0;white-space:pre;
        max-height:22rem}
.coresult.bad{border-color:var(--warn)}
.cotable{width:100%;border-collapse:collapse;font-size:.85rem}
.cotable td,.cotable th{padding:.3rem .5rem;border-bottom:1px solid var(--line);
        text-align:left}
.cotable .muted{color:var(--muted)}
"""

# The `ats` values a new entry may name, with the four that have adapters first — the
# order is the answer to "which of these can I actually check automatically?".
_ATS_CHOICES = ("greenhouse", "ashby", "lever", "aggregator",
                "workday", "gem", "bespoke", "unknown")


def render_companies(conn: sqlite3.Connection, companies, error: str = "") -> str:
    """Every tracked company, and the form that adds one. Pure read.

    Connection-in / string-out like `render_tuning` and `render_settings`.

    Unlike `/applications`, this page must NOT fall back to an empty company list when
    companies.yaml will not parse. There a missing file costs a tier badge; here it would
    render "nothing tracked" over a file that exists and is broken — absence read as
    success, which is the failure DESIGN.md §3.4 exists to prevent. The caller passes the
    loader's own message instead and it renders in place of the table.
    """
    p = [
        "<!doctype html><meta charset=utf-8><title>Companies</title>",
        f"<style>{dashboard_mod._CSS}{_EXTRA_CSS}{_COMPANIES_CSS}</style>",
        "<body><div class=wrap>"
        f"<h1>Companies {dashboard_mod.version_chip()}</h1>", _NAV,
    ]
    p.extend(_add_company_card())
    if error:
        p.append(
            '<div class="coresult bad"><strong>companies.yaml did not load.</strong>'
            f"<pre>{html.escape(error)}</pre></div>"
        )
    else:
        p.extend(_tracked_companies(conn, companies))
    p.append(f"<script>{_JS}</script></div></body></html>")
    return "\n".join(p)


def _add_company_card() -> list:
    """The form. Not a `<form>` element — the CSP is `form-action 'none'`, so every page
    here collects `data-key` inputs and POSTs JSON, and a real form would silently do
    nothing on submit."""
    p = ["<div class=cocard>",
         "<strong>Add a company</strong>",
         "<p class=note>Written straight to companies.yaml, with the diff shown below. "
         "An <code>api</code> board is fetched and checked before anything is written; "
         "<code>manual</code> entries are never fetched, by rule.</p>",
         "<div class=grid>"]
    for key, label, placeholder in (
        ("name", "Name", "OpenRouter"),
        ("slug", "Slug", "openrouter"),
        ("category", "Category", "ai-infra"),
        ("careers_page", "Careers page", "https://openrouter.ai/careers"),
        ("board_url", "Board URL (aggregator feeds only)", "https://raw.githubusercontent.com/…"),
        ("expected_board_name", "Expected board name (optional)", "seeded from the board"),
    ):
        p.append(
            f"<label>{html.escape(label)}"
            f'<input class=newco data-key="{html.escape(key, quote=True)}" type=text '
            f'placeholder="{html.escape(placeholder, quote=True)}"></label>'
        )
    p.append("<label>ATS" + _select("newco", "ats", _ATS_CHOICES, "greenhouse") + "</label>")
    p.append(
        "<label>Check method"
        + _select("newco", "check_method", ("api", "manual", "aggregator"), "api")
        + '<p class=hint>An aggregator with no board URL is skipped rather than fetched — '
          "that is how an unconfirmed feed is parked.</p></label>"
    )
    p.append(
        "<label>Tier"
        + _select("newco", "tier", ("", "1", "2", "3", "4", "5", "6", "7"), "")
        + "</label>"
    )
    p.append(
        '<label class=wide>Notes<textarea class=newco data-key="notes" '
        'placeholder="Why this entry is what it is — especially after a fix."></textarea>'
        "</label>"
    )
    p.append("</div>")
    p.append("<div class=row style='margin-top:.8rem'>"
             "<button class=co-save>Verify and add</button> "
             # Rendered always and revealed by the script, never created by it: the parity
             # test reads the buttons off the markup, so a button the JS mints is a button
             # nothing checks has a handler. Same mechanism as `.tabs` and `.cotoggle`.
             '<button class=co-force hidden>Add without verifying</button></div>')
    p.append('<div class=coresult id=coout hidden></div>')
    p.append("</div>")
    return p


def _select(cls: str, key: str, options, current: str) -> str:
    opts = "".join(
        f'<option value="{html.escape(o, quote=True)}"'
        f'{" selected" if o == current else ""}>{html.escape(o or "—")}</option>'
        for o in options
    )
    return (f'<select class={cls} data-key="{html.escape(key, quote=True)}">'
            f"{opts}</select>")


def _tracked_companies(conn: sqlite3.Connection, companies) -> list:
    """The list, grouped by tier and in tier order — the order companies.yaml itself is
    kept in, and the one `insert_entry` maintains when it writes."""
    if not companies:
        return ['<div class=empty>No companies tracked yet.</div>']
    groups: dict = {}
    for c in companies:
        groups.setdefault(c.tier, []).append(c)
    # None last: the aggregator feeds sort after every tiered entry, on the page for the
    # same reason they do in the file.
    order = sorted((t for t in groups if t is not None)) + ([None] if None in groups else [])

    p = [f"<h2>Tracked <span class=count>{len(companies)}</span></h2>"]
    for tier in order:
        rows = groups[tier]
        var = dashboard_mod._band_var(tier)
        p.append(
            f'<h3><span class="tier" style="background:var({var});color:var({var}-ink)">'
            f'T{tier if tier is not None else "?"}</span> '
            f'<span class=count>{len(rows)}</span></h3>'
        )
        p.append('<table class=cotable><tbody>')
        for c in rows:
            health = store.get_health(conn, c.name)
            status = health.status.value if health else "never checked"
            board = (health.observed_board_name if health else "") or ""
            target = f"{c.ats}/{c.slug}" if c.slug else c.ats
            link = c.careers_page or c.board_url
            name = html.escape(c.name)
            if link:
                name = (f'<a href="{dashboard_mod._safe_url(link)}" target="_blank" '
                        f'rel="noopener">{name}</a>')
            p.append(
                f"<tr><td>{name}</td>"
                f"<td class=muted><code>{html.escape(target)}</code></td>"
                f"<td class=muted>{html.escape(c.check_method)}</td>"
                f"<td class=muted>{html.escape(status)}"
                f"{' · ' + html.escape(board) if board else ''}</td></tr>"
            )
        p.append("</tbody></table>")
    return p

def render_apply(conn: sqlite3.Connection, session, view_url: str = "") -> str:
    """The live application form, mirrored into fields you can actually type in.

    Pure read — it never writes to `conn`, and it never touches the browser. Everything
    it renders comes from the `live.Session` the worker thread publishes into.

    Why this page exists at all: the window is on the machine running `serve`, which on a
    headless host means watching it through VNC. Every keystroke was a round trip to a
    remote X server rendered as video, for a task that is fifteen text fields. Here the
    typing is local and instant and only the finished value crosses the wire.

    What it deliberately does not do is submit. There is no control on this page that
    can, for the same reason `browser.py` has no click path: an application is
    irreversible and goes out under your name. The window is still where you read it over
    and send it, which is why the viewer link is kept and pointed at exactly that job.
    """
    p = [
        "<!doctype html><meta charset=utf-8><title>Fill in</title>",
        f"<style>{dashboard_mod._CSS}{_EXTRA_CSS}{_APPLY_CSS}</style>",
        "<body><div class=wrap>",
        f"<h1>Fill in {dashboard_mod.version_chip()}</h1>", _NAV,
    ]

    if session is None:
        p.append(
            "<p class=note>No window is open. Pick a job on the "
            '<a href="/">dashboard</a> and press <b>Open prefilled</b> — this page is '
            "where you fill it in.</p>"
        )
        p.append(f"<script>{_APPLY_JS}</script></div></body></html>")
        return "\n".join(p)

    snap = session.snapshot()
    p.append(
        f'<h2 class="who">{html.escape(snap["company"])} — '
        f'{html.escape(snap["title"])}</h2>'
    )
    p.append(
        f'<p class="phase" data-epoch="{html.escape(str(snap["epoch"]), quote=True)}">'
        f'<span class="pill" id="phase">{html.escape(snap["phase"])}</span> '
        f'<span id="summary">{html.escape(live.summary(snap))}</span></p>'
    )

    # The form changed shape under the page. Rendered here, hidden, so the script only
    # ever unhides it — the same rule that keeps `.applymsg` server-rendered: a script
    # that writes markup is a script that can be made to write somebody else's markup.
    p.append(
        '<p class="banner bad" id="moved" hidden>The form changed while you were '
        "typing, so the fields below no longer point at it. Reload to read it again — "
        'nothing you already pushed is lost. <button id="reload">Reload</button></p>'
    )

    # The window is gone. Said here rather than only in the phase pill, because the
    # difference between "the browser is holding your form" and "there is no browser"
    # is the difference between a page that works and a page that quietly does nothing:
    # every field would still accept typing, every push would queue into a closed
    # session, and the button that closed it would sit on "closing…" forever. That is
    # the mirror-over-a-dead-browser state this whole phase exists to make visible.
    #
    # Rendered from the server when the session is already closed, so a reload is honest
    # too; the script only ever unhides it.
    gone = "" if snap["phase"] == live.CLOSED else " hidden"
    p.append(
        f'<p class="banner bad" id="gone"{gone}>The window is closed, and nothing on '
        "this page can reach a browser now. An application you did not submit is not "
        "saved anywhere — no ATS keeps a draft for an anonymous candidate — so this is "
        "a record of what was filled in, not something to carry on with. "
        '<a href="/">Back to the dashboard</a> to open it again.</p>'
    )

    p.append('<div class="split">')

    # -- the preview ------------------------------------------------------------------
    p.append('<div class="pane">')
    p.append(
        '<div class="phead">Preview '
        '<button id="pause" data-paused="0">Pause</button>'
        '<button id="zoom" data-fit="1">100%</button>'
        '<span class="ago" id="ago"></span></div>'
    )
    # A still of the *whole* form, refreshed on a cadence — not a stream. What this page
    # replaces was a stream, and being a stream is why it was slow.
    #
    # It renders scaled to fit, from the server, because a browser viewport is 720px and
    # the form is several thousand: a viewport-shaped picture showed five fields of
    # thirty-two over a window nobody here can scroll. `.fit` is written in the markup
    # rather than added by the script, so the whole page is what you get with JS off —
    # and `#zoom`, which only a script can honour, stays hidden until one is running.
    # Same rule as `.tabs` and `.cotoggle` on the dashboard.
    p.append(
        '<div id="shotbox">'
        '<img id="preview" class="fit" '
        'alt="the whole application form as the browser sees it"></div>'
    )
    p.append(
        '<p class=note>The whole page, a few seconds behind. The fields are not behind. '
        'Click it (or <b>100%</b>) to read it at full size.</p>'
    )
    if view_url:
        # Kept, and pointed at the two things a mirrored form genuinely cannot do:
        # solve a captcha, and let you read the whole application before you send it.
        # Still only a link — this app does not start, probe or manage the viewer.
        p.append(
            "<h3>Review &amp; submit</h3>"
            "<p class=note>When it is right, open the window and send it yourself. "
            "Nothing on this page can submit an application.</p>"
            f'<p><a class="viewwin" href="{dashboard_mod._safe_url(view_url)}" '
            'target="_blank" rel="noopener">View window ↗</a></p>'
        )
    else:
        p.append(
            "<h3>Review &amp; submit</h3>"
            "<p class=note>Nothing on this page can submit an application — that is "
            "deliberate. Set <code>JOBTRACKER_BROWSER_VIEW_URL</code> to get a link to "
            "the window from here, or use the screen the browser is drawing on.</p>"
        )
    # The way out of a session, and on a headless host the only one there is. The window
    # is on the machine running `serve`; if you cannot reach that machine's screen you
    # cannot close it, and until it closes the one-window lock stays held and every later
    # "Open prefilled" is refused. It was `serve` restarts or nothing. Observed
    # 2026-08-19.
    p.append(
        '<p class="done"><button id="closewin">Done — close the window</button> '
        '<span class="note" id="donemsg"></span></p>'
    )
    p.append("</div>")

    # -- the fields -------------------------------------------------------------------
    p.append('<div class="pane fields">')
    p.append(
        '<div class="phead">Fields '
        '<button id="reread">Read the form again</button></div>'
    )
    if not snap["discovered"]:
        # Zero is a finding, not a finished job. Said the same way `FillReport` says it.
        p.append(
            f'<p class="banner bad">{html.escape(live.summary(snap))}</p>'
            "<p class=note>Nothing was found to fill in: the page may not have "
            "rendered, may only link to the real application, or may be behind a login. "
            "Open the window and look.</p>"
        )
    for row in snap["fields"]:
        p.extend(_live_field(row, snap["epoch"], dead=snap["phase"] == live.CLOSED))
    p.append(
        f'<p class=note id="counts">{html.escape(live.summary(snap))}</p>'
    )
    # The mirror can only show what the discovery pass could see — a collapsed section,
    # a drag-and-drop dropzone or a rich-text editor is not an input and never appears
    # here. Saying so beats implying the list is the whole form.
    p.append(
        "<p class=note>These are the fields read off the page. Anything it could not "
        "read — a custom widget, a collapsed section, a captcha — is only in the "
        "window.</p>"
    )
    p.append("</div>")

    p.append("</div>")
    p.append(f"<script>{_APPLY_JS}</script></div></body></html>")
    return "\n".join(p)


def _live_field(row: dict, epoch: int, dead: bool = False) -> list:
    """One mirrored form field.

    Everything here is third-party text off an ATS page — labels, option values, the
    value we put in — so all of it is escaped, exactly like the posting titles on the
    dashboard.

    The control is server-rendered rather than built in JS so the page is legible and
    complete with the script off; the script only reads values out of it and writes
    statuses back in.
    """
    handle = html.escape(row["handle"], quote=True)
    label = html.escape(row["label"] or row["key"] or row["handle"])
    status = row["status"]
    # `dead` is the window being gone. A control that still takes typing over a browser
    # that closed is the same lie as a live-looking preview of it: every push would be
    # refused one field at a time, which reads as "this field would not take it".
    off = " disabled" if dead else ""
    out = [
        f'<div class="lf" data-handle="{handle}" '
        f'data-epoch="{html.escape(str(epoch), quote=True)}" '
        f'data-type="{html.escape(row["type"], quote=True)}">',
        f'<div class="lab">{label}',
    ]
    if row["required"]:
        out.append(' <span class="req">required</span>')
    out.append(f' <span class="st st-{html.escape(status, quote=True)}">'
               f'{html.escape(_STATUS_WORD.get(status, status))}</span>')
    out.append("</div>")

    value = html.escape(row["value"] or "")
    if row["type"] in ("select", "multiselect"):
        out.append(f'<select class="lv"{off}>')
        # A blank first option, always. Without one, opening the page would look like
        # every dropdown already holds its first value — and a dropdown we could not
        # answer is a gap, which is a thing to see rather than a thing to hide.
        out.append('<option value="">— choose —</option>')
        for option in row["options"]:
            sel = " selected" if option == row["value"] else ""
            o = html.escape(option, quote=True)
            out.append(f'<option value="{o}"{sel}>{html.escape(option)}</option>')
        out.append("</select>")
    elif row["type"] == "file":
        out.append(f'<input class="lf-file" type="file"{off}>')
        if row["value"]:
            # A file input is the one control a browser gives you no way to empty — you
            # can pick a different file, never no file. Without this button the resume
            # is the single field on the form that cannot be taken back off.
            out.append(f'<p class=note>attached: <code>{value}</code> '
                       f'<button class="lf-detach"{off}>detach</button></p>')
    elif row["type"] == "checkbox":
        checked = " checked" if row["status"] == live.FILLED else ""
        out.append(f'<label class="cbx"><input class="lv" type="checkbox"{checked}'
                   f"{off}> yes</label>")
    elif row["type"] == "textarea":
        out.append(f'<textarea class="lv" rows="4"{off}>{value}</textarea>')
    else:
        out.append(f'<input class="lv" type="text" value="{value}"{off}>')

    # Answering it here can also answer it everywhere. Offered only where there is
    # something to learn — a field the fill could not answer — and it goes through
    # `/api/answer`, the same writer the Settings tab uses. No second path into the bank.
    if status in (live.GAP, live.REFUSED) and row["type"] != "file":
        key = html.escape(answers_mod.slugify(row["label"] or row["key"]), quote=True)
        out.append(
            '<label class="bank"><input type="checkbox" class="tobank"> also save to my '
            f'answer bank as <input class="bankkey" type="text" value="{key}"></label>'
        )
    out.append("</div>")
    return out


# What each status is called on the page. Duplicated in `_APPLY_JS.paint`, because one
# renders on the server and the other repaints on a poll; `test_the_page_and_its_script
# _call_a_status_the_same_thing` is what keeps the two in step.
_STATUS_WORD = {
    live.FILLED: "filled",
    live.GAP: "needs you",
    live.REFUSED: "would not take it",
    live.PENDING: "…",
    live.CLEARED: "cleared",
}


def _mail_proposals_card(conn: sqlite3.Connection) -> list:
    """What your inbox suggests happened, awaiting your ruling.

    Nothing here has been written to `applications`. Accepting is what writes, which is
    the whole shape of this subsystem — the narrower decides that a message is relevant,
    the model decides what it means, and you decide whether it happened.

    Everything rendered comes from a stranger with your email address, so every field is
    escaped and the message id travels in a data attribute rather than a handler.
    """
    rows = store.pending_mail_proposals(conn)
    if not rows:
        # No zero-state line. A permanent "0 from your inbox" stops being read long
        # before it has anything to say.
        return []

    p = [f"<h2>From your mail <span class=count>{len(rows)}</span>"
         '<span class="sub">proposed — nothing has been written yet</span></h2>',
         '<div class="apps">']
    for row in rows:
        job = row["ats_job_id"] or ""
        status = row["status"]
        p.append(f'<article class="app prop" data-message='
                 f'"{html.escape(row["message_id"], quote=True)}">')
        bits = [f'<span class="st st-{html.escape(status)}">{html.escape(status)}</span>',
                f'<span>{html.escape(row["company"])}</span>']
        if row["sent_on"]:
            bits.append(f'<span>{html.escape(row["sent_on"])}</span>')
        bits.append(f'<span class="src">{html.escape(row["from_addr"])}</span>')
        p.append(f'<div class="meta">{"<span>·</span>".join(bits)}</div>')
        p.append(f'<div class="subj">{html.escape(row["subject"] or "(no subject)")}</div>')
        if row["evidence"]:
            p.append(f'<div class="quote">“{html.escape(row["evidence"])}”</div>')
        if row["snippet"]:
            p.append(f'<div class="note">{html.escape(row["snippet"])}</div>')

        choices = json.loads(row["choices"] or "[]")
        if not job:
            # The narrower could not tell which application this is about, and the model
            # declined to. Guessing would put a stage on the wrong job; asking costs one
            # dropdown.
            p.append('<label class=note>Which application '
                     '<select class=propjob><option value="">choose…</option>')
            for choice in choices:
                app = store.get_application(conn, row["company"], choice)
                label = app["title"] if app else choice
                p.append(f'<option value="{html.escape(choice, quote=True)}">'
                         f"{html.escape(label)}</option>")
            p.append("</select></label>")
        else:
            app = store.get_application(conn, row["company"], job)
            if app is not None:
                p.append(f'<div class=note>{html.escape(app["title"])} '
                         f'— currently <strong>{html.escape(app["status"])}</strong></div>')
        p.append('<div class="row">'
                 "<button class=app-accept>Accept</button>"
                 "<button class=app-dismiss>Not this</button></div>")
        p.append("</article>")
    p.append("</div>")
    return p


def _add_application_card(today: str) -> list:
    """Record a job the pipeline never surfaced — a referral, a LinkedIn post, a company
    that is not on the target list at all.

    Only company and title are required. Everything else is optional because the moment
    you want to record an application is the moment right after you sent it, and a form
    that demands six fields then is a form you will skip.
    """
    p = ["<div class=addapp>",
         "<strong>Add an application</strong>",
         "<p class=note>For anything you applied to outside the tracker. Company and "
         "title are required; the date applied is today.</p>",
         "<div class=grid>"]
    for key, label, kind, placeholder in (
        ("company", "Company", "text", "Ramp"),
        ("title", "Title", "text", "Backend Engineer, New Grad"),
        ("url", "Link (optional)", "url", "https://…"),
        ("location", "Location (optional)", "text", "New York, NY"),
        ("next_action_note", "Next action (optional)", "text", "follow up"),
    ):
        p.append(
            f"<label>{html.escape(label)}"
            f'<input class=newapp data-key="{html.escape(key, quote=True)}" '
            f'type={kind} placeholder="{html.escape(placeholder, quote=True)}"></label>'
        )
    p.append("<label>Stage" + _status_select("newapp", "applied") + "</label>")
    p.append(
        '<label>Follow up on<input class=newapp data-key="next_action" type=date '
        f'value="{html.escape(apps_mod.default_next_action(today), quote=True)}"></label>'
    )
    p.append("</div>")
    p.append("<div class=row style='margin-top:.8rem'>"
             "<button class=app-add>Add application</button></div>")
    p.append("</div>")
    return p


def _status_select(cls: str, current: str, key: str = "status") -> str:
    options = "".join(
        f'<option value="{html.escape(s, quote=True)}"'
        f'{" selected" if s == current else ""}>{html.escape(s)}</option>'
        for s in store.APPLICATION_STATUSES
    )
    return (f'<select class={cls} data-key="{html.escape(key, quote=True)}">'
            f"{options}</select>")


def _application_card(app, events_by, today: str, by_name) -> list:
    """One application, with its controls.

    Mirrors `dashboard._application` for everything above the form — deliberately, so
    the two surfaces read the same — and adds the row of inputs that can change it.

    The identity travels in `data-company` / `data-job` attributes rather than being
    interpolated into a handler, for the same reason the picks do it: a manual entry's
    company name is text the user typed, and a quote in it would otherwise break out.
    """
    events = events_by.get((app["company"], app["ats_job_id"]), [])
    state = apps_mod.action_state(app, today)
    stale = apps_mod.is_stale(app, today)

    cls = "app"
    if state in ("overdue", "today", "soon"):
        cls += " urgent"
    elif stale:
        cls += " stale"
    if apps_mod.is_closed(app):
        cls += " done"

    c = html.escape(app["company"], quote=True)
    j = html.escape(app["ats_job_id"], quote=True)
    p = [f'<article class="{cls}" data-company="{c}" data-job="{j}">']

    title = html.escape(app["title"])
    href = dashboard_mod._safe_url(app["url"])
    heading = (
        f'<a href="{href}" target="_blank" rel="noopener">{title}</a>'
        if href != "#" else title
    )
    p.append(f'<h3>{heading} <span class="co">· {html.escape(app["company"])}</span></h3>')

    meta = []
    repeats = apps_mod.round_counts(events).get(app["status"], 0)
    times = f" ×{repeats}" if repeats > 1 else ""
    meta.append(
        f'<span class="st st-{html.escape(app["status"], quote=True)}">'
        f'{html.escape(app["status"])}{html.escape(times)}</span>'
    )
    tier = dashboard_mod._tier_of(app["company"], by_name)
    if tier != "—":
        var = dashboard_mod._band_var(tier)
        meta.append(
            f'<span class="tier" style="background:var({var});color:var({var}-ink)">'
            f"T{html.escape(str(tier))}</span>"
        )
    if app["location"]:
        meta.append(html.escape(app["location"]))
    applied_days = apps_mod.days_since(app["applied_at"], today)
    if applied_days is not None:
        meta.append(f"applied {dashboard_mod._ago(applied_days)}")
    moved = apps_mod.days_since(app["updated_at"], today)
    if stale and moved is not None:
        meta.append(f'<span class="quiet">no movement in {moved}d</span>')
    if state:
        meta.append(dashboard_mod._due_label(app, today, state))
    if app["source"] == "manual":
        meta.append('<span class="src">manual</span>')
    p.append(f'<div class="meta">{" · ".join(meta)}</div>')

    if len(events) > 1:
        p.append(f"<details><summary>History ({len(events)})</summary><div class='tl'>")
        for event in events:
            p.append(
                f'<span class="d">{html.escape(apps_mod.day_of(event["at"]) or "")}</span>'
                f'<span class="s">{html.escape(event["status"])}</span>'
                f'<span class="n">{html.escape(event["note"] or "")}</span>'
            )
        p.append("</div></details>")

    # Two separate writes, because they mean different things. Moving the stage is an
    # event and joins the history; changing a reminder is not and must not, or the
    # timeline fills with entries recording that you rescheduled a phone call.
    p.append("<div class=appform>")
    p.append("<label>Stage" + _status_select("appstatus", app["status"]) + "</label>")
    p.append(
        '<label>What happened<input class=appnote type=text placeholder="round 2 — '
        'system design"></label>'
    )
    p.append('<div class=acts><button class=app-save>Log stage</button></div>')
    p.append(
        '<label>Follow up on<input class=appnext type=date value="'
        f'{html.escape(app["next_action"] or "", quote=True)}"></label>'
    )
    p.append(
        '<label>On what<input class=appnextnote type=text value="'
        f'{html.escape(app["next_action_note"] or "", quote=True)}" '
        'placeholder="follow up"></label>'
    )
    p.append('<div class=acts><button class=app-meta>Set reminder</button>'
             '<button class="app-delete danger">Delete</button></div>')
    p.append("</div>")

    if app["note"]:
        p.append(f'<div class="note">{html.escape(app["note"])}</div>')
    p.append("</article>")
    return p


def _identity_card(answers) -> list:
    """The fields every application asks for, as a form.

    This is what makes a fresh box usable: with no `answers.yaml` at all, filling in the
    three required fields here is what creates one. The alternative was copying an
    example file that ships someone else's name in it.

    Every key is rendered, present or not, because the closed `IDENTITY_KEYS` tuple is
    the whole vocabulary — a field that is not on this form can never be filled by
    anything downstream, so hiding the empty ones would hide the actual options.
    """
    from .answers import IDENTITY_KEYS, REQUIRED_IDENTITY

    held = answers.identity if answers is not None else {}
    p = ["<h2>You</h2>",
         "<p class=note>Typed into every application form. Required fields are marked; "
         "the rest are filled when a form asks for them. Clearing a field removes it.</p>",
         "<div class=card>"]
    for key in IDENTITY_KEYS:
        required = " <span class=req>required</span>" if key in REQUIRED_IDENTITY else ""
        value = html.escape(str(held.get(key, "")), quote=True)
        label = key.replace("_", " ")
        p.append(
            f"<label class=field><span>{html.escape(label)}{required}</span>"
            f'<input class=identity data-key="{html.escape(key, quote=True)}" '
            f'type=text value="{value}"></label>'
        )
    p.append("<div class=row><button class=save-identity>Save</button></div>")
    p.append("</div>")
    return p


def _gap_card(gap, asked_by: int) -> list:
    """One unanswered question, with the box you answer it in.

    Identical markup in both lists on purpose. The split above decides ordering and
    nothing else, so `div.gap`, `input.answer[data-key]` and `button.save[data-key]` are
    the same everywhere — which is why `_JS`'s save branch needed no change at all: it
    looks the input up by key, and `question_key` is the table's primary key.
    """
    p = ["<div class=gap>"]
    p.append(f"<div class=ask>{html.escape(gap['ask'])}</div>")
    note = (f"asked by {html.escape(gap['seen_on'])} · type {html.escape(gap['type'])}"
            f" · first seen {html.escape(gap['first_seen'])}")
    if asked_by > 1:
        note += f" · <strong>{asked_by} employers</strong>"
    p.append(f"<div class=note>{note}</div>")
    if gap["options"]:
        p.append(f"<div class=note>one of: {html.escape(gap['options'])}</div>")
    # The key travels in a data attribute rather than being interpolated into a
    # handler — the question text comes from a third-party ATS.
    key = html.escape(gap["question_key"], quote=True)
    p.append(
        f"<div class=row><input class=answer type=text placeholder='Your answer' "
        f'data-key="{key}">'
        f'<button class=save data-key="{key}">Save</button></div>'
    )
    p.append("</div>")
    return p


def _resume_card(answers) -> list:
    """Upload a resume, or see the one already attached.

    A resume is the one field no URL and no cookie can ever carry — it is attached by
    `set_input_files()` on a real page — so the file has to exist on disk beside the
    answer bank before an application can be prefilled at all.
    """
    p = ["<h2>Resume</h2>", "<div class=card>"]
    resume = answers.resume if answers is not None else None
    if resume is not None and resume.is_file():
        size = resume.stat().st_size
        p.append(
            f"<p>Attached: <code>{html.escape(resume.name)}</code> "
            f"<span class=note>({size // 1024} KB)</span></p>"
        )
    else:
        p.append("<p class=note>No resume attached. Applications cannot be prefilled "
                 "without one.</p>")

    if answers is None:
        p.append("<p class=note>Save your name and email above first — the upload needs "
                 "an answer bank to record the path in.</p>")
    else:
        p.append(
            "<div class=row><input type=file id=resume-file accept='.pdf,.docx'>"
            "<button class=upload-resume>Upload</button></div>"
            "<p class=note>PDF or DOCX, up to 6 MB. Replaces whatever is attached now."
            "</p>"
        )
    p.append("</div>")
    return p


def _load_answers_quietly(path: Path):
    """(Answers|None, error|None). A missing or broken file is a page, not a 500."""
    from .answers import load_answers

    if not Path(path).exists():
        return None, None
    try:
        return load_answers(path), None
    except ValueError as exc:
        return None, str(exc)


# -- server -------------------------------------------------------------------------
class TuningServer(HTTPServer):
    """Carries the paths the handler needs; HTTPServer has nowhere else to put them."""

    def __init__(self, addr, handler, db_path: Path, criteria_path: Path,
                 companies_path: Optional[Path],
                 answers_path: Optional[Path] = None) -> None:
        super().__init__(addr, handler)
        self.db_path = db_path
        self.criteria_path = criteria_path
        self.companies_path = companies_path
        self.answers_path = answers_path or config.ANSWERS_YAML


class Handler(BaseHTTPRequestHandler):
    server_version = "jobtracker"

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- plumbing ------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        return store.connect(self.server.db_path)

    def _send(self, body: str, status: int = 200,
              ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", _CSP)
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, ctype: str, status: int = 200) -> None:
        """A response that is not text. Today that is exactly one thing: the preview.

        `_send` encodes UTF-8, so it cannot carry a JPEG. Kept to the same CSP and given
        `no-store` because the image changes every couple of seconds and a cached one is
        a picture of a form you already filled in.
        """
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _CSP)
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        self._send(json.dumps(payload), status, "application/json")

    def _read_json(self, limit: int = MAX_BODY) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, TypeError):
            return {}

    # -- routing -------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        # Health checks answer before the try/except so they can never be turned into the
        # HTML 500 page, and so an orchestrator polling them adds no DB load on the
        # liveness path. Kept off the nav — they are for machines, not the user.
        if path == "/healthz":
            self._send_json({"status": "ok"})
            return
        if path == "/readyz":
            payload, status = self._readiness()
            self._send_json(payload, status)
            return
        try:
            if path == "/":
                self._send(self._render_dashboard())
            elif path == "/tuning":
                conn = self._conn()
                try:
                    page = render_tuning(conn, load_criteria(self.server.criteria_path))
                finally:
                    conn.close()
                self._send(page)
            elif path == "/settings":
                conn = self._conn()
                try:
                    page = render_settings(conn, Path(self.server.answers_path))
                finally:
                    conn.close()
                self._send(page)
            elif path == "/applications":
                conn = self._conn()
                try:
                    page = render_applications(conn, self._companies(), _today())
                finally:
                    conn.close()
                self._send(page)
            elif path == "/companies":
                conn = self._conn()
                try:
                    # Not self._companies(): that swallows a load failure and returns [],
                    # which on this page would render "nothing tracked" over a file that
                    # is merely broken. Here the failure is the news.
                    error = ""
                    try:
                        companies = config.load_companies(self.server.companies_path)
                    except Exception as exc:  # noqa: BLE001
                        companies, error = [], str(exc)
                    page = render_companies(conn, companies, error)
                finally:
                    conn.close()
                self._send(page)
            elif path == "/apply":
                conn = self._conn()
                try:
                    page = render_apply(conn, live.current(), config.BROWSER_VIEW_URL)
                finally:
                    conn.close()
                self._send(page)
            elif path == "/api/session":
                self._send_json(self._api_session())
            elif path == "/api/session/preview.jpg":
                self._send_preview()
            else:
                self._send("<h1>404</h1>", 404)
        except Exception:  # noqa: BLE001
            log.exception("GET %s failed", path)
            self._send("<h1>500</h1><p>See the server log.</p>", 500)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        # The cap is per-route rather than global: only the upload carries a file, and
        # leaving 6 MB open on every endpoint would let a decision POST buffer one.
        payload = self._read_json(MAX_UPLOAD if path in _UPLOAD_ROUTES else MAX_BODY)
        try:
            if path == "/api/decision":
                self._send_json(self._api_decision(payload))
            elif path == "/api/disposition":
                self._send_json(self._api_disposition(payload))
            elif path == "/api/rule":
                self._send_json(self._api_rule(payload))
            elif path == "/api/rematch":
                self._send_json(self._api_rematch())
            elif path == "/api/answer":
                self._send_json(self._api_answer(payload))
            elif path == "/api/identity":
                self._send_json(self._api_identity(payload))
            elif path == "/api/resume":
                self._send_json(self._api_resume(payload))
            elif path == "/api/posting-resume":
                self._send_json(self._api_posting_resume(payload))
            elif path == "/api/posting-resume/clear":
                self._send_json(self._api_posting_resume_clear(payload))
            elif path == "/api/prefill":
                self._send_json(self._api_prefill(payload))
            elif path == "/api/apply-to":
                self._send_json(self._api_apply_to(payload))
            elif path == "/api/session/set":
                self._send_json(self._api_session_set(payload))
            elif path == "/api/session/clear":
                self._send_json(self._api_session_clear(payload))
            elif path == "/api/session/rediscover":
                self._send_json(self._api_session_command(live.REDISCOVER))
            elif path == "/api/session/close":
                self._send_json(self._api_session_close())
            elif path == "/api/session/file":
                self._send_json(self._api_session_file(payload))
            elif path == "/api/company":
                self._send_json(self._api_company(payload))
            elif path == "/api/application":
                self._send_json(self._api_application(payload))
            elif path == "/api/application/meta":
                self._send_json(self._api_application_meta(payload))
            elif path == "/api/application/delete":
                self._send_json(self._api_application_delete(payload))
            elif path == "/api/mail/accept":
                self._send_json(self._api_mail_accept(payload))
            elif path == "/api/mail/dismiss":
                self._send_json(self._api_mail_dismiss(payload))
            else:
                self._send_json({"ok": False, "error": "unknown endpoint"}, 404)
        except Exception as exc:  # noqa: BLE001
            log.exception("POST %s failed", path)
            self._send_json({"ok": False, "error": str(exc)}, 500)

    def _readiness(self) -> tuple[dict, int]:
        """Ready = the DB opens and answers and criteria parses. Deliberately distinct
        from liveness: a locked or missing DB means 'do not send traffic yet', not 'kill
        the process'. 503 is the signal to hold traffic; the payload names what failed."""
        checks: dict[str, str] = {}
        ok = True
        try:
            conn = self._conn()
            try:
                conn.execute("SELECT 1")
            finally:
                conn.close()
            checks["db"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["db"] = f"error: {exc}"
            ok = False
        try:
            load_criteria(self.server.criteria_path)
            checks["criteria"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["criteria"] = f"error: {exc}"
            ok = False
        return {"status": "ready" if ok else "unready", "checks": checks}, (200 if ok else 503)

    def _companies(self):
        """companies.yaml, for the tier chips. Never fatal.

        The applications page must open for someone who applied to five companies none
        of which are on the target list — that is the whole point of manual entry. A
        missing or unreadable file costs the tier badge and nothing else.
        """
        try:
            return config.load_companies(self.server.companies_path)
        except Exception:  # noqa: BLE001
            log.warning("could not load companies.yaml; tier chips will be omitted")
            return []

    def _render_dashboard(self) -> str:
        """The existing dashboard, regenerated live. Unchanged code, unchanged output."""
        conn = self._conn()
        try:
            companies = config.load_companies(self.server.companies_path)
            criteria = load_criteria(self.server.criteria_path)
            # interactive=True: only the served page gets the disposition buttons,
            # because only here is there something for them to POST to.
            page = dashboard_mod.build_dashboard(
                conn, companies, _today(), criteria, interactive=True,
                view_url=config.BROWSER_VIEW_URL,
            )
        finally:
            conn.close()
        return page.replace("</h1>", "</h1>" + _NAV, 1)

    # -- write endpoints -----------------------------------------------------------
    def _api_decision(self, payload: dict) -> dict:
        company = str(payload.get("company") or "")
        job_id = str(payload.get("ats_job_id") or "")
        decision = str(payload.get("decision") or "")
        if decision not in ("match", "reject"):
            return {"ok": False, "error": "decision must be match or reject"}

        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT title, location FROM postings WHERE company=? AND ats_job_id=?",
                (company, job_id),
            ).fetchone()
            if row is None:
                return {"ok": False, "error": "no such posting"}
            # A reject on a rule-`match` is a false positive — the same quality signal
            # the CLI `decide` emits. Counter lives in cli.py; import lazily to avoid a
            # cycle (cli imports this module). Read the standing verdict before overwriting.
            if decision == "reject":
                prior = conn.execute(
                    "SELECT verdict FROM verdicts WHERE company=? AND ats_job_id=?",
                    (company, job_id),
                ).fetchone()
                if prior and prior["verdict"] == "match":
                    from .cli import matches_rejected_total

                    matches_rejected_total.add(1)
            now = datetime.now().isoformat(timespec="seconds")
            store.record_decision(
                conn, company, job_id, row["title"], decision, now,
                location=row["location"] or "", note=str(payload.get("note") or ""),
            )
            # Pin it as well. The decision feeds `eval`; the override holds the
            # verdict, so a later rule change cannot silently undo a call you made
            # by hand.
            store.set_override(conn, company, job_id, decision, now, reason="manual")
            store.record_verdict(
                conn,
                Verdict(company, job_id, Decision(decision), "override:manual", "human"),
                now,
            )
            conn.commit()
            n = store.decision_count(conn)
        finally:
            conn.close()
        return {"ok": True, "decisions": n}

    def _api_disposition(self, payload: dict) -> dict:
        """Act on one of today's picks: applied, skipped, or snoozed.

        This is what makes the top 3 a queue rather than a rolling sample. A pick holds
        its slot until it gets one of these, so nothing you meant to apply to falls off
        unseen, and tomorrow does not just repeat today.

        'applied' goes to `applications`, not `deferrals` — it is not a deferral, it is
        the start of the outer loop that table already tracks.
        """
        company = str(payload.get("company") or "")
        job_id = str(payload.get("ats_job_id") or "")
        action = str(payload.get("action") or "")
        if action not in ("applied", "skipped", "snoozed"):
            return {"ok": False, "error": "action must be applied, skipped, or snoozed"}

        # `payload.get("days") or 7` would be wrong: 0 is falsy, so an explicit
        # "snooze for 0 days" would silently become a 7-day snooze.
        raw_days = payload.get("days")
        try:
            days = 7 if raw_days is None else int(raw_days)
        except (TypeError, ValueError):
            return {"ok": False, "error": "days must be a number"}
        if days < 1:
            return {"ok": False, "error": "days must be at least 1"}

        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT title, url, location FROM postings "
                "WHERE company=? AND ats_job_id=?",
                (company, job_id),
            ).fetchone()
            if row is None:
                return {"ok": False, "error": "no such posting"}

            now = datetime.now().isoformat(timespec="seconds")
            note = str(payload.get("note") or "")
            if action == "applied":
                # advance_, not record_: this is the first thing that happened to the
                # application, and it belongs in the history like every later stage.
                # url/location are copied across now because the posting row is pruned
                # when the req closes, which is exactly when "where did I apply?" still
                # needs answering.
                store.advance_application(
                    conn, company, job_id, row["title"], "applied", now, note=note,
                    url=row["url"], location=row["location"], source="tracked",
                    next_action=apps_mod.default_next_action(_today()),
                    next_action_note="follow up",
                )
                from .cli import applications_total

                applications_total.add(1)
                detail = "applied"
            elif action == "skipped":
                store.set_deferral(conn, company, job_id, "skipped", now, note=note)
                detail = "skipped"
            else:
                until = (date.fromisoformat(_today()) + timedelta(days=days)).isoformat()
                store.set_deferral(
                    conn, company, job_id, "snoozed", now, until=until, note=note
                )
                detail = f"snoozed until {until}"

            conn.commit()
            # Hand back the next pick so the card can be replaced without a reload.
            remaining = rank_mod.top_n(store.ranked_matches(conn), 3, _today())
            nxt = [
                {"company": r["company"], "ats_job_id": r["ats_job_id"],
                 "title": r["title"], "score": r["score"]}
                for r in remaining
            ]
        finally:
            conn.close()
        log.info("%s %s/%s", detail, company, job_id)
        return {"ok": True, "detail": detail, "top": nxt}

    def _api_application(self, payload: dict) -> dict:
        """Create an application, or move one to a new stage. Always logs an event.

        Two modes, told apart by whether an `ats_job_id` arrives:

        * **Tracked** — an id is given, and the posting must exist, the same check
          `_api_disposition` makes. This is the path the dashboard's "I applied" button
          and any later edit of that row take.
        * **Manual** — no id, so one is minted from the title. This is the whole reason
          the page exists: a referral or a job off LinkedIn has no board, no verdict and
          no posting row, and every existing write path refuses it.

        Nothing is written unless every field validates. A half-recorded application is
        worse than a refused one — you would believe it was saved.
        """
        company = str(payload.get("company") or "").strip()
        title = str(payload.get("title") or "").strip()
        job_id = str(payload.get("ats_job_id") or "").strip()
        status = str(payload.get("status") or "applied").strip()
        note = str(payload.get("note") or "").strip()

        if not company:
            return {"ok": False, "error": "company is required"}
        if status not in store.APPLICATION_STATUSES:
            return {"ok": False, "error":
                    f"status must be one of {', '.join(store.APPLICATION_STATUSES)}"}

        # An empty string means "clear it"; absent means "leave it alone". A date that
        # does not parse is a refusal, never stored raw — text sorted against real dates
        # would silently never come due.
        next_action, err = _optional_day(payload, "next_action")
        if err:
            return {"ok": False, "error": err}
        next_action_note = _optional_text(payload, "next_action_note")
        url = _optional_text(payload, "url")
        location = _optional_text(payload, "location")
        if url and dashboard_mod._safe_url(url) == "#":
            return {"ok": False, "error": "link must be an http(s) URL"}

        conn = self._conn()
        try:
            if job_id:
                existing = store.get_application(conn, company, job_id)
                row = conn.execute(
                    "SELECT title, url, location FROM postings "
                    "WHERE company=? AND ats_job_id=?",
                    (company, job_id),
                ).fetchone()
                if row is None and existing is None:
                    return {"ok": False, "error": "no such posting"}
                if not title:
                    title = (existing or row)["title"]
                source = existing["source"] if existing else "tracked"
                # Carry the posting's own link and location across on first record, so
                # the row still resolves after the req closes and the posting is pruned.
                if row is not None:
                    url = url if url is not None else row["url"]
                    location = location if location is not None else row["location"]
            else:
                if not title:
                    return {"ok": False, "error": "title is required"}
                job_id = store.manual_job_id(title)
                source = "manual"

            now = datetime.now().isoformat(timespec="seconds")
            store.advance_application(
                conn, company, job_id, title, status, now, note=note,
                url=url, location=location, source=source,
                next_action=next_action, next_action_note=next_action_note,
            )
            conn.commit()
            from .cli import applications_total

            applications_total.add(1, {"status": status})
        finally:
            conn.close()
        log.info("application %s %s/%s", status, company, job_id)
        return {"ok": True, "detail": f"{company} — {status}", "ats_job_id": job_id}

    def _api_application_meta(self, payload: dict) -> dict:
        """Change the reminder or the note, without logging an event.

        Rescheduling a phone call is not a thing that happened to the application, and
        an event log that records every reschedule stops being a readable history of the
        stages — which is the only thing it is for.
        """
        company = str(payload.get("company") or "").strip()
        job_id = str(payload.get("ats_job_id") or "").strip()
        if not company or not job_id:
            return {"ok": False, "error": "company and ats_job_id are required"}

        next_action, err = _optional_day(payload, "next_action")
        if err:
            return {"ok": False, "error": err}
        next_action_note = _optional_text(payload, "next_action_note")

        conn = self._conn()
        try:
            app = store.get_application(conn, company, job_id)
            if app is None:
                return {"ok": False, "error": "no such application"}
            note = payload.get("note")
            store.record_application(
                conn, company, job_id, app["title"], app["status"],
                datetime.now().isoformat(timespec="seconds"),
                note=str(note) if note is not None else (app["note"] or ""),
                next_action=next_action, next_action_note=next_action_note,
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "detail": "reminder updated"}

    def _api_application_delete(self, payload: dict) -> dict:
        """Remove an application and its history — for a mistyped manual entry."""
        company = str(payload.get("company") or "").strip()
        job_id = str(payload.get("ats_job_id") or "").strip()
        if not company or not job_id:
            return {"ok": False, "error": "company and ats_job_id are required"}
        conn = self._conn()
        try:
            if store.get_application(conn, company, job_id) is None:
                return {"ok": False, "error": "no such application"}
            store.delete_application(conn, company, job_id)
            conn.commit()
        finally:
            conn.close()
        log.info("deleted application %s/%s", company, job_id)
        return {"ok": True, "detail": "deleted"}

    def _api_mail_accept(self, payload: dict) -> dict:
        """Agree with what a message said, and move the application.

        The only path from the mailbox into `applications`. Everything is validated
        before the single commit, so a refusal touches neither table — a half-recorded
        interview round is worse than one you have to enter by hand, because you would
        believe it saved.

        The event note is composed here, from the subject and the date. The model's quote
        stays where it was written, as the evidence on the card: DESIGN.md §8.4's rule
        that no sentence the model composed reaches a field, with your own application
        history as the field.
        """
        message_id = str(payload.get("message_id") or "").strip()
        if not message_id:
            return {"ok": False, "error": "message_id is required"}

        conn = self._conn()
        try:
            proposal = store.get_mail_proposal(conn, message_id)
            if proposal is None:
                return {"ok": False, "error": "no such proposal"}
            if proposal["resolution"] != "pending":
                return {"ok": False,
                        "error": f"already {proposal['resolution']}"}
            if proposal["status"] not in store.APPLICATION_STATUSES:
                return {"ok": False, "error": "unknown status"}

            job_id = (str(payload.get("ats_job_id") or "").strip()
                      or proposal["ats_job_id"] or "")
            if not job_id:
                return {"ok": False,
                        "error": "choose which application this is about"}
            app = store.get_application(conn, proposal["company"], job_id)
            if app is None:
                return {"ok": False, "error": "no such application"}

            cand = conn.execute(
                "SELECT subject, sent_on FROM mail_candidates WHERE message_id=?",
                (message_id,),
            ).fetchone()
            subject = (cand["subject"] if cand else "") or "(no subject)"
            sent_on = (cand["sent_on"] if cand else "") or ""
            note = f"from mail: {subject[:80]}" + (f" ({sent_on})" if sent_on else "")

            now = datetime.now().isoformat(timespec="seconds")
            store.advance_application(
                conn, proposal["company"], job_id, app["title"],
                proposal["status"], now, note=note,
            )
            store.resolve_mail_proposal(conn, message_id, "accepted", now)
            conn.commit()
            from .cli import applications_total

            applications_total.add(1, {"status": proposal["status"]})
        finally:
            conn.close()
        log.info("accepted mail proposal: %s -> %s", proposal["company"],
                 proposal["status"])
        return {"ok": True, "status": proposal["status"]}

    def _api_mail_dismiss(self, payload: dict) -> dict:
        """Disagree. The row stays, marked dismissed — deleting it is what would let the
        next scan propose the same message all over again."""
        message_id = str(payload.get("message_id") or "").strip()
        if not message_id:
            return {"ok": False, "error": "message_id is required"}
        conn = self._conn()
        try:
            proposal = store.get_mail_proposal(conn, message_id)
            if proposal is None:
                return {"ok": False, "error": "no such proposal"}
            if proposal["resolution"] != "pending":
                return {"ok": False, "error": f"already {proposal['resolution']}"}
            store.resolve_mail_proposal(
                conn, message_id, "dismissed",
                datetime.now().isoformat(timespec="seconds"),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "detail": "dismissed"}

    def _api_rule(self, payload: dict) -> dict:
        phrase = str(payload.get("phrase") or "").strip()
        key = str(payload.get("list") or "exclude_titles")
        if not phrase:
            return {"ok": False, "error": "empty phrase"}
        if key not in _LIST_KEYS:
            return {"ok": False, "error": f"unknown criteria list {key!r}"}

        path = Path(self.server.criteria_path)
        data = yaml.safe_load(path.read_text()) or {}
        existing = [str(t) for t in (data.get(key) or [])]
        if phrase.lower() in {t.lower() for t in existing}:
            return {"ok": False, "error": "already present"}
        data[key] = existing + [phrase]

        # Write a candidate, parse *that*, and only then swap. Validating after the
        # fact would leave a broken criteria.yaml on disk for the next run to hit.
        # The four steps live in safewrite.py now, shared with the answers writer.
        try:
            safewrite.write_yaml(path, data, load_criteria)
        except safewrite.RefusedWrite as exc:
            return {"ok": False, "error": f"refused invalid criteria: {exc}"}
        log.info("added %r to %s", phrase, key)
        return {"ok": True, "phrase": phrase, "list": key}

    def _api_answer(self, payload: dict) -> dict:
        """Write an answer to a question prefill could not answer.

        Goes through the same candidate-parse-backup-swap path as a criteria edit, and
        for the same reason: `answers.yaml` is loaded by a later run, so a malformed
        write here would surface as a broken prefill rather than as an error now.

        The insertion is text surgery rather than a YAML round trip. A round trip would
        delete every comment in the file, including the stubs you are working through.
        """
        from .answers import insert_answer, load_answers

        key = str(payload.get("question_key") or "").strip()
        value = str(payload.get("value") or "").strip()
        if not key:
            return {"ok": False, "error": "no question_key"}
        if not value:
            return {"ok": False, "error": "an empty answer is not an answer"}

        path = Path(self.server.answers_path)
        if not path.exists():
            return {"ok": False, "error": f"no answer bank at {path}"}

        conn = self._conn()
        try:
            gap = conn.execute(
                "SELECT ask FROM prefill_gaps WHERE question_key=?", (key,)
            ).fetchone()
            aliases = [gap["ask"]] if gap else []

            body = insert_answer(path.read_text(), key, value, aliases)
            try:
                safewrite.write_text(path, body, load_answers)
            except safewrite.RefusedWrite as exc:
                return {"ok": False, "error": f"refused invalid answers.yaml: {exc}"}

            store.resolve_gap(conn, key, _today())
            conn.commit()
            remaining = len(store.open_gaps(conn))
        finally:
            conn.close()

        log.info("answered %r", key)
        # Every stored plan was an answer to "what do I know today", and today changed.
        # They rebuild on the next prefill run, mostly without any model call.
        return {"ok": True, "question_key": key, "remaining": remaining}

    def _api_identity(self, payload: dict) -> dict:
        """Write the identity block, creating the answer bank if there is not one.

        This is the bootstrap. Before it existed the only way onto a fresh box was
        copying `answers.example.yaml`, which ships Ada Lovelace's name and email as
        documentation — so the first prefill would have typed a stranger's identity into
        a real application, or (more likely) the copy never happened and `prepare` stayed
        red forever.

        Same candidate-parse-backup-swap path as every other write here. The validator is
        the strict loader the pipeline itself uses, so "it saved" and "the next run can
        read it" are the same statement rather than two hopes.
        """
        from .answers import STARTER, load_answers, upsert_identity

        fields = payload.get("identity")
        if not isinstance(fields, dict):
            return {"ok": False, "error": "no identity fields"}
        fields = {str(k): str(v or "") for k, v in fields.items()}

        path = Path(self.server.answers_path)
        existed = path.exists()
        try:
            existing = path.read_text() if existed else STARTER
            body = upsert_identity(existing, fields)
        except ValueError as exc:  # an unknown identity key, before anything is written
            return {"ok": False, "error": str(exc)}

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            safewrite.write_text(path, body, load_answers)
        except safewrite.RefusedWrite as exc:
            # The loader's own message names what is missing — "identity is missing
            # email" beats anything this layer could invent about it.
            return {"ok": False, "error": f"refused: {exc}"}

        log.info("%s identity in %s", "created" if not existed else "updated", path)
        return {"ok": True, "created": not existed}

    def _api_resume(self, payload: dict) -> dict:
        """Store a resume beside the answer bank and point the `resume:` key at it.

        A resume is the one answer no URL and no cookie can carry — it is attached with
        `set_input_files()` on a live page — so the bytes have to be on disk next to
        `answers.yaml` before anything can be prefilled.

        Order is load-bearing: the file lands first, then the key. `load_answers` refuses
        a `resume:` that is not a real file, so writing the key first would guarantee a
        refused write and leave the upload orphaned.
        """
        from .answers import load_answers, set_resume

        path = Path(self.server.answers_path)
        if not path.exists():
            return {"ok": False, "error": "save your name and email first — the upload "
                                          "needs an answer bank to record the path in"}

        try:
            blob, suffix = resumes.validate_upload(
                payload.get("filename"), payload.get("content_b64")
            )
        except resumes.RefusedUpload as exc:
            return {"ok": False, "error": str(exc)}

        target = path.parent / f"resume{suffix}"
        resumes.write_atomic(target, blob)

        try:
            safewrite.write_text(path, set_resume(path.read_text(), target.name),
                                 load_answers)
        except safewrite.RefusedWrite as exc:
            return {"ok": False, "error": f"refused: {exc}"}

        log.info("resume saved to %s (%d bytes)", target, len(blob))
        return {"ok": True, "filename": target.name, "bytes": len(blob)}

    def _rebuild_plan(self, conn, company: str, ats_job_id: str) -> dict:
        """Re-plan one posting from what is already known. No model, no network.

        This server handles one request at a time, so anything that could block — a
        rate-limited form fetch, a router call with a 180-second timeout — would freeze
        the page for every other tab. So this pass is CPU and SQLite only, and it refuses
        rather than fetching when a form has never been learned.

        That is much less of a downgrade than it sounds. Every key the model has ever
        matched was written onto `form_fields.question_key`, and `known_question_keys`
        replays those as alias hits — the same mechanism `browser.fill_application`
        already relies on. The only thing a full run can still do is ask about a field
        the model has *never seen*, so this pass's gap count can only be equal or higher
        than the nightly one. It can understate readiness; it can never overstate it.

        `PrefillTask.apply` does the writing, so the button and the nightly run cannot
        drift about what a plan is.
        """
        from .tasks.base import TaskContext, TaskUnit
        from .tasks.prefill import PrefillTask, _field_of, plan_from_fields

        row = conn.execute(
            "SELECT title FROM postings WHERE company=? AND ats_job_id=?",
            (company, ats_job_id),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "no such posting"}

        answers, error = _load_answers_quietly(Path(self.server.answers_path))
        if answers is None:
            return {"ok": False, "error": error or
                    "no answer bank yet — fill in your name and email under Settings"}

        cached = store.form_fields_for(conn, company)
        if not cached:
            # Zero fields is "we have never read this form", never "0/0, nothing to do".
            # Absence read as success is the failure DESIGN.md §3.4 exists to prevent.
            return {"ok": False,
                    "error": f"no application form learned for {company} yet — press "
                             f"Open prefilled once and this fills in"}

        base_hash = answers.hash
        answers, override = resumes.effective(conn, answers, company, ats_job_id)
        # `known_question_keys` is what makes a rules-only rebuild worth doing: every
        # key the model has ever matched was written onto `form_fields.question_key`,
        # so its past decisions replay here as alias hits with no call at all.
        alias_map = dict(answers.by_alias)
        alias_map.update(store.known_question_keys(conn))

        result = plan_from_fields(
            [_field_of(r) for r in cached], answers, alias_map, cached[0]["source"]
        )
        unit = TaskUnit(
            task="prefill", company=company, ats_job_id=ats_job_id,
            unit_key=base_hash, title=row["title"],
            payload={"resume_override": override.name if override else None},
        )
        # `ctx.answers` must be the BANK, not the copy: `apply` stores its hash, and the
        # copy's differs by the override's basename.
        bank, _ = _load_answers_quietly(Path(self.server.answers_path))
        PrefillTask().apply(conn, unit, result, TaskContext(today=_today(), answers=bank))
        conn.commit()
        return {
            "ok": True,
            "fields": len(result.entries),
            "gaps": len(result.gaps),
            "resume": (override.name if override else
                       (bank.resume.name if bank and bank.resume else "")),
        }

    def _api_prefill(self, payload: dict) -> dict:
        """Rebuild one posting's plan against the answers and resume as they stand now.

        Deliberately not `task.pending()` filtered down to this posting, which is how
        `prepare` does it: `matches_needing_prefill` excludes a plan whose `answers_hash`
        already matches, so that route returns zero units for a current plan — and the
        button would silently do nothing in exactly the case it exists for. That is the
        apply-to regression's shape, and it is avoided by building the unit directly.
        """
        company = str(payload.get("company") or "")
        job_id = str(payload.get("ats_job_id") or "")
        if not company or not job_id:
            return {"ok": False, "error": "company and ats_job_id are required"}
        conn = self._conn()
        try:
            return self._rebuild_plan(conn, company, job_id)
        finally:
            conn.close()

    def _api_posting_resume(self, payload: dict) -> dict:
        """Attach a resume to one posting. The bank's stays the default for everything else.

        Same validation as `/api/resume` — it is literally the same function — and the
        same ordering rule: the file lands before anything points at it.
        """
        company = str(payload.get("company") or "")
        job_id = str(payload.get("ats_job_id") or "")
        if not company or not job_id:
            return {"ok": False, "error": "company and ats_job_id are required"}

        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT title FROM postings WHERE company=? AND ats_job_id=?",
                (company, job_id),
            ).fetchone()
            if row is None:
                return {"ok": False, "error": "no such posting"}
            try:
                blob, suffix = resumes.validate_upload(
                    payload.get("filename"), payload.get("content_b64")
                )
            except resumes.RefusedUpload as exc:
                return {"ok": False, "error": str(exc)}

            name = resumes.stored_name(company, job_id, suffix)
            resumes.write_atomic(resumes.path_for(name), blob)
            store.set_posting_resume(conn, company, job_id, name, len(blob), _today())
            conn.commit()
            log.info("resume for %s %s saved as %s (%d bytes)",
                     company, job_id, name, len(blob))
            # Re-plan immediately, so the card can say what changed rather than telling
            # you to wait for tonight.
            out = self._rebuild_plan(conn, company, job_id)
            out.setdefault("ok", True)
            out["filename"] = name
            out["bytes"] = len(blob)
            return out
        finally:
            conn.close()

    def _api_posting_resume_clear(self, payload: dict) -> dict:
        """Go back to the answer bank's resume for this posting."""
        company = str(payload.get("company") or "")
        job_id = str(payload.get("ats_job_id") or "")
        if not company or not job_id:
            return {"ok": False, "error": "company and ats_job_id are required"}
        conn = self._conn()
        try:
            row = store.get_posting_resume(conn, company, job_id)
            if row is None:
                return {"ok": False, "error": "this posting has no resume of its own"}
            store.clear_posting_resume(conn, company, job_id)
            conn.commit()
            path = resumes.path_for(row["filename"])
            # Unlink after the row is gone: an orphaned file is inert, while a row
            # pointing at a deleted file is a lookup that has to log every time.
            try:
                path.unlink()
            except OSError:
                log.warning("could not remove %s", path)
            out = self._rebuild_plan(conn, company, job_id)
            out.setdefault("ok", True)
            return out
        finally:
            conn.close()


    def _companies_path(self) -> Path:
        """The file to write. `companies_path` is None unless --companies was passed, and
        reads tolerate that (`load_companies(None)` defaults) while a write cannot."""
        return Path(self.server.companies_path or config.COMPANIES_YAML)

    def _api_company(self, payload: dict) -> dict:
        """Add one company to companies.yaml, verifying the board first.

        Three outcomes, and the middle one is why `ok` alone is not enough:

          ok:false                    the request was wrong — validation, a duplicate, a
                                      file that will not parse. Nothing was written and
                                      there is nothing to retry but the form.
          ok:true, saved:false        the request was fine and the BOARD did not check
                                      out. Nothing was written; the page shows the
                                      evidence and offers to add it anyway. Folding this
                                      into ok:false would make every handler's
                                      `if (!res.ok) alert()` swallow the escape hatch.
          ok:true, saved:true         written, with the diff that was applied.

        This is the one endpoint on this server that opens a socket. It cannot go on a
        daemon thread the way `_api_apply_to` does, because the whole point is that the
        answer decides whether the write happens, and nothing on a thread can answer the
        click that started it. So it stays inline and is bounded instead — see the
        Fetcher call below — and a concurrent one is refused rather than queued.
        """
        from dataclasses import replace

        raw_tier = str(payload.get("tier") or "").strip()
        try:
            tier = int(raw_tier) if raw_tier else None
        except ValueError:
            return {"ok": False, "error": "tier must be a whole number from 1 to 7"}

        def field(key: str) -> str:
            return str(payload.get(key) or "").strip()

        company = Company(
            name=field("name"),
            ats=field("ats"),
            slug=field("slug"),
            tier=tier,
            category=field("category"),
            check_method=field("check_method") or "manual",
            expected_board_name=field("expected_board_name") or None,
            careers_page=field("careers_page"),
            board_url=field("board_url"),
            notes=field("notes"),
        )

        path = self._companies_path()
        try:
            existing = config.load_companies(path) if path.exists() else []
        except Exception as exc:  # noqa: BLE001 — a broken file is not this form's fault
            return {"ok": False, "error": f"companies.yaml did not load: {exc}"}

        errors = curation.validate_new(company, existing)
        if errors:
            # Validation has no "add anyway". It is about whether the entry is coherent;
            # only verification — whether the world agrees — gets an escape hatch.
            return {"ok": False, "error": errors[0], "errors": errors}

        force = bool(payload.get("force"))
        verification = None
        skipped = ""
        if force:
            skipped = "you chose to add it without verifying"
        elif company.check_method != "api":
            # Never fetch a manual entry. That rule predates this page: a bespoke portal
            # or a Workday tenant has no keyless board, and surfacing it for a human to
            # check is correct where pretending to have checked it is not.
            skipped = f"{company.check_method} entries are never fetched"
        else:
            verified, err = self._verify_board(company)
            if err:
                return {"ok": False, "error": err}
            verification = verified
            if not verification.accepted:
                return {
                    "ok": True, "saved": False,
                    "verification": _verification_json(verification),
                }
            company = replace(
                company,
                expected_board_name=verification.board_name or None,
            )

        if skipped:
            # An unverified entry stores no board name. Writing the typed one would make
            # the first nightly run either alert on a name nobody checked or — because
            # identity_matches returns True when either side is empty — quietly "verify"
            # it. Null is the honest state, and `repair` picks it up from there.
            company = replace(company, expected_board_name=None)

        # Re-read AFTER the fetch, not before. Verification can take seconds, and a
        # `repair --write` or a hand edit landing in that window would otherwise be
        # clobbered by a splice computed against stale text — on curated data, with .bak
        # as the only recourse.
        before = path.read_text() if path.exists() else _HEADER
        after = curation.insert_entry(before, company)
        try:
            safewrite.write_text(path, after, config.load_companies)
        except safewrite.RefusedWrite as exc:
            return {"ok": False, "error": f"refused invalid companies.yaml: {exc}"}

        log.info("added %s (%s/%s) to %s", company.name, company.ats, company.slug, path)
        return {
            "ok": True, "saved": True,
            "diff": curation.diff(str(path), before, after),
            "backup": str(path) + ".bak",
            "verification": _verification_json(verification) if verification else None,
            "skipped_because": skipped,
        }

    def _verify_board(self, company):
        """Fetch the candidate board and hold it to `repair`'s rule. Returns
        (Verification, error) — the error is for "could not even try"."""
        from .fetch import Fetcher
        from .repair import judge_board

        if not _VERIFY_LOCK.acquire(blocking=False):
            return None, "a verification is already running — try again in a moment"
        # Bounded on purpose: one worker, an 8s timeout and a single attempt, against the
        # nightly Fetcher's 3 attempts on a 20s timeout. `min_interval` is left alone —
        # the per-host pacing costs 0.34s across two requests and is a politeness property
        # this project does not get to skip because a page is waiting.
        fetcher = Fetcher(max_workers=1, timeout=8, max_retries=1)
        try:
            result = fetcher.fetch_company(company)
        finally:
            fetcher.close()
            _VERIFY_LOCK.release()
        return judge_board(
            company.expected_board_name or company.name,
            company.ats,
            company.slug,
            result,
            # NOT "provenance". Nothing served this slug — it was typed — so there is no
            # careers page backing it and no provenance to claim. See judge_board.
            weak_evidence="reachable",
        ), ""

    def _api_apply_to(self, payload: dict) -> dict:
        """Open one application in a browser, filled in. Returns before it finishes.

        This server handles one request at a time — it is `HTTPServer`, not
        `ThreadingHTTPServer` — so driving a browser inline would freeze the page for
        as long as the window stayed open. The fill runs on a daemon thread and this
        answers immediately.

        Playwright's sync API is used on that thread, which is fine; what it must not do
        is run inside an asyncio loop, and nothing here has one.

        Everything that can be known before the thread starts is checked here and
        returned to the page: the posting, the answer bank, whether there is a browser
        to drive at all, and whether one is already open. Nothing on that thread can
        report to the click that started it — an exception there reaches the log and
        nowhere else — so a failure this method could have seen would show up as a
        button sitting on "Opening…" forever.
        """
        company_name = str(payload.get("company") or "")
        job_id = str(payload.get("ats_job_id") or "")
        if not company_name or not job_id:
            return {"ok": False, "error": "company and ats_job_id are required"}

        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT url, title FROM postings WHERE company=? AND ats_job_id=?",
                (company_name, job_id),
            ).fetchone()
            if row is None:
                return {"ok": False, "error": "no such posting"}
            plan = store.get_plan(conn, company_name, job_id)
            plan_json = plan["plan"] if plan else None
            url = row["url"]
            override = resumes.override_for(conn, company_name, job_id)
        finally:
            conn.close()

        companies = {c.name: c for c in config.load_companies(self.server.companies_path)}
        company = companies.get(company_name)
        if company is None:
            return {"ok": False, "error": f"{company_name} is not in companies.yaml"}

        answers, error = _load_answers_quietly(Path(self.server.answers_path))
        if answers is None:
            # Named, with the way out of it: on a fresh box there is no answer bank at
            # all, and "no usable answer bank" on a card reads as a defect rather than
            # as the one setup step nobody has done yet.
            return {"ok": False, "error": error or (
                f"no answer bank at {self.server.answers_path} — add your name and "
                "email on the Settings tab to create one")}

        # Both halves of the override, and both are needed. The replaced `Answers` covers
        # DOM fields no stored plan ever saw; `retarget_resume` covers the ones it did,
        # because `browser._plan_index` lets a stored plan value win over a fresh
        # `resolve_field`. Applied identically in `cli.cmd_apply_to`, so the button and
        # the terminal cannot disagree about which file goes out under your name.
        if override is not None:
            from dataclasses import replace as _replace

            from .tasks.prefill import retarget_resume

            answers = _replace(answers, resume=override)
            plan_json = retarget_resume(plan_json, str(override))

        from . import browser as browser_mod

        reason = browser_mod.unavailable_reason()
        if reason:
            return {"ok": False, "error": reason}

        # One window at a time. The browser profile is a single directory and Chromium
        # locks it, so a second launch while the first window is open fails — on the
        # worker thread, where nobody sees it. Saying so is the honest answer.
        if not _APPLY_LOCK.acquire(blocking=False):
            # Name the job and say where the way out is. The window is on *this* machine,
            # which on a headless host is nowhere you can click — so "close it first"
            # used to be advice with no way to take it, and the honest half of the
            # sentence is the second one.
            open_now = live.current()
            held = f" for {open_now.title}" if open_now is not None else ""
            return {"ok": False,
                    "error": f"a prefilled window is already open{held} — finish it on "
                             "the Fill in page, or press Done there to close it"}

        # The page the click is about to land on reads this, so it exists before the
        # thread does — a session created on the worker would leave `/apply` reporting
        # "no window is open" for the first second of every session.
        session = live.start(company_name, job_id, row["title"], url)

        def _run() -> None:
            worker_conn = store.connect(self.server.db_path)
            try:
                browser_mod.fill_application(
                    worker_conn,
                    company=company,
                    ats_job_id=job_id,
                    url=url,
                    answers=answers,
                    today=_today(),
                    user_data_dir=config.BROWSER_PROFILE,
                    plan_json=plan_json,
                    headless=False,
                    # No terminal here, so there is no Enter to wait on — but the
                    # window still has to outlive the fill, and closing the context is
                    # what ends it. `hold` blocks this thread until you close the
                    # browser yourself; without it the window opens and vanishes.
                    wait=False,
                    hold=True,
                    # Mirror the form into the page rather than leaving it to be typed
                    # through a video stream of this window. `hold` still governs how
                    # long the window lives; this governs what you type into.
                    session=session,
                )
            except Exception:  # noqa: BLE001 — a browser failure must not kill serve
                log.exception("apply-to %s/%s failed", company_name, job_id)
                session.set_phase(live.CLOSED, "the window could not be opened — "
                                               "see the server log")
            finally:
                session.set_phase(live.CLOSED)
                worker_conn.close()
                _APPLY_LOCK.release()

        threading.Thread(
            target=_run, name=f"jobtracker-apply-{job_id}", daemon=True
        ).start()
        # Where to go to fill it in. The button navigates rather than relabelling
        # itself: the form is now typed on that page, not in the window.
        return {"ok": True, "href": "/apply",
                "detail": f"opening {row['title'][:60]}…"}

    # -- the live form -------------------------------------------------------------
    #
    # Five small endpoints, and every one of them returns without touching a browser.
    # That is not a style choice: this is `HTTPServer`, one request in flight, and the
    # browser lives on a daemon thread that owns its Playwright objects exclusively. So
    # a write here queues a command and answers immediately, and the outcome arrives on
    # the next poll — the same shape `_api_apply_to` already has, for the same reason.
    def _api_session(self) -> dict:
        """The poll. Also what tells the browser thread somebody is still watching."""
        session = live.current()
        if session is None:
            return {"ok": False, "error": "no window is open"}
        session.watch()
        return {"ok": True, "session": session.snapshot()}

    def _send_preview(self) -> None:
        session = live.current()
        blob = session.shot if session is not None else None
        if blob is None:
            # Not an error worth a body: for the first couple of seconds of a session
            # there genuinely is no picture yet, and the page just tries again.
            self._send_bytes(b"", "image/jpeg", 404)
            return
        self._send_bytes(blob, "image/jpeg")

    def _api_session_close(self) -> dict:
        """Close the window `serve` is holding, from the page that mirrors it.

        Not a `live.Command`: the vocabulary is what a request may do to the *form*, and
        this does nothing to the form. It sets a flag the browser thread reads in the
        poll it is already doing, which then ends the hold, closes the context and
        releases the one-window lock.

        Deliberately not conditional on the phase. A session stuck part-way through
        filling is exactly when this is needed, and refusing it then would leave the lock
        held for the rest of the process's life — which is the defect it exists to fix.
        """
        session = live.current()
        if session is None:
            return {"ok": False, "error": "no window is open"}
        session.request_close()
        return {"ok": True, "detail": "closing the window…"}

    def _api_session_command(self, kind: str, handle: str = "",
                             value: str = "", epoch: int = -1) -> dict:
        session = live.current()
        if session is None:
            return {"ok": False, "error": "no window is open"}
        command = live.Command(kind=kind, handle=handle, value=value, epoch=epoch)
        if not session.submit(command):
            return {"ok": False, "error": f"cannot {kind} — the window is closed"}
        return {"ok": True}

    def _api_session_set(self, payload: dict) -> dict:
        """Put one value in one field of the live form.

        The payload is a handle, a value and the epoch it was written against — no
        selector, and nothing the browser thread evaluates. The epoch is carried rather
        than checked here on purpose: only the browser thread knows whether the form has
        been re-read since, and checking it anywhere else is a race.
        """
        handle = str(payload.get("handle") or "")
        if not handle:
            return {"ok": False, "error": "no field"}
        try:
            epoch = int(payload.get("epoch", -1))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad epoch"}
        value = str(payload.get("value") or "")
        if not value:
            # `clear` is a different act with a different outcome, and the page knows
            # which one it means. Guessing here would make an empty text box and a
            # deliberate erasure arrive as the same request.
            return {"ok": False, "error": "an empty value is a clear, not a set"}
        return self._api_session_command(live.SET, handle, value, epoch)

    def _api_session_clear(self, payload: dict) -> dict:
        """Empty one field of the live form.

        Same shape as `_api_session_set` with no value to carry, and the same epoch rule:
        clearing the wrong field is exactly as bad as filling it, so the check stays on
        the browser thread where it cannot be bypassed.
        """
        handle = str(payload.get("handle") or "")
        if not handle:
            return {"ok": False, "error": "no field"}
        try:
            epoch = int(payload.get("epoch", -1))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad epoch"}
        return self._api_session_command(live.CLEAR, handle, "", epoch)

    def _api_session_file(self, payload: dict) -> dict:
        """Attach a file to a file field of the live form.

        The browser runs on *this* host, so its file picker shows this host's disk, not
        yours. This upload is the file transfer — the same reason the per-posting resume
        travels as base64 through the one JSON POST path this server has.

        Validation, naming and the write are `resumes`', unchanged: suffix allowlist
        before decode, magic bytes after, and a name minted here rather than taken from
        the client.
        """
        session = live.current()
        if session is None:
            return {"ok": False, "error": "no window is open"}
        handle = str(payload.get("handle") or "")
        if not handle:
            return {"ok": False, "error": "no field"}
        try:
            epoch = int(payload.get("epoch", -1))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad epoch"}
        try:
            blob, suffix = resumes.validate_upload(
                str(payload.get("filename") or ""), str(payload.get("content") or "")
            )
        except resumes.RefusedUpload as exc:
            return {"ok": False, "error": str(exc)}

        name = resumes.stored_name(session.company, f"{session.ats_job_id}-{handle}",
                                   suffix)
        path = resumes.path_for(name)
        resumes.write_atomic(path, blob)
        out = self._api_session_command(live.SET, handle, str(path), epoch)
        out["filename"] = name
        return out

    def _api_rematch(self) -> dict:
        """Re-apply criteria to every open posting — same logic as `jobtracker rematch`."""
        criteria = load_criteria(self.server.criteria_path)
        conn = self._conn()
        try:
            overrides = store.load_overrides(conn)
            before = store.counts_by_verdict(conn)
            now = _today()
            rows = conn.execute(
                "SELECT company, ats_job_id, title, location, url FROM postings "
                "WHERE closed_at IS NULL"
            ).fetchall()
            for r in rows:
                posting = Posting(r["company"], r["ats_job_id"], r["title"],
                                  r["url"], r["location"] or "")
                store.record_verdict(
                    conn, tuning.apply_override(match(posting, criteria), overrides), now
                )
            conn.commit()
            after = store.counts_by_verdict(conn)
        finally:
            conn.close()
        return {"ok": True, "before": before, "after": after, "postings": len(rows)}


def _today() -> str:
    return date.today().isoformat()



def _verification_json(v) -> dict:
    """A Verification as the page needs it. `evidence_kind` travels verbatim so the page
    can say `reachable` rather than implying identity was checked when it was not."""
    return {
        "accepted": v.accepted,
        "reason": v.reason,
        "evidence_kind": v.evidence_kind,
        "job_count": v.job_count,
        "board_name": v.board_name,
        "sample_titles": list(v.sample_titles),
    }


def _optional_text(payload: dict, key: str) -> Optional[str]:
    """None when the field is absent, the stripped string when it is present.

    The distinction is load-bearing: `record_application` writes optional fields through
    COALESCE, so None means "leave what is stored" and "" means "clear it". Collapsing
    the two would make every partial edit wipe the fields it did not mention.
    """
    if key not in payload or payload[key] is None:
        return None
    return str(payload[key]).strip()


def _optional_day(payload: dict, key: str) -> tuple[Optional[str], Optional[str]]:
    """(value, error). An absent key is None; a blank clears; anything else must parse."""
    raw = _optional_text(payload, key)
    if raw is None or raw == "":
        return raw, None
    parsed = apps_mod.parse_day(raw)
    if parsed is None:
        return None, f"{key.replace('_', ' ')} must be a date like 2026-08-16"
    return parsed, None


_NAV = (
    '<nav class=nav><a href="/">Dashboard</a> · <a href="/applications">Applications</a>'
    ' · <a href="/companies">Companies</a> · <a href="/tuning">Tuning</a>'
    ' · <a href="/settings">Settings</a></nav>'
)

_SETTINGS_CSS = """
.gap{padding:.7rem .9rem;margin:.5rem 0;border-left:3px solid #d97706;background:rgba(217,119,6,.07)}
.gap .ask{font-weight:600;margin-bottom:.2rem}
.gap .row{display:flex;gap:.5rem;margin-top:.5rem}
.gap input.answer{flex:1;padding:.3rem .5rem;border-radius:5px;
border:1px solid currentColor;background:transparent;color:inherit;font:inherit}
.card{padding:.8rem .9rem;margin:.5rem 0;border:1px solid rgba(127,127,127,.35);border-radius:6px}
.card .row{display:flex;gap:.5rem;margin-top:.8rem;align-items:center}
.field{display:flex;align-items:center;gap:.6rem;margin:.25rem 0}
.field>span{flex:0 0 11rem;font-size:.9rem;opacity:.85}
.field input{flex:1;padding:.3rem .5rem;border-radius:5px;
border:1px solid currentColor;background:transparent;color:inherit;font:inherit}
.req{color:#d97706;font-size:.75rem;text-transform:uppercase;letter-spacing:.04em}
"""

# Only the editable half. Everything that decides how an application *looks* — the
# status pills, the timeline grid, the urgency rules — lives in dashboard._CSS, which
# this page also loads, so the read-only tab and this page cannot drift apart.
_APPS_CSS = """
.addapp{padding:.9rem 1rem;margin:.6rem 0 1.4rem;border:1px solid rgba(127,127,127,.35);
border-radius:8px}
.addapp .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
gap:.6rem;margin-top:.7rem}
.addapp label{display:flex;flex-direction:column;gap:.25rem;font-size:.82rem;opacity:.85}
.appform input,.appform select,.addapp input,.addapp select{padding:.35rem .5rem;
border-radius:5px;border:1px solid currentColor;background:transparent;color:inherit;
font:inherit;font-size:.88rem;min-width:0}
.appform{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:.5rem;margin-top:.75rem;align-items:end}
.appform label{display:flex;flex-direction:column;gap:.25rem;font-size:.78rem;opacity:.8}
.appform .acts{display:flex;gap:.4rem;align-items:end}
.app .danger{border-color:rgba(220,53,69,.6);opacity:.75}
.app .danger:hover{opacity:1;color:#dc3545}
/* A proposal from the mailbox. Marked as unwritten — the border is the reminder that
   nothing on this card has touched your application history yet. */
.app.prop{border-left:3px solid var(--accent)}
.app.prop .subj{font-weight:600;margin-top:.35rem}
.app.prop .quote{margin:.4rem 0;padding-left:.7rem;border-left:2px solid var(--rule);
font-size:.88rem;color:var(--ink-2)}
.app.prop .row{display:flex;gap:.4rem;margin-top:.6rem}
.app.prop select{margin-left:.4rem}
"""

_APPLY_CSS = """
.split{display:grid;grid-template-columns:minmax(280px,1fr) minmax(320px,1.1fr);
gap:1.2rem;align-items:start}
@media (max-width:820px){.split{grid-template-columns:1fr}}
.pane{min-width:0}
.phead{display:flex;gap:.6rem;align-items:center;font-weight:600;margin:.2rem 0 .6rem}
.phead .ago{font-weight:400;font-size:.8rem;opacity:.7;margin-left:auto}
#preview{border:1px solid rgba(127,127,127,.35);border-radius:6px;
background:rgba(127,127,127,.08);display:block;min-height:120px;margin:0 auto}
/* Two ways to look at a page several thousand pixels tall. `fit` scales all of it into
   view — every field at once, the state of the form at a glance. `actual` is full width
   and legible, and it is the *box* that scrolls, never the document, so the field list
   beside it does not move while you look. */
#shotbox{max-height:78vh;overflow:auto}
#preview.fit{width:auto;max-width:100%;max-height:78vh;object-fit:contain;
cursor:zoom-in}
#preview.actual{width:100%;height:auto;cursor:zoom-out}
#zoom{display:none}
.js-zoom #zoom{display:inline-block}
.who{margin:.2rem 0 .1rem}
.phase{margin:.1rem 0 1rem;font-size:.9rem}
.phase .pill{padding:.1rem .5rem;border-radius:99px;background:rgba(127,127,127,.18);
font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
.lf{padding:.55rem .7rem;margin:.45rem 0;border:1px solid rgba(127,127,127,.3);
border-radius:6px}
.lf .lab{font-size:.86rem;margin-bottom:.35rem;display:flex;gap:.45rem;
align-items:baseline;flex-wrap:wrap}
.lf .lv,.lf input[type=file]{width:100%;padding:.35rem .5rem;border-radius:5px;
border:1px solid currentColor;background:transparent;color:inherit;font:inherit;
font-size:.9rem}
.lf textarea.lv{resize:vertical}
.lf .cbx{display:flex;gap:.4rem;align-items:center;font-size:.9rem}
.lf .cbx .lv{width:auto}
.lf .bank{display:flex;gap:.4rem;align-items:center;margin-top:.45rem;font-size:.78rem;
opacity:.85;flex-wrap:wrap}
.lf .bank .bankkey{flex:1;min-width:8rem;padding:.15rem .35rem;border-radius:4px;
border:1px solid currentColor;background:transparent;color:inherit;font:inherit;
font-size:.78rem}
.lf .bank input[type=checkbox]{width:auto}
/* Status is a word, not a colour: a red dot alone does not say whether the field is
   waiting on you or was refused by the page, and those need different actions. */
.st{margin-left:auto;font-size:.74rem;text-transform:uppercase;letter-spacing:.04em;
padding:.05rem .4rem;border-radius:99px}
.st-filled{color:var(--good);background:rgba(25,135,84,.14)}
.st-gap{color:#d97706;background:rgba(217,119,6,.14)}
.st-refused{color:#dc3545;background:rgba(220,53,69,.14)}
.st-pending{opacity:.6;background:rgba(127,127,127,.14)}
.lf.busy{opacity:.6}
.viewwin{font-weight:600}
"""

_EXTRA_CSS = """
.nav{margin:0 0 1rem;font-size:.9rem}
.banner{padding:.6rem .8rem;border-radius:6px;font-weight:600}
.banner.ok{background:#0f5132;color:#d1e7dd}
.banner.bad{background:#58151c;color:#f8d7da}
.regression{padding:.4rem .8rem;border-left:3px solid #dc3545;margin:.3rem 0}
.sugg{padding:.4rem 0;display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
.rules{margin:.9rem 0;padding:.1rem 0 .6rem;border-top:1px solid var(--rule)}
.rules h3{margin:.6rem 0 .2rem;font-size:1rem;font-weight:600}
.rules h3 small{opacity:.6;font-weight:400}
.rules .note{margin:.2rem 0 .5rem}
.chips{display:flex;gap:.3rem;flex-wrap:wrap;margin:.3rem 0 .5rem}
.chip{padding:.1rem .45rem;border:1px solid var(--rule);border-radius:10px;
font-size:.82rem;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.rules.reject .chip{border-color:#dc3545}
.rules.accept .chip{border-color:#0f5132}
.addbox{display:flex;gap:.4rem;align-items:center;flex-wrap:wrap}
.addbox input{padding:.25rem .5rem;border-radius:5px;border:1px solid var(--rule);
background:transparent;color:inherit;font:inherit;min-width:16rem}
select{padding:.2rem .4rem;border-radius:5px;border:1px solid currentColor;
background:transparent;color:inherit;font:inherit}
.note{opacity:.75;font-size:.9rem}
button{cursor:pointer;padding:.25rem .6rem;border-radius:5px;border:1px solid currentColor;
background:transparent;color:inherit;font:inherit}
button:hover{opacity:.7}
"""

# Every control on the apply page has its handler here, in the file that renders it.
# That rule is not stylistic: "Open prefilled" once had its markup in dashboard.py and
# its handler in server._JS, which the dashboard never loads, so every click did nothing
# at all and nothing logged. There is a test asserting these two lists match.
#
# Values are read out of the DOM the server rendered. Nothing here builds markup.
_APPLY_JS = """
(function () {
  var root = document.querySelector('.phase');
  if (!root) return;                       // no session; the page is just a note

  var POLL_MS = 2000;
  var DEBOUNCE_MS = 400;
  var epoch = parseInt(root.dataset.epoch, 10);
  var paused = false;
  var moved = document.getElementById('moved');
  var img = document.getElementById('preview');

  function post(url, body) {
    return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                       body: JSON.stringify(body || {})}).then(function (r) {
      return r.json();
    });
  }

  // -- pushing a value ---------------------------------------------------------------
  // The value goes with the epoch it was typed against. The browser thread drops it if
  // the form has been read again since, because the handle would by then name a
  // different input — see live.py. So a refusal here is a correct refusal.
  //
  // An empty value is a clear, not a set, and it goes to its own endpoint. Sending it as
  // a set would land on the real page as a field holding nothing while the row read
  // "filled" — done, and out of the "need you" count, which is the one number on this
  // page that has to stay honest.
  function push(card, value) {
    var st = card.querySelector('.st');
    setStatus(st, 'pending', '…');
    card.classList.add('busy');
    var body = {handle: card.dataset.handle, epoch: epoch};
    var url = '/api/session/clear';
    if (value) { url = '/api/session/set'; body.value = value; }
    return post(url, body).then(function (res) {
      card.classList.remove('busy');
      if (!res.ok) setStatus(st, 'refused', res.error || 'refused');
      return res;
    }).catch(function () {
      card.classList.remove('busy');
      setStatus(st, 'refused', 'the server did not answer');
    });
  }

  function setStatus(st, kind, word) {
    if (!st) return;
    st.className = 'st st-' + kind;
    st.textContent = word;
  }

  // Also answer it everywhere, if you said so. Same endpoint the Settings tab writes
  // through — there is no second way into the answer bank.
  function bank(card, value) {
    var tick = card.querySelector('.tobank');
    if (!tick || !tick.checked || !value) return;
    var key = card.querySelector('.bankkey');
    post('/api/answer', {question_key: key ? key.value : '', value: value})
      .then(function (res) { if (res.ok) tick.checked = false; });
  }

  var timers = {};
  function schedule(card, value) {
    var handle = card.dataset.handle;
    if (timers[handle]) clearTimeout(timers[handle]);
    timers[handle] = setTimeout(function () {
      delete timers[handle];
      push(card, value).then(function () { bank(card, value); });
    }, DEBOUNCE_MS);
  }

  document.addEventListener('input', function (e) {
    var card = e.target.closest('.lf');
    if (!card || !e.target.classList.contains('lv')) return;
    if (card.dataset.type === 'select' || card.dataset.type === 'multiselect') return;
    if (card.dataset.type === 'checkbox') return;
    schedule(card, e.target.value);
  });

  // Blur beats the timer: leaving a field is the clearest statement that you are done
  // with it, and waiting out the debounce after that just looks like lag.
  document.addEventListener('focusout', function (e) {
    var card = e.target.closest('.lf');
    if (!card || !e.target.classList.contains('lv')) return;
    var handle = card.dataset.handle;
    if (!timers[handle]) return;
    clearTimeout(timers[handle]);
    delete timers[handle];
    push(card, e.target.value).then(function () { bank(card, e.target.value); });
  }, true);

  // A dropdown or a tickbox is one decision, so it goes at once rather than on a timer.
  document.addEventListener('change', function (e) {
    var card = e.target.closest('.lf');
    if (!card) return;
    if (e.target.classList.contains('lv')) {
      if (card.dataset.type === 'checkbox') {
        push(card, e.target.checked ? 'yes' : '');
      } else if (card.dataset.type === 'select' ||
                 card.dataset.type === 'multiselect') {
        // Including the blank "— choose —" option, which is how you take a dropdown
        // answer back. Ignoring it left the one control on this page you could not
        // change your mind about.
        var v = e.target.value;
        push(card, v).then(function () { bank(card, v); });
      }
      return;
    }
    // The browser's file picker shows the *server's* disk, not yours. So the file
    // travels as base64 through the one JSON POST path this server has, exactly like
    // the per-posting resume does.
    if (e.target.classList.contains('lf-file') && e.target.files.length) {
      var file = e.target.files[0];
      var st = card.querySelector('.st');
      setStatus(st, 'pending', 'uploading…');
      var reader = new FileReader();
      reader.onload = function () {
        post('/api/session/file', {
          handle: card.dataset.handle, epoch: epoch, filename: file.name,
          content: String(reader.result).split(',')[1] || ''
        }).then(function (res) {
          if (!res.ok) setStatus(st, 'refused', res.error || 'refused');
        });
      };
      reader.readAsDataURL(file);
    }
  });

  // Detaching. A file input offers no way to hold nothing once it holds something, so
  // this is the only way the resume comes back off — and the picker is reset too, or it
  // would keep reporting the file that is no longer on the form.
  document.addEventListener('click', function (e) {
    if (!e.target.classList.contains('lf-detach')) return;
    var card = e.target.closest('.lf');
    if (!card) return;
    var picker = card.querySelector('.lf-file');
    if (picker) picker.value = '';
    push(card, '');
  });

  // -- the two buttons ---------------------------------------------------------------
  document.getElementById('reread').addEventListener('click', function () {
    post('/api/session/rediscover', {});
  });

  // Fit or full size. Entirely on this side of the wire: the shot is always the whole
  // page, so this costs no request, no command and no session state — it is two classes.
  var zoom = document.getElementById('zoom');
  document.body.classList.add('js-zoom');
  function setZoom(fit) {
    img.className = fit ? 'fit' : 'actual';
    zoom.dataset.fit = fit ? '1' : '0';
    zoom.textContent = fit ? '100%' : 'Fit';
  }
  zoom.addEventListener('click', function () {
    setZoom(zoom.dataset.fit !== '1');
  });
  img.addEventListener('click', function () {
    setZoom(zoom.dataset.fit !== '1');
  });

  var pause = document.getElementById('pause');
  pause.addEventListener('click', function () {
    paused = !paused;
    pause.textContent = paused ? 'Resume' : 'Pause';
    pause.dataset.paused = paused ? '1' : '0';
  });

  var reload = document.getElementById('reload');
  if (reload) reload.addEventListener('click', function () { location.reload(); });

  // Done. Closes the window this page mirrors, which is what frees the one-window lock
  // — there is no other way to end a session from a machine that is not the one the
  // browser is drawing on. It closes a browser; it cannot send anything.
  var closewin = document.getElementById('closewin');
  var donemsg = document.getElementById('donemsg');
  closewin.addEventListener('click', function () {
    // Closing discards the fill. No ATS keeps a draft for an anonymous candidate, which
    // is the same fact that makes this whole feature a browser rather than a link — so
    // the window is the only place the work exists, and one misclick is all of it.
    if (!confirm('Close the window? An application you have not submitted is not saved '
                 + 'anywhere, so the fill is discarded.')) return;
    closewin.disabled = true;
    donemsg.textContent = 'closing…';
    post('/api/session/close', {}).then(function (res) {
      // Deliberately not "closed": the request only asked. The browser thread reads the
      // flag on its next poll, and `gone()` says so when it has actually happened.
      donemsg.textContent = res.ok ? (res.detail || 'closing…')
                                   : (res.error || 'could not close it');
      if (!res.ok) closewin.disabled = false;
    }).catch(function () {
      closewin.disabled = false;
      donemsg.textContent = 'the server did not answer';
    });
  });

  // -- the poll ----------------------------------------------------------------------
  // This is also what tells the browser thread somebody is watching, which is the only
  // thing that makes it take screenshots. Stop polling and the work stops.
  var ago = document.getElementById('ago');

  // The window is gone. Everything here is about not looking alive: the banner the
  // server already rendered comes out of hiding, every control stops taking input, and
  // the poll stops — there is nothing left to ask about, and asking anyway is what kept
  // the button sitting on "closing…" over a browser that had already closed.
  var stopped = false;
  function gone() {
    if (stopped) return;
    stopped = true;
    document.getElementById('phase').textContent = 'closed';
    document.getElementById('gone').hidden = false;
    document.querySelectorAll('.lf .lv, .lf .lf-file').forEach(function (el) {
      el.disabled = true;
    });
    closewin.disabled = true;
    donemsg.textContent = 'the window is closed';
    ago.textContent = 'the window is closed';
  }

  function tick() {
    fetch('/api/session').then(function (r) { return r.json(); }).then(function (res) {
      if (!res.ok) { gone(); return; }
      var s = res.session;
      if (s.phase === 'closed') { gone(); return; }
      document.getElementById('phase').textContent = s.phase;
      var line = s.discovered
        ? s.filled + '/' + s.discovered + ' fields filled' +
          (s.need ? ' · ' + s.need + ' need you' : ' · nothing left to type')
        : 'no application form found on ' + s.url;
      document.getElementById('summary').textContent = line;
      var counts = document.getElementById('counts');
      if (counts) counts.textContent = line;

      // The form was read again and the handles moved, so every field on this page now
      // points at the wrong input. Say so and stop — silently pushing into whatever is
      // there now is the one outcome this whole mechanism exists to prevent.
      if (s.epoch !== epoch) {
        moved.hidden = false;
        document.querySelectorAll('.lf .lv, .lf .lf-file').forEach(function (el) {
          el.disabled = true;
        });
        return;
      }
      paint(s);
      if (!paused && s.has_shot) {
        img.src = '/api/session/preview.jpg?t=' + s.shot_at;
        ago.textContent = 'refreshed just now';
      } else if (paused) {
        ago.textContent = 'paused';
      }
    }).catch(function () {}).then(function () {
      if (!stopped) setTimeout(tick, POLL_MS);
    });
  }

  // Statuses only, and only for fields you are not in the middle of typing into. The
  // rows themselves were rendered by the server and are not rebuilt here.
  function paint(s) {
    s.fields.forEach(function (f) {
      var card = document.querySelector('.lf[data-handle="' + f.handle + '"]');
      if (!card || card.contains(document.activeElement)) return;
      if (card.classList.contains('busy')) return;
      var word = {filled: 'filled', gap: 'needs you',
                  refused: 'would not take it', pending: '…',
                  cleared: 'cleared'}[f.status] || f.status;
      setStatus(card.querySelector('.st'), f.status, word);
    });
  }

  tick();
})();
"""


# Values come from data-* attributes rather than being interpolated into onclick
# handlers: a title containing a quote would otherwise break out of the attribute,
# and ATS titles are attacker-controllable in principle.
_JS = """
async function post(url, body) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                             body: JSON.stringify(body||{})});
  return r.json();
}
// Both rule controls land here. The rematch is not cosmetic: a token is only safe to
// keep once you have seen what it did to the corpus, so the count delta is reported
// before the page reloads and the old numbers are gone.
async function addRule(phrase, list) {
  const res = await post('/api/rule', {phrase, list});
  if (!res.ok) { alert(res.error); return; }
  const rm = await post('/api/rematch', {});
  alert('Added "' + phrase + '" to ' + list +
        '\\nmatch: ' + (rm.before.match||0) + ' -> ' + (rm.after.match||0) +
        '\\nuncertain: ' + (rm.before.uncertain||0) + ' -> ' + (rm.after.uncertain||0) +
        '\\nreject: ' + (rm.before.reject||0) + ' -> ' + (rm.after.reject||0));
  location.reload();
}
document.addEventListener('click', async (e) => {
  const rej = e.target.closest('button.reject');
  if (rej) {
    const res = await post('/api/decision', {company: rej.dataset.company,
                                             ats_job_id: rej.dataset.job,
                                             decision: 'reject'});
    if (!res.ok) { alert(res.error); return; }
    location.reload();
    return;
  }
  const add = e.target.closest('button.add-rule');
  if (add) {
    const phrase = add.dataset.phrase;
    const sel = add.closest('.sugg').querySelector('select.sugg-list');
    await addRule(phrase, sel ? sel.value : 'exclude_titles');
    return;
  }
  const tok = e.target.closest('button.add-token');
  if (tok) {
    const box = document.querySelector('input.token[data-list="' + tok.dataset.list + '"]');
    const phrase = box ? box.value.trim() : '';
    if (!phrase) { alert('nothing to add'); return; }
    await addRule(phrase, tok.dataset.list);
    return;
  }
  const save = e.target.closest('button.save');
  if (save) {
    const box = document.querySelector('input.answer[data-key="' + save.dataset.key + '"]');
    const res = await post('/api/answer', {question_key: save.dataset.key,
                                           value: box ? box.value : ''});
    if (!res.ok) { alert(res.error); return; }
    location.reload();
    return;
  }
  const cosave = e.target.closest('button.co-save, button.co-force');
  if (cosave) {
    const body = {force: cosave.classList.contains('co-force')};
    document.querySelectorAll('.newco').forEach(el => { body[el.dataset.key] = el.value; });
    const out = document.getElementById('coout');
    const force = document.querySelector('button.co-force');
    cosave.disabled = true;
    out.hidden = false;
    out.className = 'coresult';
    out.textContent = body.force ? 'Adding…' : 'Verifying the board…';
    let res;
    try { res = await post('/api/company', body); }
    finally { cosave.disabled = false; }
    if (!res.ok) { out.className = 'coresult bad'; out.textContent = res.error; return; }
    if (!res.saved) {
      // A refused board, not a refused request: show what came back and reveal the
      // escape hatch. The button already exists in the markup; this only unhides it.
      const v = res.verification || {};
      out.className = 'coresult bad';
      out.textContent = 'Not added — ' + v.reason +
        (v.board_name ? '\nboard name: ' + v.board_name : '') +
        (v.job_count ? '\njobs: ' + v.job_count : '') +
        ((v.sample_titles || []).length ? '\ntitles: ' + v.sample_titles.join(' · ') : '');
      if (force) force.hidden = false;
      return;
    }
    out.className = 'coresult';
    const v = res.verification;
    // textContent, never innerHTML: the diff carries a company name somebody typed.
    out.textContent = 'Added. Backup at ' + res.backup +
      (res.skipped_because ? '\nNot verified — ' + res.skipped_because +
                             '; expected_board_name written as null.' : '') +
      (v ? '\nVerified: ' + v.evidence_kind + ' · ' + v.job_count + ' jobs · board "' +
           v.board_name + '"' +
           (v.evidence_kind === 'reachable'
             ? '\nThis ATS publishes no board name, so that is NOT an identity check — ' +
               'it only proves the board answered and is not empty. Read the titles: ' +
               (v.sample_titles || []).join(' · ')
             : '') : '');
    const pre = document.createElement('pre');
    pre.textContent = res.diff;
    out.appendChild(pre);
    if (force) force.hidden = true;
    return;
  }
  const ident = e.target.closest('button.save-identity');
  if (ident) {
    const identity = {};
    document.querySelectorAll('input.identity').forEach(i => identity[i.dataset.key] = i.value);
    const res = await post('/api/identity', {identity});
    if (!res.ok) { alert(res.error); return; }
    location.reload();
    return;
  }
  const up = e.target.closest('button.upload-resume');
  if (up) {
    const input = document.getElementById('resume-file');
    const file = input && input.files[0];
    if (!file) { alert('Choose a file first.'); return; }
    up.disabled = true;
    // Base64 inside the JSON body rather than multipart: it reuses the one POST path
    // this server has, and keeps `form-action 'none'` in the CSP meaningful.
    const b64 = await new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result.split(',')[1]);
      r.onerror = reject;
      r.readAsDataURL(file);
    });
    const res = await post('/api/resume', {filename: file.name, content_b64: b64});
    up.disabled = false;
    if (!res.ok) { alert(res.error); return; }
    location.reload();
    return;
  }
  // -- applications ----------------------------------------------------------------
  // These three branches belong here because render_applications, in this file, is what
  // emits the buttons. Same rule the apply-to note below records.
  const addapp = e.target.closest('button.app-add');
  if (addapp) {
    const body = {};
    document.querySelectorAll('.addapp .newapp').forEach(i => body[i.dataset.key] = i.value);
    if (!body.company || !body.title) { alert('Company and title are required.'); return; }
    addapp.disabled = true;
    const res = await post('/api/application', body);
    addapp.disabled = false;
    if (!res.ok) { alert(res.error); return; }
    location.reload();
    return;
  }
  const stage = e.target.closest('button.app-save');
  if (stage) {
    const card = stage.closest('.app');
    const res = await post('/api/application', {
      company: card.dataset.company, ats_job_id: card.dataset.job,
      status: card.querySelector('select.appstatus').value,
      note: card.querySelector('input.appnote').value});
    if (!res.ok) { alert(res.error); return; }
    location.reload();
    return;
  }
  const meta = e.target.closest('button.app-meta');
  if (meta) {
    const card = meta.closest('.app');
    const res = await post('/api/application/meta', {
      company: card.dataset.company, ats_job_id: card.dataset.job,
      next_action: card.querySelector('input.appnext').value,
      next_action_note: card.querySelector('input.appnextnote').value});
    if (!res.ok) { alert(res.error); return; }
    location.reload();
    return;
  }
  const del = e.target.closest('button.app-delete');
  if (del) {
    const card = del.closest('.app');
    // The one destructive control on any of these pages, and the history goes with it.
    if (!confirm('Delete this application and its history?')) return;
    const res = await post('/api/application/delete', {
      company: card.dataset.company, ats_job_id: card.dataset.job});
    if (!res.ok) { alert(res.error); return; }
    location.reload();
    return;
  }
  const acc = e.target.closest('button.app-accept');
  if (acc) {
    const card = acc.closest('.prop');
    // Only present when the narrower could not tell which application it is. Sending ''
    // is what makes the server refuse rather than guess.
    const sel = card.querySelector('select.propjob');
    const res = await post('/api/mail/accept', {
      message_id: card.dataset.message, ats_job_id: sel ? sel.value : ''});
    if (!res.ok) { alert(res.error); return; }
    location.reload();
    return;
  }

  const nope = e.target.closest('button.app-dismiss');
  if (nope) {
    const card = nope.closest('.prop');
    const res = await post('/api/mail/dismiss', {message_id: card.dataset.message});
    if (!res.ok) { alert(res.error); return; }
    location.reload();
    return;
  }

  // No `button.apply-to`, `button.pick-rebuild`, `button.pick-attach` or
  // `button.pick-detach` branch here on purpose. This script is emitted by the tuning,
  // settings and applications pages only; those buttons are rendered by the dashboard,
  // whose script is dashboard._JS. A handler here never ran, which is exactly how the
  // Open prefilled button came to do nothing at all.
});
"""


def serve(db_path: Path, criteria_path: Path, companies_path: Optional[Path],
          host: str = "127.0.0.1", port: int = 8765,
          answers_path: Optional[Path] = None) -> int:
    httpd = TuningServer((host, port), Handler, db_path, criteria_path, companies_path,
                         answers_path)
    log.info(
        "serving on http://%s:%d  (dashboard: /  tuning: /tuning  settings: /settings  "
        "health: /healthz /readyz)",
        host, port,
    )
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "bound to %s — this has no authentication and can edit %s", host, criteria_path
        )

    # Graceful shutdown. SIGTERM is how an orchestrator asks a process to stop, so it has
    # to mean "drain and exit 0", not "die". serve_forever() runs on a worker thread and
    # the signal sets an Event the main thread waits on; httpd.shutdown() then stops the
    # loop *from a different thread* (calling it from within serve_forever's thread would
    # deadlock). The server handles one request at a time, so once shutdown() returns the
    # single in-flight request, if any, has already completed — that is the drain.
    stop = threading.Event()

    def _handle_signal(signum: int, _frame) -> None:
        log.info("received %s — draining and shutting down", signal.Signals(signum).name)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except ValueError:
            # signal() only works on the main thread; a serve() invoked from a worker
            # (e.g. a test) falls back to the KeyboardInterrupt path below.
            pass

    worker = threading.Thread(target=httpd.serve_forever, name="jobtracker-serve", daemon=True)
    worker.start()
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass  # only reached if SIGINT could not be installed above
    finally:
        httpd.shutdown()          # returns once serve_forever has stopped accepting
        worker.join(timeout=5)
        httpd.server_close()
    log.info("stopped")
    return 0
