"""Carry a prefill plan to a real application form, then stop.

Why a browser at all
--------------------
A URL cannot do this. Only Lever honours query-parameter prefill, and no URL of any
kind can attach a file. Nor can a saved cookie: Greenhouse, Ashby and Lever keep no
server-side draft for an anonymous candidate, so filling a form in one browser leaves
nothing behind for another to pick up. What actually fills a third-party form is code
running on the page — which is how the commercial autofill extensions do it, and what
this does with Playwright instead of an extension.

Three consequences worth stating plainly:

**It submits in exactly one place, and only when asked.** `_submit` holds the only
click in this module, and a test asserts that against the source: one such call, inside
that function, reached from nowhere but the hold loop — and still no `requestSubmit`, no
`form.submit`, no `dispatchEvent`, no `keyboard.press`. That shape is the point rather than a leftover of the old rule — a real
click on the employer's own control runs their validation, their required-field checks and
their captcha hooks, all of which a synthetic form submission skips.

Everything protecting it is on the other side: `Session.request_submit` refuses unless the
form is settled, the epoch matches, every required field is filled and the company name
has been typed; `claim_submit` takes the one submit a session has, under a lock, before
the click. An application is irreversible and goes out under your name, so nothing here
happens because a code path arrived at it — only because somebody asked for it by name.

**The DOM is also how forms are discovered.** Greenhouse publishes its questions;
nobody else does. Reading the rendered form gives every ATS — Ashby, Lever, and a
Workday portal too — the same "here is a question I have no answer for" loop, keyed per
company. Visit one Ashby posting and every later Ashby posting at that company is
prefillable.

**The browser profile persists.** Not for prefill state, which it cannot hold, but so
candidate-account logins survive between runs. That is the one thing that could ever
make the `manual` companies tractable. Not used for that yet.

Playwright's *sync* API is used here, and it must not be called from inside an asyncio
loop. That is why this module is separate from `tasks/`, which is async: `apply-to` is
a foreground command, and `serve` runs it on a plain daemon thread.
"""

from __future__ import annotations

import json
import logging
import queue
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import live, store
from .answers import normalize_label, slugify
from .models import FormField
from .tasks.prefill import resolve_field

log = logging.getLogger("jobtracker.browser")


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed, or has no browser to drive."""


NO_PLAYWRIGHT = (
    "playwright is not installed. `pip install 'jobtracker[browser]'` "
    "then `playwright install chrome`."
)


def unavailable_reason() -> Optional[str]:
    """Why no browser can be driven right now, or None if one can.

    Exists for callers that cannot see an exception. `serve` starts the fill on a daemon
    thread, so anything raised in `fill_application` reaches the log and nothing else —
    a missing Playwright would show up in the page as a button that says "Opening…" over
    a browser that never opens, which is the absence-read-as-success shape this project
    keeps out of its other loops.

    Only the import is checked. Whether a browser *binary* is installed is a question
    `_launch` can only answer by launching one, which is seconds, not milliseconds.
    """
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return NO_PLAYWRIGHT
    return None


# Reads every field on the page, tags each with a stable handle, and reports what it
# found. Runs in the page rather than through locators because the label for an input is
# a DOM-shaped question — four different conventions, tried in order of reliability —
# and answering it once in JS beats four round trips per field.
_DISCOVER_JS = """
() => {
  const skip = new Set(['hidden', 'submit', 'button', 'image', 'reset']);
  const text = (el) => (el ? (el.innerText || el.textContent || '') : '').trim();

  const labelFor = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const parts = by.split(/\\s+/).map((id) => text(document.getElementById(id)));
      const joined = parts.filter(Boolean).join(' ').trim();
      if (joined) return joined;
    }
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab && text(lab)) return text(lab);
    }
    const wrapping = el.closest('label');
    if (wrapping && text(wrapping)) return text(wrapping);
    // Last resorts: a heading-ish sibling above the input, then the placeholder.
    let node = el.parentElement;
    for (let hops = 0; node && hops < 3; hops++, node = node.parentElement) {
      const lab = node.querySelector('label, legend, .label, [class*="label"]');
      if (lab && text(lab)) return text(lab);
    }
    return (el.getAttribute('placeholder') || el.getAttribute('name') || '').trim();
  };

  const out = [];
  let n = 0;
  const nodes = document.querySelectorAll(
    'input, select, textarea, [contenteditable="true"]'
  );
  for (const el of nodes) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && skip.has(type)) continue;
    if (el.disabled) continue;
    if (!el.offsetParent && el.type !== 'file') continue;   // not rendered

    const handle = 'jt' + n++;
    el.setAttribute('data-jt-id', handle);

    let kind = 'text';
    if (tag === 'select') kind = el.multiple ? 'multiselect' : 'select';
    else if (tag === 'textarea') kind = 'textarea';
    else if (type === 'file') kind = 'file';
    else if (type === 'checkbox' || type === 'radio') kind = 'checkbox';

    const options = tag === 'select'
      ? Array.from(el.options).map((o) => o.label || o.text).filter(Boolean)
      : [];

    out.push({
      handle,
      name: el.getAttribute('name') || '',
      // `id` matters as much as `name` here. Greenhouse's current board UI sets no
      // name attributes at all and keys everything off id — the resume input is
      // `id="resume"` with no name — so a discovery that read only `name` would fail
      // to recognize the one field that matters most. Observed on a live board,
      // 2026-08-13.
      elementId: el.getAttribute('id') || '',
      label: labelFor(el).replace(/\\s+/g, ' ').slice(0, 300),
      type: kind,
      required: el.required === true || el.getAttribute('aria-required') === 'true',
      options,
    });
  }
  return out;
}
"""

# Applied to required fields nothing was written into, so the first thing you see on
# taking the window over is what still needs you.
_HIGHLIGHT_JS = """
(handles) => {
  for (const h of handles) {
    const el = document.querySelector(`[data-jt-id="${h}"]`);
    if (!el) continue;
    el.style.outline = '2px solid #d97706';
    el.style.outlineOffset = '2px';
  }
  if (handles.length) {
    const first = document.querySelector(`[data-jt-id="${handles[0]}"]`);
    if (first) first.scrollIntoView({ block: 'center' });
  }
}
"""


# Finding the one control that sends the form. Separate from `_DISCOVER_JS` because that
# pass deliberately skips submit and button inputs — they are not questions, and a handle
# minted for one would put a click inside the vocabulary that says it cannot activate
# anything. This mints a handle of its own, in its own attribute, reachable only from
# `_submit`.
#
# The ranking is deliberate. A real application form has several buttons on it — "Back",
# "Save draft", a cookie banner's "Accept" — and picking the wrong one is at best a lost
# fill. So: an explicit submit type beats a bare `<button>`, submit-shaped text beats
# other text, and anything reading as a cancel or a back is dropped outright rather than
# ranked last.
_SUBMIT_JS = """
() => {
  const text = (el) => ((el.value || '') + ' ' + (el.innerText || el.textContent || ''))
    .replace(/\\s+/g, ' ').trim();
  const nodes = document.querySelectorAll(
    'button[type="submit"], input[type="submit"], button:not([type]), ' +
    'button[type="button"], [role="button"]'
  );
  let best = null;
  for (const el of nodes) {
    if (el.disabled) continue;
    if (!el.offsetParent) continue;                       // not rendered
    const label = text(el).slice(0, 120);
    if (/\\b(cancel|back|previous|close|save draft|reset|accept cookies)\\b/i.test(label))
      continue;
    const type = (el.getAttribute('type') || '').toLowerCase();
    const explicit = type === 'submit';
    const worded = /\\b(submit|apply|send)\\b/i.test(label);
    // Explicit type is worth more than wording: the wording is the employer's copy and
    // "Apply" is also what the button that *opens* the form says.
    const score = (explicit ? 2 : 0) + (worded ? 1 : 0);
    if (score === 0) continue;
    if (!best || score > best.score) best = {label, score, el};
  }
  if (!best) return null;
  best.el.setAttribute('data-jt-submit', 'go');
  return {handle: 'go', label: best.label};
}
"""


@dataclass
class Filled:
    handle: str
    label: str
    type: str
    value: str
    question_key: Optional[str] = None


@dataclass
class FillReport:
    url: str = ""
    discovered: int = 0
    filled: list = field(default_factory=list)
    gaps: list = field(default_factory=list)  # FormField, unanswered
    new_questions: int = 0

    @property
    def found_a_form(self) -> bool:
        """Whether there was anything to fill in at all.

        Distinct from "filled everything", and the distinction is the point. Zero fields
        discovered means the form was not on that page — a JS shell that had not
        rendered, an employer careers page that only links to the real application, a
        login wall. Reporting that as "0/0 filled, nothing left to do" would be the
        absence-read-as-success failure this project exists to avoid (DESIGN.md §3.4).
        """
        return self.discovered > 0

    def summary(self) -> str:
        if not self.found_a_form:
            return f"no application form found on {self.url}"
        return f"{len(self.filled)}/{self.discovered} fields filled"


GREENHOUSE_BOARD = "https://job-boards.greenhouse.io"

# The form itself, as Greenhouse serves it for embedding. A complete standalone page —
# every question, the resume input, and the employer's own submit button — and the one
# Greenhouse URL that is never redirected away. See `apply_url_for`.
GREENHOUSE_FORM = f"{GREENHOUSE_BOARD}/embed/job_app"


def apply_url_for(
    url: str, ats: str, slug: str = "", ats_job_id: str = ""
) -> str:
    """The page carrying the application form, given the posting's URL.

    Kept here rather than in `sources/` because it is a fact about the *hosted careers
    page*, not about the JSON API those adapters exist to speak.

    Greenhouse gets special handling that is not cosmetic, and it has now been wrong
    twice in the same direction. Its `absolute_url` is whatever the employer configured,
    and large employers point it at their own careers site: Stripe's is
    `stripe.com/jobs/search?gh_jid=8077887`, a search page with no form on it at all.
    That was fixed on 2026-08-13 by using the hosted board instead — which turned out to
    move the problem rather than solve it, because **the hosted board redirects too**.
    `job-boards.greenhouse.io/asana/jobs/7766762` 302s to `asana.com/jobs/apply/…`, a JS
    shell whose form is a cross-origin iframe, so the browser found zero fields and the
    card said *"no application form found"* about a job that has a form.

    Measured across every Greenhouse board we track, 2026-08-19: **25 of 45 boards do
    not carry the form** at `/{slug}/jobs/{id}` (airbnb, asana, betterment, brex,
    coinbase, databricks, datadog, dropbox, lyft, mongodb, okta, pinterest, stripe and
    more — Cedar's board answers 403 there outright). The embed URL carried it for all
    45. So that is what the browser is pointed at: it is the form, it is keyless, and
    nothing redirects it. The employer's own URL stays the fallback for when the slug or
    the job id is not known.
    """
    base = (url or "").rstrip("/")
    if ats == "greenhouse" and slug and ats_job_id:
        return f"{GREENHOUSE_FORM}?for={slug}&token={ats_job_id}"
    if not base:
        return base
    if ats == "ashby":
        return base if base.endswith("/application") else f"{base}/application"
    if ats == "lever":
        return base if base.endswith("/apply") else f"{base}/apply"
    if ats == "greenhouse":
        return base if "#" in base else f"{base}#app"
    return base


def _launch(playwright, user_data_dir: Path, headless: bool):
    """A persistent context, preferring the Chrome already installed.

    Using the system Chrome means no second browser to download and no second profile
    to keep logged in. Falling back to Playwright's bundled Chromium keeps this working
    on a machine that has neither.
    """
    user_data_dir.mkdir(parents=True, exist_ok=True)
    for channel in ("chrome", None):
        try:
            return playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=headless,
                channel=channel,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:  # noqa: BLE001 — try the next channel
            log.debug("could not launch channel=%s: %s", channel, exc)
    raise BrowserUnavailable(
        "no browser to drive. Install one with `playwright install chrome` "
        "(or `playwright install chromium`)."
    )


def _fields_from_dom(found: list) -> list[FormField]:
    """DOM findings into the shared vocabulary.

    Key preference is name, then id, then a slug of the label. `name` first because
    that is what the ATS's own API calls the field, so a form learned from the DOM and
    one learned from the API agree; `id` next because Greenhouse's current UI sets no
    names at all; the label slug last, as the thing that always exists.
    """
    return [
        FormField(
            key=(f["name"] or f.get("elementId") or slugify(f["label"]) or f["handle"]),
            label=f["label"] or f["name"] or f.get("elementId") or f["handle"],
            type=f["type"],
            required=bool(f["required"]),
            options=tuple(f["options"]),
        )
        for f in found
    ]


def _discover(page) -> tuple:
    """Read every field on the page, and say which surface they were read off.

    `page.evaluate` runs in the main frame and nowhere else. An employer that embeds its
    ATS — which is ordinary practice, not an exotic case — puts the whole application in
    a cross-origin iframe, and a main-frame-only reading of that page finds nothing at
    all. Asana's careers page is exactly this: it renders the Greenhouse embed script,
    the form lives in an iframe, and the card reported *"no application form found"*
    about a job with thirty-one fields on it.

    The apply URL is now chosen to land on the form directly (see `apply_url_for`), so
    this is the second line of defence rather than the first — but zero discovered is the
    one reading this project may never take at face value, and looking in the frames
    before believing it costs one evaluate per frame.

    Returns `(found, surface)`. The surface is the `Page` or the `Frame` the fields were
    read off, and it is what every later write, highlight and re-reading must use: a
    handle minted by a discovery in one frame names nothing in any other.
    """
    found = page.evaluate(_DISCOVER_JS)
    if found:
        return found, page

    best, best_found = page, found
    for frame in getattr(page, "frames", []):
        if frame is getattr(page, "main_frame", None):
            continue
        try:
            got = frame.evaluate(_DISCOVER_JS)
        except Exception as exc:  # noqa: BLE001 — a frame we cannot read is not a crash
            log.debug("could not read frame %s: %s", getattr(frame, "url", "?"), exc)
            continue
        if len(got) > len(best_found):
            best, best_found = frame, got

    if best is not page:
        log.info("the form is in a frame: %d field(s) on %s",
                 len(best_found), getattr(best, "url", "?"))
    return best_found, best


def _page_of(surface):
    """The `Page` behind a surface, for the things only a page can do.

    A screenshot is one of them: `Frame` has `evaluate` and `fill` but no `screenshot`,
    and a picture of one frame would not be the picture the preview is for anyway.
    """
    return getattr(surface, "page", surface)


def _plan_index(plan_json: Optional[str]) -> dict:
    """Plan entries keyed every way a DOM field might be recognized.

    A plan built from Greenhouse's API is keyed by ATS field name; one built from an
    earlier DOM visit is keyed by a slug of the label. Indexing both, plus the
    normalized label itself, is what lets a plan built one way be applied the other.
    """
    if not plan_json:
        return {}
    index: dict = {}
    for entry in json.loads(plan_json):
        if entry.get("value") is None:
            continue
        for key in (entry["form_key"], slugify(entry["label"]),
                    normalize_label(entry["label"])):
            index.setdefault(key, entry)
    return index


def fill_application(
    conn,
    company,
    ats_job_id: str,
    url: str,
    answers,
    today: str,
    user_data_dir: Path,
    plan_json: Optional[str] = None,
    headless: bool = False,
    wait: bool = True,
    hold: bool = False,
    session=None,
    on_submitted=None,
) -> FillReport:
    """Open the application, fill what we know, record what we do not, and stop.

    Writes to `conn` — the form it read and any question it could not answer — because
    a visit that taught us an employer's form must not have to be repeated. It never
    writes an application anywhere, and it never clicks anything.

    The window lives exactly as long as this call does, because the context is closed on
    the way out — so a caller that wants to hand the window to a human has to say which
    kind of waiting it can do. `wait` is the CLI's: print the gaps and block on Enter.
    `hold` is `serve`'s: no terminal to prompt at, so block until the browser is closed.
    Neither is the same as "return quickly and leave it up" — there is no such mode, and
    asking for one is how the served button came to open a window that vanished.

    `session` is a `live.Session` to mirror the form into while holding it. It changes
    nothing about what gets filled; it publishes what was read and then answers queued
    commands from the dashboard, so the fields can be typed somewhere with no latency
    instead of through a video stream of this window. It implies `hold`.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise BrowserUnavailable(NO_PLAYWRIGHT) from exc

    target = apply_url_for(
        url, getattr(company, "ats", ""), getattr(company, "slug", ""), ats_job_id
    )
    report = FillReport(url=target)
    index = _plan_index(plan_json)
    alias_map = dict(answers.by_alias)
    alias_map.update(store.known_question_keys(conn))

    with sync_playwright() as playwright:
        context = _launch(playwright, user_data_dir, headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            if session is not None:
                # One derivation of the target, so the page and the browser cannot
                # disagree about which URL was opened.
                session.retarget(target)
                session.set_phase(live.FILLING)
            page.goto(target, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(1500)  # let a SPA render its form

            # `surface` is the page, or the frame the form turned out to live in.
            # Everything downstream — the writes, the highlight, every later reading —
            # goes through it, because a handle belongs to the discovery that minted it
            # and that discovery happened here.
            found, surface = _discover(page)
            report.discovered = len(found)
            fields = _fields_from_dom(found)
            log.info("%s: %d field(s) on %s", company.name, len(fields), target)

            unfilled_required: list[str] = []
            # What happened to each field, for the mirror. Collected here rather than
            # reconstructed from `report` afterwards because `report.gaps` holds
            # `FormField`s, which carry no handle — and the handle is the only thing
            # that identifies an input on the live page.
            statuses: dict = {}
            for raw, field_ in zip(found, fields):
                entry = (
                    index.get(field_.key)
                    or index.get(slugify(field_.label))
                    or index.get(normalize_label(field_.label))
                )
                value = entry["value"] if entry else None
                question_key = entry["question_key"] if entry else None

                if value is None:
                    # Not in the plan — try the rules directly. A DOM visit often finds
                    # fields the plan never saw, and re-deriving here means the first
                    # visit to a new ATS still fills name and email.
                    resolved = resolve_field(field_, answers, alias_map)
                    value, question_key = resolved.value, resolved.question_key

                if value is None:
                    report.gaps.append(field_)
                    statuses[raw["handle"]] = (live.GAP, "")
                    if field_.required:
                        unfilled_required.append(raw["handle"])
                    _remember(conn, company.name, field_, None, today)
                    continue

                if _write(surface, raw, value):
                    report.filled.append(Filled(
                        handle=raw["handle"], label=field_.label,
                        type=field_.type, value=value, question_key=question_key,
                    ))
                    statuses[raw["handle"]] = (live.FILLED, value)
                    _remember(conn, company.name, field_, question_key, today)
                else:
                    report.gaps.append(field_)
                    statuses[raw["handle"]] = (live.REFUSED, "")
                    if field_.required:
                        unfilled_required.append(raw["handle"])
                    _remember(conn, company.name, field_, None, today)

            # One visible question can be several inputs — Greenhouse renders a combobox
            # as a text input plus a hidden select, and "Resume/CV" as a file input plus
            # a textarea. Once any of them holds the answer, the question is answered,
            # and listing its siblings would send the user off to answer it again.
            satisfied = {f.label for f in report.filled if f.label}
            report.gaps = [g for g in report.gaps if g.label not in satisfied]

            for field_ in report.gaps:
                if store.record_gap(
                    conn,
                    question_key=slugify(field_.label),
                    ask=field_.label,
                    field_type=field_.type,
                    company=company.name,
                    now=today,
                    options=" | ".join(field_.options[:20]) if field_.options else None,
                ):
                    report.new_questions += 1
            conn.commit()

            surface.evaluate(_HIGHLIGHT_JS, unfilled_required)

            if session is not None:
                # Keyed by field key for the carry-over, because that is what survives a
                # later reading; `rows_from` does the rest.
                carried = {f.key: statuses.get(r["handle"], (live.PENDING, ""))
                           for r, f in zip(found, fields)}
                session.absorb(live.rows_from(found, fields, carried))
                session.set_submit_control(_find_submit(surface))
                session.set_phase(live.READY, report.summary())

            if wait and not headless:
                print(f"\n{report.summary()}")
                _print_gaps(report)
                print("\nThe window is yours. Review, then submit it yourself.")
                try:
                    input("Press Enter here when you are done to close the browser… ")
                except (EOFError, KeyboardInterrupt):
                    pass
            elif (hold or session is not None) and not headless:
                _hold_until_closed(report, context, session, surface, on_submitted)
        finally:
            context.close()

    return report


HOLD_POLL_MS = 500

# How often the preview is refreshed while somebody is watching. A still-image cadence,
# not a video one — which is the whole point. The expensive path this replaces shipped
# every frame of a remote X server.
#
# Four seconds rather than two because the shot is now the whole page: measured on a live
# Greenhouse form (1280x3352, 2026-08-19) a full-page JPEG is 190 KB and 113 ms against
# 22 KB and 36 ms for the viewport. That is a fair price for seeing every field, and the
# picture is deliberately behind anyway — the fields you type into are local and instant.
SHOT_EVERY_S = 4.0

# Cheap enough to send often, legible enough to read a form in. PNG would be several
# times the bytes for a screenshot that is mostly flat white boxes.
SHOT_QUALITY = 60


def _hold_until_closed(report: FillReport, context, session=None,
                       surface=None, on_submitted=None) -> None:
    """Block until the window is gone. For a caller with no terminal to prompt at.

    **The wait has to happen inside a Playwright call.** The sync API only dispatches
    driver events while you are in one, so a loop of `time.sleep` plus a look at
    `context.pages` sees a frozen snapshot: measured 2026-08-16, a browser killed
    outright still read as one open page, forever, and the thread never returned. Half a
    second inside `wait_for_timeout` is a round trip to the driver, and that is what lets
    the close arrive.

    Both endings then land in the same place. Close the window and the page list empties
    or the next call raises `TargetClosedError`; kill the browser and it raises too. Any
    exception here means the browser is gone, which is the exit condition, not an error.

    The summary goes to the log because that is the only place a served run has to speak.

    There is a second way out, and it exists because the first one is not always
    reachable. The window opens on the machine running `serve`, which for a headless
    host means there is no window to close — so a session left open pinned the
    one-window lock until `serve` itself was restarted, and every later "Open prefilled"
    answered *"a prefilled window is already open"*. `Session.request_close` is the
    Done button on `/apply`, read here, on the thread that owns the browser.

    With a `session`, this is also where the dashboard's edits land. The tick was already
    here and already inside a Playwright call, so draining the queue in it costs nothing
    and inherits the one property that matters: every call to `page` happens on the
    thread that made it. Nothing else in the process may touch it.
    """
    log.info("%s — the window is yours; it closes when you close it", report.summary())
    for field_ in report.gaps:
        log.info("  needs you: %s", field_.label[:80])
    while True:
        try:
            pages = context.pages
            if not pages:
                break
            if session is not None:
                if session.close_requested():
                    log.info("closing the window — asked for from the page")
                    break
                # The surface is where the form was read, which is not the page itself
                # when the employer embeds its ATS in an iframe.
                here = surface if surface is not None else pages[0]
                # Before the drain, so a queued edit cannot land between the checks the
                # gate made and the click that acts on them.
                if session.submit_requested():
                    _submit(session, here, on_submitted)
                    continue
                _drain(session, here)
            pages[0].wait_for_timeout(HOLD_POLL_MS)
        except Exception:  # noqa: BLE001 — the browser is gone, which is the exit
            break
    if session is not None:
        # The window closing is the end of the session, and the page has to be able to
        # tell that from "still working". Without this it polls a form nobody is holding
        # any more and every edit queues into nothing.
        session.set_phase(live.CLOSED)


# How long to wait for the page to settle after the click before reading it. A submit is
# a navigation on every ATS we have seen; this is the ceiling, not the expectation.
SUBMIT_SETTLE_MS = 8000


def _submit(session, surface, on_submitted=None) -> None:
    """Send the application. The one irreversible thing in this project.

    Every check `Session.request_submit` made is re-taken here, and not out of caution
    for its own sake: that reading is up to one poll old, and a form can reveal a required
    question in the meantime. The gate answers the click; this answers the page.

    The claim comes before the click and is taken under the session lock, so two reads of
    the flag cannot become two applications.

    What follows the click is a reading, never a verdict. Nothing on this side can prove
    an employer received anything, so this records what changed — the URL, how many fields
    are still on the page — and lets the page say that. An unverifiable "submitted
    successfully" is how a failed send stops being re-checked.
    """
    page = _page_of(surface)
    control = session.snapshot()["submit_control"]
    blockers = session.unfilled_required()
    if not control or blockers:
        # The gate let it through and the page has changed since. Say so and stand down —
        # `submitted_at` is untouched, so the button comes back rather than jamming.
        session.set_phase(live.READY, "not sent: " + (
            f"still needed: {', '.join(blockers[:4])}" if blockers
            else "the submit button is no longer on the form"))
        session.disarm()
        return

    if not session.claim_submit():
        return  # already gone; the flag was read twice

    before_url = page.url
    before_fields = session.snapshot()["discovered"]
    log.info("submitting %s — clicking %r", session.title, control["label"][:60])

    # The one click in this module. A real press of the employer's own control, so their
    # validation, their required-field checks and their captcha hooks all run — which is
    # exactly what submitting the form programmatically would skip.
    surface.click('[data-jt-submit="go"]')

    try:
        page.wait_for_load_state("networkidle", timeout=SUBMIT_SETTLE_MS)
    except Exception as exc:  # noqa: BLE001 — a page that never settles is still a page
        log.debug("the page did not settle after the submit: %s", exc)
    page.wait_for_timeout(1000)

    try:
        _reread(session, surface)
    except Exception as exc:  # noqa: BLE001 — navigated away, which is the good outcome
        log.debug("could not read the form after the submit: %s", exc)
    _shoot(session, surface)

    after_url = page.url
    after_fields = session.snapshot()["discovered"]
    changed = after_url != before_url or after_fields != before_fields
    result = {
        "url_before": before_url,
        "url_after": after_url,
        "fields_before": before_fields,
        "fields_after": after_fields,
        "changed": changed,
        "note": (
            f"clicked {control['label'][:60]!r}; the page went to {after_url}"
            if after_url != before_url else
            f"clicked {control['label'][:60]!r}; the form went from {before_fields} "
            f"fields to {after_fields}" if changed else
            f"clicked {control['label'][:60]!r} and nothing on the page changed — "
            "read the preview before assuming it went"
        ),
    }
    session.finish_submit(result)
    if on_submitted is not None:
        try:
            on_submitted(result)
        except Exception:  # noqa: BLE001 — recording it must not lose the reading
            log.exception("could not record the application after submitting")


def _drain(session, page) -> None:
    """Carry the dashboard's queued commands to the live page, then keep the preview up.

    Runs on the browser thread, once per tick. Every command is one of four names and
    carries a handle rather than a selector, so there is nothing here that can be pointed
    at an arbitrary element or made to evaluate arbitrary text — see `live.py`.
    """
    while True:
        try:
            command = session.commands.get_nowait()
        except queue.Empty:
            break
        try:
            _obey(session, page, command)
        except Exception as exc:  # noqa: BLE001 — one bad command is not a dead session
            # Warning, not debug: `_write` already returns False for a field that simply
            # would not take the value, so reaching here means something structural, and
            # a mirror quietly dropping every edit is the failure-is-absence shape all
            # over again.
            log.warning("command %s failed: %s", command.kind, exc)

    if session.watching() and session.due_for_shot(SHOT_EVERY_S):
        _shoot(session, page)


def _obey(session, page, command) -> None:
    """One command. The epoch check is the load-bearing line."""
    if command.kind == live.SHOOT:
        _shoot(session, page)
        return
    if command.kind == live.REDISCOVER:
        _reread(session, page)
        return
    if command.kind not in (live.SET, live.CLEAR, live.HIGHLIGHT):
        return  # not in the vocabulary; `Session.submit` refused it too

    if command.epoch != session.epoch:
        # The form was read again since this was written, and the reading renumbered the
        # handles, so this one may now name a different input. Dropping it is the entire
        # purpose of the epoch: the alternative is putting an answer you did not give
        # into a field you cannot see.
        log.debug("stale %s for %s (epoch %s, now %s)",
                  command.kind, command.handle, command.epoch, session.epoch)
        return

    row = next((r for r in session.fields if r["handle"] == command.handle), None)
    if row is None:
        return

    if command.kind == live.HIGHLIGHT:
        page.evaluate(_HIGHLIGHT_JS, [command.handle])
        return

    # `_write` is the only writer, here as in the fill. It takes the raw DOM finding, of
    # which it reads exactly two keys — so the mirror row stands in for it directly and
    # no second write path exists to keep in step with the first.
    raw = {"handle": row["handle"], "type": row["type"], "label": row["label"]}

    if command.kind == live.CLEAR:
        done, status = _clear(page, raw), live.CLEARED
    else:
        # An empty `set` is refused rather than written. Playwright's `fill` would take it
        # happily and `_write` would return True, and the row would then read `filled`
        # holding nothing — counted as done, counted out of "need you", and indistinguish-
        # able from a question nobody ever answered. That is the reading `answers.py`
        # refuses for the same reason ("an empty answer is indistinguishable from a
        # missing one"). Emptying a field on purpose has its own name and its own status.
        if not command.value:
            log.debug("empty set for %s — clear it if that is what you meant",
                      command.handle)
            return
        done, status = _write(page, raw, command.value), live.FILLED

    if done:
        session.mark(command.handle, status, command.value)
        # A form that reveals a question once you answer another one is the ordinary
        # case, not an exotic one. Reading it again here is what stops the mirror going
        # stale exactly when you are making progress.
        _reread(session, page)
    else:
        session.mark(command.handle, live.REFUSED, "")
    _shoot(session, page)


def _reread(session, page) -> None:
    """Read the form again and republish it. Never navigates."""
    found = page.evaluate(_DISCOVER_JS)
    fields = _fields_from_dom(found)
    if session.absorb(live.rows_from(found, fields, session.carried())):
        log.info("the form changed shape — %d field(s), epoch %d",
                 len(found), session.epoch)
    # The button is re-found every time, because a form that reveals a question can just
    # as well reveal the control that sends it — and because a handle minted by an
    # earlier pass names nothing after the page has moved.
    session.set_submit_control(_find_submit(page))


def _find_submit(page) -> Optional[dict]:
    """The control that would send this form, or None if there is not one.

    None is a real answer and the caller treats it as one. A page with no submit control
    is a page this cannot send — the same finding as zero fields discovered, and it must
    never render as a button that quietly does nothing.
    """
    try:
        return page.evaluate(_SUBMIT_JS)
    except Exception as exc:  # noqa: BLE001 — no button is a finding, not a crash
        log.debug("could not look for a submit control: %s", exc)
        return None


def _shoot(session, surface) -> None:
    """A still of the real page, for the preview.

    A picture rather than a stream, deliberately: what this replaces was a stream, and
    the reason it was slow is that a stream is what it was. A few seconds of staleness on
    an image you glance at costs nothing; the fields you actually type into are local.

    **The whole page, not the window.** A browser viewport is 720 px and an application
    form is several thousand — the Asana one is 3352 — so a viewport shot showed the first
    five fields and nothing else, over a window nobody watching this page can scroll. The
    picture has to be of the form, not of the part of it Chromium happens to be showing.
    Scaling it to fit, or zooming back in, is the page's business and costs no bytes.
    """
    session.store_shot(
        _page_of(surface).screenshot(
            type="jpeg", quality=SHOT_QUALITY, full_page=True
        )
    )


def _write(page, raw: dict, value: str) -> bool:
    """Put `value` in one field. False if the field would not take it.

    Refusing is a real outcome, not an error: a dropdown that does not offer the option
    we hold is a question we cannot answer, and it belongs in the gap list next to the
    ones we never had an answer for.
    """
    selector = f'[data-jt-id="{raw["handle"]}"]'
    kind = raw["type"]
    try:
        if kind == "file":
            path = Path(value)
            if not path.is_file():
                return False
            page.set_input_files(selector, str(path))
            return True
        if kind in ("select", "multiselect"):
            page.select_option(selector, label=value)
            return True
        if kind == "checkbox":
            if str(value).strip().lower() in ("yes", "true", "1", "on"):
                page.check(selector)
                return True
            return False
        page.fill(selector, value)
        return True
    except Exception as exc:  # noqa: BLE001 — an unfillable field is a gap, not a crash
        log.debug("could not fill %s (%s): %s", raw.get("label"), kind, exc)
        return False


def _clear(page, raw: dict) -> bool:
    """Empty one field. False if the field would not give the value up.

    The exact inverse of `_write`, one branch per branch, and it exists as its own
    function for the same reason `_write` is the only writer: there is one place that
    knows how each kind of control is operated, and clearing is an operation on the same
    four kinds.

    Every call here is Playwright's own primitive for "this field now holds nothing", and
    each fires the DOM's `input`/`change` events on the way — which is what a React or
    Ashby-style controlled component needs in order to believe it. Setting `.value = ''`
    from injected script would leave the framework's own state holding the old answer,
    and the field would repopulate itself the moment anything else on the form changed.

    `uncheck` is the mirror of the `check` `_write` already does. Neither can reach a
    submit control: `_DISCOVER_JS` never mints a handle for one.
    """
    selector = f'[data-jt-id="{raw["handle"]}"]'
    kind = raw["type"]
    try:
        if kind == "file":
            page.set_input_files(selector, [])
            return True
        if kind in ("select", "multiselect"):
            page.select_option(selector, [])
            return True
        if kind == "checkbox":
            page.uncheck(selector)
            return True
        page.fill(selector, "")
        return True
    except Exception as exc:  # noqa: BLE001 — a field that will not empty is not a crash
        log.debug("could not clear %s (%s): %s", raw.get("label"), kind, exc)
        return False


def _remember(conn, company: str, field_: FormField, question_key, today: str) -> None:
    store.upsert_form_field(
        conn,
        company=company,
        form_key=field_.key,
        label=field_.label,
        field_type=field_.type,
        now=today,
        required=field_.required,
        options=json.dumps(list(field_.options)) if field_.options else None,
        question_key=question_key,
        source="dom",
    )


def _print_gaps(report: FillReport) -> None:
    if not report.found_a_form:
        print("  The page rendered but had no form on it. It may be a careers page that")
        print("  only links to the application, or a login wall. Open it and check:")
        print(f"    {report.url}")
        return
    if not report.gaps:
        print("  nothing left — every field it found is filled.")
        return
    print(f"\n  {len(report.gaps)} field(s) need you:")
    for field_ in report.gaps[:20]:
        mark = "*" if field_.required else " "
        detail = f" [{' | '.join(field_.options[:4])}]" if field_.options else ""
        print(f"   {mark} {field_.label[:70]}{detail}")
    if len(report.gaps) > 20:
        print(f"     … and {len(report.gaps) - 20} more")
    if report.new_questions:
        print(f"\n  {report.new_questions} of these are new — they have been added to "
              f"answers.yaml and the Settings tab.")
