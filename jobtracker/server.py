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

import base64
import binascii
import html
import json
import logging
import os
import signal
import sqlite3
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import yaml

from . import (
    applications as apps_mod,
    config,
    dashboard as dashboard_mod,
    rank as rank_mod,
    safewrite,
    store,
    tuning,
)
from .criteria import _LIST_KEYS, load_criteria
from .match import location_label, location_rank, match
from .models import Decision, Posting, Verdict

log = logging.getLogger("jobtracker.serve")

MAX_BODY = 64 * 1024  # a decision payload is a few hundred bytes

# The resume upload, base64'd inside a JSON body — so the wire cost is ~4/3 of the file.
# 6 MB of body is comfortably over any real resume and still bounded.
MAX_UPLOAD = 6 * 1024 * 1024

# Held for as long as a prefilled window is open. One browser profile directory, which
# Chromium locks, so two at once is not a thing that can work — and the second failure
# would happen on a worker thread where nothing can report it back to the click.
_APPLY_LOCK = threading.Lock()

# What a resume may be. An allowlist rather than a blocklist, and checked before anything
# reaches disk: this file is written into a directory the pipeline reads on every run.
RESUME_TYPES = {
    ".pdf": b"%PDF-",
    # DOCX is a zip. `PK\x03\x04` is the local file header every one starts with.
    ".docx": b"PK\x03\x04",
}


# -- rendering (pure reads, testable without a server) ------------------------------
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
                f"<button data-phrase=\"{html.escape(s.phrase, quote=True)}\" "
                'class=add-rule>add to exclude_titles</button></div>'
            )

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

    p.append(f"<h2>Unanswered questions ({len(gaps)})</h2>")
    if not gaps:
        p.append("<p class=note>Nothing outstanding. Every field prefill has seen so "
                 "far has an answer.</p>")
    for gap in gaps:
        options = gap["options"] or ""
        p.append("<div class=gap>")
        p.append(f"<div class=ask>{html.escape(gap['ask'])}</div>")
        p.append(
            "<div class=note>"
            f"asked by {html.escape(gap['seen_on'])} · type {html.escape(gap['type'])}"
            f" · first seen {html.escape(gap['first_seen'])}</div>"
        )
        if options:
            p.append(f"<div class=note>one of: {html.escape(options)}</div>")
        # The key travels in a data attribute rather than being interpolated into a
        # handler — the question text comes from a third-party ATS.
        p.append(
            f"<div class=row><input class=answer type=text placeholder='Your answer' "
            f"data-key=\"{html.escape(gap['question_key'], quote=True)}\">"
            f"<button class=save data-key=\"{html.escape(gap['question_key'], quote=True)}\">"
            "Save</button></div>"
        )
        p.append("</div>")

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
        # No external anything, matching the static dashboard's guarantee.
        #
        # `connect-src 'self'` is required, not optional: every write on this server
        # goes out as a fetch() to /api/..., and connect-src falls back to default-src
        # when unset — which is 'none' here. Without it the browser blocks the request
        # and the buttons silently do nothing.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; form-action 'none'",
        )
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
            else:
                self._send("<h1>404</h1>", 404)
        except Exception:  # noqa: BLE001
            log.exception("GET %s failed", path)
            self._send("<h1>500</h1><p>See the server log.</p>", 500)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        # The cap is per-route rather than global: only the upload carries a file, and
        # leaving 6 MB open on every endpoint would let a decision POST buffer one.
        payload = self._read_json(MAX_UPLOAD if path == "/api/resume" else MAX_BODY)
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
            elif path == "/api/apply-to":
                self._send_json(self._api_apply_to(payload))
            elif path == "/api/application":
                self._send_json(self._api_application(payload))
            elif path == "/api/application/meta":
                self._send_json(self._api_application_meta(payload))
            elif path == "/api/application/delete":
                self._send_json(self._api_application_delete(payload))
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

        filename = str(payload.get("filename") or "").strip()
        b64 = str(payload.get("content_b64") or "")
        if not filename or not b64:
            return {"ok": False, "error": "no file"}

        # The extension decides the name we write, so it can never come from the client's
        # string — a filename is attacker-shaped input even when the attacker is you with
        # a badly named file. Only the suffix is read, and only from an allowlist.
        suffix = Path(filename).suffix.lower()
        if suffix not in RESUME_TYPES:
            return {"ok": False,
                    "error": f"{suffix or 'that'} is not a resume; expected "
                             f"{' or '.join(sorted(RESUME_TYPES))}"}
        try:
            blob = base64.b64decode(b64, validate=True)
        except (ValueError, binascii.Error):
            return {"ok": False, "error": "could not decode the upload"}
        if not blob:
            return {"ok": False, "error": "the file is empty"}
        if len(blob) > MAX_UPLOAD:
            return {"ok": False, "error": f"{len(blob) // 1024} KB is over the "
                                          f"{MAX_UPLOAD // (1024 * 1024)} MB limit"}
        if not blob.startswith(RESUME_TYPES[suffix]):
            # Content, not just the name. A .docx that is really something else would be
            # attached to a job application and read by a person.
            return {"ok": False, "error": f"that is not really a {suffix} file"}

        target = path.parent / f"resume{suffix}"
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(blob)
        os.replace(tmp, target)

        try:
            safewrite.write_text(path, set_resume(path.read_text(), target.name),
                                 load_answers)
        except safewrite.RefusedWrite as exc:
            return {"ok": False, "error": f"refused: {exc}"}

        log.info("resume saved to %s (%d bytes)", target, len(blob))
        return {"ok": True, "filename": target.name, "bytes": len(blob)}

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

        from . import browser as browser_mod

        reason = browser_mod.unavailable_reason()
        if reason:
            return {"ok": False, "error": reason}

        # One window at a time. The browser profile is a single directory and Chromium
        # locks it, so a second launch while the first window is open fails — on the
        # worker thread, where nobody sees it. Saying so is the honest answer.
        if not _APPLY_LOCK.acquire(blocking=False):
            return {"ok": False,
                    "error": "a prefilled window is already open — close it first"}

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
                )
            except Exception:  # noqa: BLE001 — a browser failure must not kill serve
                log.exception("apply-to %s/%s failed", company_name, job_id)
            finally:
                worker_conn.close()
                _APPLY_LOCK.release()

        threading.Thread(
            target=_run, name=f"jobtracker-apply-{job_id}", daemon=True
        ).start()
        return {"ok": True, "detail": f"opening {row['title'][:60]}…"}

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
    ' · <a href="/tuning">Tuning</a> · <a href="/settings">Settings</a></nav>'
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
"""

_EXTRA_CSS = """
.nav{margin:0 0 1rem;font-size:.9rem}
.banner{padding:.6rem .8rem;border-radius:6px;font-weight:600}
.banner.ok{background:#0f5132;color:#d1e7dd}
.banner.bad{background:#58151c;color:#f8d7da}
.regression{padding:.4rem .8rem;border-left:3px solid #dc3545;margin:.3rem 0}
.sugg{padding:.4rem 0;display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
.note{opacity:.75;font-size:.9rem}
button{cursor:pointer;padding:.25rem .6rem;border-radius:5px;border:1px solid currentColor;
background:transparent;color:inherit;font:inherit}
button:hover{opacity:.7}
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
    const res = await post('/api/rule', {phrase, list: 'exclude_titles'});
    if (!res.ok) { alert(res.error); return; }
    const rm = await post('/api/rematch', {});
    alert('Added "' + phrase + '"\\nmatch: ' + (rm.before.match||0) +
          ' -> ' + (rm.after.match||0));
    location.reload();
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
  // No `button.apply-to` branch here on purpose. This script is emitted by the tuning,
  // settings and applications pages only; that button is rendered by the dashboard,
  // whose script is dashboard._JS. A handler here never ran, which is exactly how the
  // button came to do nothing at all.
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
