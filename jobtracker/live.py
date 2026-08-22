"""The one live application form, mirrored into something the dashboard can render.

Why this exists
---------------
`browser.py` opens a real Chromium on the machine running `serve` and fills what it
knows. Everything left over used to be yours to type *in that window* — which on a
headless host means through a VNC viewer, shipping video frames for a task that is
fifteen text fields. The lag is structural, not a tuning problem.

But the fields are already known. `_DISCOVER_JS` tags every input with a `data-jt-id`,
`_fields_from_dom` names them, and `_write` puts a value into exactly one of them. What
was missing was a channel from the page you are actually looking at to that writer. This
module is that channel, and nothing else.

Three constraints shape every line here:

**Playwright objects belong to the thread that made them.** The HTTP handler thread may
never touch `page`. So a request enqueues a command and returns; the browser thread
drains the queue inside the poll it was already doing, and the outcome comes back on the
next snapshot. Nothing here blocks on a browser.

**A command points, it does not write.** The vocabulary is four names, and a command
carries a field *handle* — an opaque token minted by the discovery pass — never a
selector, never an expression, never anything the browser thread evaluates. That is the
same bound the prefill model is held to, for the same reason: there must be no path by
which text from outside becomes code running on somebody's application form. Nothing in
this vocabulary can activate anything.

**A handle is only valid for the discovery that minted it.** `_DISCOVER_JS` renumbers
`jt0…jtN` from scratch on every pass, so once the form changes shape the same handle
names a different input. Every command carries the `epoch` it was written against and is
dropped on a mismatch. This is the one way this feature could put an answer you did not
give into a field you cannot see, so the check lives in the drain, on the browser thread,
where it cannot be bypassed.

No Playwright, no HTTP, no SQLite: `browser.py` and `server.py` both import this and
neither imports the other, and the whole mechanism is testable with neither a browser nor
a socket. Same split as the task modules, where the runner owns every socket.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

# How long a poll buys. The page polls about every 2s, so this outlives a couple of
# missed beats without leaving the browser thread taking screenshots for a closed tab.
WATCH_WINDOW_S = 8.0

# Phases, in the order they happen. `OPENING` and `FILLING` exist because the fill takes
# several seconds — navigate, wait for the SPA, then one write per field — and the page
# you land on has to say which of those is happening rather than showing an empty form.
# `CLOSED` is set when the window is gone, and is the difference between "nothing to
# show yet" and "there is nothing to show".
OPENING = "opening"
FILLING = "filling"
READY = "ready"
CLOSED = "closed"

# The complete command vocabulary. Adding to it is adding to what a web request can make
# a browser do on a page holding your name, address and work history — so it is a closed
# list with a test on it, not an open protocol.
SET = "set"                # put a value in one field, by handle
CLEAR = "clear"            # empty one field, by handle
REDISCOVER = "rediscover"  # read the form again; its shape may have changed
SHOOT = "shoot"            # take a preview screenshot
HIGHLIGHT = "highlight"    # outline one field, so the preview follows what you edit

VOCABULARY = frozenset({SET, CLEAR, REDISCOVER, SHOOT, HIGHLIGHT})

# `CLEAR` is its own name rather than `SET` with an empty value, for two reasons that both
# bite. A `file` row's value is a path on this machine, so `""` there is not "no text" but
# "no file", and the one place the two readings differ is the one holding your resume. And
# a write that means "undo the write" should not be spelled the same as the write — the
# outcome is a different status, and overloading the name is how it ends up sharing one.
#
# It stays inside the vocabulary because it still cannot activate anything: emptying a
# field is the exact inverse of filling one. Submitting is not, which is why it is a
# session flag and not a sixth name here — see `Session.request_submit`.

# Row statuses. `refused` is a real outcome and not an error: a dropdown that does not
# offer the answer we hold is a question we cannot answer, and saying so beats picking
# the nearest option and putting words in your mouth.
#
# `cleared` is a fifth: you emptied it on purpose. It is not `filled` (there is nothing in
# it), and it is not `gap` (a gap is a question nobody ever had an answer for). Keeping it
# distinct is what lets the page say "cleared" rather than implying the fill missed it.
FILLED = "filled"
GAP = "gap"
REFUSED = "refused"
PENDING = "pending"
CLEARED = "cleared"


@dataclass
class Command:
    """One thing to do to the live page, on the thread that owns it.

    `handle` is a discovery token and `value` is text destined for a form field. That is
    the entire payload: there is deliberately no selector field and no expression field.
    """

    kind: str
    handle: str = ""
    value: str = ""
    epoch: int = -1  # -1 means "not tied to a discovery" — SHOOT and REDISCOVER


def rows_from(found: list, fields: list, carried: Optional[dict] = None) -> list:
    """Build the mirrored rows from one discovery pass.

    `found` and `fields` are the two halves `fill_application` already computes and walks
    in lockstep — the raw DOM findings and the named `FormField`s. Taking both means the
    mirror's rows and the fill's own outcome come from a single pass and cannot disagree
    about what is on the page.

    `carried` maps a field *key* to `(status, value)` from the previous reading. Keyed by
    key rather than handle deliberately: handles are positional and shift when the form
    grows a question, whereas the key is the ATS's own field name (or the label's slug),
    which does not. A field with nothing carried is `PENDING` — the correct state for a
    question that has only just appeared.
    """
    carried = carried or {}
    rows = []
    for raw, field_ in zip(found, fields):
        status, value = carried.get(field_.key, (PENDING, ""))
        rows.append({
            "handle": raw["handle"],
            "key": field_.key,
            "label": field_.label,
            "type": field_.type,
            "required": bool(field_.required),
            "options": list(field_.options),
            "value": value,
            "status": status,
        })
    return rows


def signature(rows: list) -> tuple:
    """What has to stay the same for a handle issued earlier to still be trustworthy.

    Handle, key and type together: `_DISCOVER_JS` walks the DOM in order and numbers as
    it goes, so if every position still reports the same field under the same handle, no
    handle moved and every one the page is holding is still good. Anything else — a
    question revealed by an answer, a section collapsing, a field going disabled — shifts
    the numbering from that point on, and every later handle now names its neighbour.
    """
    return tuple((r["handle"], r["key"], r["type"]) for r in rows)


@dataclass
class Session:
    """Everything the page needs to know about the window `serve` is holding open.

    One at a time, because Chromium locks the single browser-profile directory — the
    same reason `server._APPLY_LOCK` exists. This is the readable half of that lock.
    """

    company: str = ""
    ats_job_id: str = ""
    title: str = ""
    url: str = ""

    phase: str = OPENING
    epoch: int = 0

    # What the page had, as counted by the discovery pass. Kept separately from
    # `len(fields)` on purpose: they are the same number today, and the moment they stop
    # being, the honest one is this. Zero means no form was found — a finding, not a
    # finished job.
    discovered: int = 0
    fields: list = field(default_factory=list)
    note: str = ""

    shot: Optional[bytes] = None
    shot_at: float = 0.0
    watch_until: float = 0.0

    commands: queue.Queue = field(default_factory=queue.Queue)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # Asked for from the page, read by the browser thread. Not a `Command`, deliberately:
    # the vocabulary is what a web request may do *to the form*, and this does nothing to
    # the form — it ends the session. It also has to work when nothing is being accepted
    # any more, which is exactly when you most want it.
    closing: bool = False

    # -- written from the browser thread -------------------------------------------
    def carried(self) -> dict:
        """The current statuses, keyed for `rows_from` to carry into the next reading."""
        with self.lock:
            return {r["key"]: (r["status"], r["value"]) for r in self.fields}

    def absorb(self, rows: list) -> bool:
        """Take a fresh reading, bumping the epoch only if the handles moved.

        Both halves matter. Bumping always would be safe and useless: a successful write
        re-reads the form, so every edit would invalidate the handles the page is holding
        and the next field you typed would be refused for no reason. Never bumping would
        be the silent-corruption case in the module docstring.

        So the epoch tracks the one thing it is about — whether a handle still names the
        input it named when it was issued — and `signature` is that question asked of the
        whole form at once. Returns whether the shape changed.
        """
        with self.lock:
            moved = signature(rows) != signature(self.fields)
            if moved:
                self.epoch += 1
            self.fields = rows
            self.discovered = len(rows)
            return moved

    def mark(self, handle: str, status: str, value: str) -> None:
        with self.lock:
            for row in self.fields:
                if row["handle"] == handle:
                    row["status"] = status
                    row["value"] = value
                    return

    def retarget(self, url: str) -> None:
        """Say which page the browser actually went to.

        The session is created from the posting's URL, because the page has to render
        before the fill has started. The browser then goes somewhere else: an Ashby
        posting gains `/application`, and a Greenhouse one is redirected at the form
        itself, which is a different host from the posting entirely. If the two are
        allowed to disagree, *"no application form found on …"* names a page nobody
        looked at — and that sentence exists to send you to look.
        """
        with self.lock:
            self.url = url

    def set_phase(self, phase: str, note: str = "") -> None:
        with self.lock:
            self.phase = phase
            if note:
                self.note = note

    def store_shot(self, blob: bytes, now: Optional[float] = None) -> None:
        with self.lock:
            self.shot = blob
            self.shot_at = time.time() if now is None else now

    def due_for_shot(self, every: float, now: Optional[float] = None) -> bool:
        clock = time.time() if now is None else now
        with self.lock:
            return clock - self.shot_at >= every

    def watching(self, now: Optional[float] = None) -> bool:
        """Whether anyone is looking, which is the only reason to take a screenshot.

        The page refreshes this every poll, so closing the tab stops the work within one
        window rather than leaving the browser thread rendering JPEGs into a void. It is
        also, for free, the pause button: stop polling and the shooting stops.
        """
        clock = time.monotonic() if now is None else now
        with self.lock:
            return clock < self.watch_until

    # -- written from the request thread -------------------------------------------
    def watch(self, now: Optional[float] = None) -> None:
        clock = time.monotonic() if now is None else now
        with self.lock:
            self.watch_until = clock + WATCH_WINDOW_S

    def request_close(self) -> None:
        """Ask for the window to be closed, from a thread that may not touch it.

        The window opens on the machine running `serve`. On a headless host — which is
        the case this whole page was built for — there is no window to walk over to and
        close, so the only ending `_hold_until_closed` knew about could never happen: the
        browser stayed up, the one-window lock stayed held, and every later attempt to
        open a prefilled application was refused with *"a prefilled window is already
        open"* until `serve` was restarted. Observed 2026-08-19.

        Setting a flag rather than closing anything here is the same rule every other
        write on this page follows: Playwright objects belong to the thread that made
        them, so the browser thread reads this in the poll it is already doing.
        """
        with self.lock:
            self.closing = True

    def close_requested(self) -> bool:
        with self.lock:
            return self.closing

    def submit(self, command: Command) -> bool:
        """Queue one command. False if it is not one of the four, or the window is gone.

        Refusing an unknown kind here as well as in the drain is deliberate belt-and-
        braces: it lets the page say so immediately instead of on the next poll, and it
        keeps the browser thread's check a second line of defence rather than the only
        one.
        """
        if command.kind not in VOCABULARY:
            return False
        with self.lock:
            if self.phase == CLOSED:
                return False
        self.commands.put(command)
        return True

    def snapshot(self) -> dict:
        """What the page polls for. A plain dict — no bytes, no locks, no Playwright."""
        with self.lock:
            filled = sum(1 for r in self.fields if r["status"] == FILLED)
            # Everything that is not filled needs you — including PENDING. A field the
            # fill has not reached, or one a re-reading has only just revealed, is
            # emphatically not "nothing left to type", and counting it as such is the
            # same absence-read-as-success mistake `summary` guards against for zero.
            need = sum(1 for r in self.fields if r["status"] != FILLED)
            return {
                "closing": self.closing,
                "company": self.company,
                "ats_job_id": self.ats_job_id,
                "title": self.title,
                "url": self.url,
                "phase": self.phase,
                "epoch": self.epoch,
                "discovered": self.discovered,
                "filled": filled,
                "need": need,
                "note": self.note,
                "has_shot": self.shot is not None,
                "shot_at": self.shot_at,
                "fields": [dict(r) for r in self.fields],
            }


# The one live session, or None. Written by `server._api_apply_to` while it holds the
# same lock that guards the browser profile directory; read by everything else.
CURRENT: Optional[Session] = None


def current() -> Optional[Session]:
    return CURRENT


def start(company: str, ats_job_id: str, title: str, url: str) -> Session:
    global CURRENT
    CURRENT = Session(company=company, ats_job_id=ats_job_id, title=title, url=url)
    return CURRENT


def summary(snap: dict) -> str:
    """One line about the form, said the same way everywhere.

    Zero discovered is *"no application form found"*, never "0/0 fields · nothing left to
    type". A form that had not rendered, an employer page that only links to the real
    application, a login wall — all of them produce zero, and all of them need you to
    look. Absence read as success is the failure this project exists to avoid
    (DESIGN.md §3.4), and this is the sentence where it would happen.
    """
    if not snap["discovered"]:
        return f"no application form found on {snap['url']}"
    tail = f" · {snap['need']} need you" if snap["need"] else " · nothing left to type"
    return f"{snap['filled']}/{snap['discovered']} fields filled{tail}"
