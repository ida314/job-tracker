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
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import yaml

from . import config, dashboard as dashboard_mod, store, tuning
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


# -- server -------------------------------------------------------------------------
class TuningServer(HTTPServer):
    """Carries the paths the handler needs; HTTPServer has nowhere else to put them."""

    def __init__(self, addr, handler, db_path: Path, criteria_path: Path,
                 companies_path: Optional[Path]) -> None:
        super().__init__(addr, handler)
        self.db_path = db_path
        self.criteria_path = criteria_path
        self.companies_path = companies_path


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
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; form-action 'none'",
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
            elif path == "/api/rule":
                self._send_json(self._api_rule(payload))
            elif path == "/api/rematch":
                self._send_json(self._api_rematch())
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
            page = dashboard_mod.build_dashboard(conn, companies, _today(), criteria)
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
        tmp = path.with_suffix(".yaml.candidate")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))
        try:
            load_criteria(tmp)
        except Exception as exc:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            return {"ok": False, "error": f"refused invalid criteria: {exc}"}

        shutil.copy2(path, path.with_suffix(".yaml.bak"))
        tmp.replace(path)
        log.info("added %r to %s", phrase, key)
        return {"ok": True, "phrase": phrase, "list": key}

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


_NAV = '<nav class=nav><a href="/">Dashboard</a> · <a href="/tuning">Tuning</a></nav>'

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
  }
});
"""


def serve(db_path: Path, criteria_path: Path, companies_path: Optional[Path],
          host: str = "127.0.0.1", port: int = 8765) -> int:
    httpd = TuningServer((host, port), Handler, db_path, criteria_path, companies_path)
    log.info(
        "serving on http://%s:%d  (dashboard: /  tuning: /tuning  health: /healthz /readyz)",
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
