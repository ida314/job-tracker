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

**It never submits.** There is no click path in this module at all, and a test asserts
that. It fills what it knows, outlines what it does not, and hands you the window. An
application is irreversible and goes out under your name.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import store
from .answers import normalize_label, slugify
from .models import FormField
from .tasks.prefill import resolve_field

log = logging.getLogger("jobtracker.browser")


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed, or has no browser to drive."""


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


def apply_url_for(
    url: str, ats: str, slug: str = "", ats_job_id: str = ""
) -> str:
    """The page carrying the application form, given the posting's URL.

    Kept here rather than in `sources/` because it is a fact about the *hosted careers
    page*, not about the JSON API those adapters exist to speak.

    Greenhouse gets special handling that is not cosmetic. Its `absolute_url` is
    whatever the employer configured, and large employers point it at their own careers
    site: Stripe's is `stripe.com/jobs/search?gh_jid=8077887`, a search page with no form
    on it at all. Observed on 2026-08-13 — the browser found zero fields there. The
    hosted board always carries the real form and we already know the slug and the job
    id, so it is used whenever both are available, with the employer's own URL as the
    fallback.
    """
    base = (url or "").rstrip("/")
    if ats == "greenhouse" and slug and ats_job_id:
        return f"{GREENHOUSE_BOARD}/{slug}/jobs/{ats_job_id}#app"
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
) -> FillReport:
    """Open the application, fill what we know, record what we do not, and stop.

    Writes to `conn` — the form it read and any question it could not answer — because
    a visit that taught us an employer's form must not have to be repeated. It never
    writes an application anywhere, and it never clicks anything.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise BrowserUnavailable(
            "playwright is not installed. `pip install 'jobtracker[browser]'` "
            "then `playwright install chrome`."
        ) from exc

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
            page.goto(target, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(1500)  # let a SPA render its form

            found = page.evaluate(_DISCOVER_JS)
            report.discovered = len(found)
            fields = _fields_from_dom(found)
            log.info("%s: %d field(s) on %s", company.name, len(fields), target)

            unfilled_required: list[str] = []
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
                    if field_.required:
                        unfilled_required.append(raw["handle"])
                    _remember(conn, company.name, field_, None, today)
                    continue

                if _write(page, raw, value):
                    report.filled.append(Filled(
                        handle=raw["handle"], label=field_.label,
                        type=field_.type, value=value, question_key=question_key,
                    ))
                    _remember(conn, company.name, field_, question_key, today)
                else:
                    report.gaps.append(field_)
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

            page.evaluate(_HIGHLIGHT_JS, unfilled_required)

            if wait and not headless:
                print(f"\n{report.summary()}")
                _print_gaps(report)
                print("\nThe window is yours. Review, then submit it yourself.")
                try:
                    input("Press Enter here when you are done to close the browser… ")
                except (EOFError, KeyboardInterrupt):
                    pass
        finally:
            context.close()

    return report


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
