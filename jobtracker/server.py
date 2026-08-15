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
import shutil
import signal
import sqlite3
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import yaml

from . import (
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
        "<h1>Tuning</h1>",
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
        "<body><div class=wrap><h1>Settings</h1>", _NAV,
        f"<p class=note>Answer bank: <code>{html.escape(str(answers_path))}</code>"
        " — git-ignored, and the source of truth. This page edits it in place.</p>",
    ]

    if error:
        p.append(f"<p class='banner bad'>{html.escape(error)}</p>")
    elif answers is None:
        p.append(
            "<p class='banner bad'>No answer bank yet. "
            "<code>cp answers.example.yaml answers.yaml</code> and fill it in.</p>"
        )

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

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
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
            else:
                self._send("<h1>404</h1>", 404)
        except Exception:  # noqa: BLE001
            log.exception("GET %s failed", path)
            self._send("<h1>500</h1><p>See the server log.</p>", 500)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        payload = self._read_json()
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
            elif path == "/api/apply-to":
                self._send_json(self._api_apply_to(payload))
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

    def _render_dashboard(self) -> str:
        """The existing dashboard, regenerated live. Unchanged code, unchanged output."""
        conn = self._conn()
        try:
            companies = config.load_companies(self.server.companies_path)
            criteria = load_criteria(self.server.criteria_path)
            # interactive=True: only the served page gets the disposition buttons,
            # because only here is there something for them to POST to.
            page = dashboard_mod.build_dashboard(
                conn, companies, _today(), criteria, interactive=True
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
                "SELECT title FROM postings WHERE company=? AND ats_job_id=?",
                (company, job_id),
            ).fetchone()
            if row is None:
                return {"ok": False, "error": "no such posting"}

            now = datetime.now().isoformat(timespec="seconds")
            note = str(payload.get("note") or "")
            if action == "applied":
                store.record_application(
                    conn, company, job_id, row["title"], "applied", now, note=note
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

    def _api_apply_to(self, payload: dict) -> dict:
        """Open one application in a browser, filled in. Returns before it finishes.

        This server handles one request at a time — it is `HTTPServer`, not
        `ThreadingHTTPServer` — so driving a browser inline would freeze the page for
        as long as the window stayed open. The fill runs on a daemon thread and this
        answers immediately.

        Playwright's sync API is used on that thread, which is fine; what it must not do
        is run inside an asyncio loop, and nothing here has one.
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
            return {"ok": False, "error": error or "no usable answer bank"}

        def _run() -> None:
            from . import browser as browser_mod

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
                    # Nothing is watching a terminal here, so there is no prompt to
                    # wait on. The window stays open because the context is closed
                    # only when the thread ends, and it ends when the fill is done.
                    wait=False,
                )
            except Exception:  # noqa: BLE001 — a browser failure must not kill serve
                log.exception("apply-to %s/%s failed", company_name, job_id)
            finally:
                worker_conn.close()

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


_NAV = (
    '<nav class=nav><a href="/">Dashboard</a> · <a href="/tuning">Tuning</a>'
    ' · <a href="/settings">Settings</a></nav>'
)

_SETTINGS_CSS = """
.gap{padding:.7rem .9rem;margin:.5rem 0;border-left:3px solid #d97706;background:rgba(217,119,6,.07)}
.gap .ask{font-weight:600;margin-bottom:.2rem}
.gap .row{display:flex;gap:.5rem;margin-top:.5rem}
.gap input.answer{flex:1;padding:.3rem .5rem;border-radius:5px;
border:1px solid currentColor;background:transparent;color:inherit;font:inherit}
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
  const ap = e.target.closest('button.apply-to');
  if (ap) {
    ap.disabled = true;
    const res = await post('/api/apply-to', {company: ap.dataset.company,
                                             ats_job_id: ap.dataset.job});
    if (!res.ok) { alert(res.error); ap.disabled = false; return; }
    ap.textContent = 'Opening…';
  }
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
