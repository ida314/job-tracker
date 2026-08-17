"""What a message is, and whether it is about a job you applied to. Pure.

No filesystem, no SQL, no HTML — `maildir.py` does the reading and `store.py` the
writing, the same split `match.py` has against `fetch.py`. That is what makes the
interesting half testable from a byte string.

The narrowing here is the deterministic detector DESIGN.md §8 asks for: it decides *that*
a message is relevant, and the model is then asked only *what it means*. Every index it
matches against is built from rows that already exist in `applications`, so a message can
never be a candidate for a company you never applied to. That is not a rule enforced
somewhere; it is the shape of the data.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.utils import getaddresses, parsedate_to_datetime
from typing import Optional

# What the model is shown. The tail of a recruiting mail is signatures, legal footers and
# unsubscribe boilerplate; nothing in it changes what stage the message is about.
MAX_MAIL_CHARS = 4000
# What the review card shows, so you can tell at a glance whether the reading is right.
MAX_SNIPPET_CHARS = 300
# How many applications the model may be offered at an ambiguous company.
MAX_CHOICES = 8
# Company names shorter than this are not matched by name at all: "X" and "Hex" would
# fire on half the inbox.
MIN_NAME_CHARS = 3
# A vendor job id has to look like one before it is hunted for in prose. "12" appears in
# every phone number and every date.
MIN_JOB_ID_CHARS = 6

# Shared relays. These identify an *ATS*, never a company — §7.2's "reachability is not
# identity", one layer up. `no-reply@greenhouse.io` is proof that a hosted board sent the
# mail and proof of nothing about whose board it was, so a message from one of these still
# has to name its company somewhere a sender says who they are.
ATS_DOMAINS = frozenset({
    "greenhouse.io", "greenhouse-mail.io", "us.greenhouse-mail.io",
    "lever.co", "hire.lever.co", "ashbyhq.com", "us.ashbyhq.com",
    "myworkday.com", "myworkdayjobs.com", "icims.com", "smartrecruiters.com",
    "workable.com", "jobvite.com", "bamboohr.com", "rippling.com",
})

# Words that make a message plausibly about an application rather than about a company.
# They corroborate the weakest signal only — a company name in a subject line — because
# "This week in tech: Stripe, Ramp and more" names two employers you applied to and is
# about neither of your applications. Measured against a real newsletter, not theorized.
APPLICATION_HINTS = (
    "application", "applied", "applying", "candidacy", "candidate", "recruit",
    "interview", "screen", "assessment", "take home", "offer", "hiring team",
    "your submission", "next steps", "position", "role",
)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+")
_URL = re.compile(r"https?://[^\s<>\"')]+", re.I)


@dataclass(frozen=True)
class Message:
    """One parsed message. Everything downstream reads this and never the raw bytes."""

    message_id: str
    from_addr: str = ""
    from_name: str = ""
    subject: str = ""
    sent_at: str = ""              # the raw Date header; nothing may sort on it
    sent_on: Optional[str] = None  # normalized ISO day; the only comparable one
    body: str = ""
    urls: tuple[str, ...] = ()

    @property
    def haystack(self) -> str:
        """Where a job id or a title may legitimately be looked for."""
        return f"{self.subject}\n{self.body}"


@dataclass(frozen=True)
class Candidate:
    """A message the narrower tied to something you applied to."""

    message: Message
    company: str
    ats_job_id: str = ""            # '' = the company matched, no single job did
    choices: tuple[str, ...] = ()   # what the model may point at
    match_kind: str = ""
    evidence: str = ""              # the literal text that matched, shown to you

    @property
    def snippet(self) -> str:
        return snippet(self.message.body)


@dataclass
class Index:
    """Everything the narrower matches against, built once per scan.

    Built from `applications` and `companies.yaml` — never invented. A domain in
    particular is *read* off a URL you actually applied at, never synthesized from a
    company name: guessing `stripe.com` from "Stripe" is the `ashby/cedar` mistake with a
    new coat on.
    """

    by_url: dict = field(default_factory=dict)        # normalized url -> (company, job)
    by_job_id: dict = field(default_factory=dict)     # long job id  -> (company, job)
    by_domain: dict = field(default_factory=dict)     # domain       -> company
    by_name: dict = field(default_factory=dict)       # normalized name -> company
    apps: dict = field(default_factory=dict)          # company -> [rows], best first
    floor: Optional[str] = None                       # nothing older than this matters


# -- parsing --------------------------------------------------------------------------
def normalize(text: str) -> str:
    """Fold text to a comparison form: lowercase, alphanumerics, single spaces."""
    return " ".join(_TOKEN.findall((text or "").lower()))


def tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def normalize_url(url: str) -> str:
    """A URL reduced to what identifies the posting, so two spellings of one link agree."""
    url = (url or "").strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = url.split("#", 1)[0].split("?", 1)[0]
    return url.rstrip("/")


def registrable_domain(host: str) -> str:
    """`jobs.eu.stripe.com` -> `stripe.com`. Two labels, which is wrong for `co.uk` and
    right for everything this matches; the cost of being wrong is a domain that fails to
    match, and the name check behind it still fires."""
    host = (host or "").strip().lower().split("@")[-1].split(":")[0].rstrip(".")
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _body_of(msg) -> str:
    """The text of a message, preferring text/plain and stripping HTML if that is all
    there is. Never raises: a MIME structure nobody anticipated is an empty body, which
    `pending()` treats as work the task cannot do."""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001 — email's walkers raise a wide variety
        part = None
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:  # noqa: BLE001
        try:
            text = part.get_payload(decode=True).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""
    if part.get_content_type() == "text/html":
        text = _TAG.sub(" ", text)
    return _WS.sub(" ", text).strip()


def synthetic_id(from_addr: str, sent_at: str, subject: str, size: int) -> str:
    """An identity for a message whose sender omitted Message-ID.

    Absent must not mean "new every night", and the maildir filename cannot stand in — a
    client renames it the moment you read the message. Same fallback shape as
    `store.manual_job_id` and the aggregator's stable ids.
    """
    digest = hashlib.sha1(
        f"{from_addr}\x00{sent_at}\x00{subject}\x00{size}".encode("utf-8", "replace")
    ).hexdigest()[:16]
    return f"synth:{digest}"


def parse(raw: bytes) -> Optional[Message]:
    """Bytes to a `Message`, or None if it is not a message at all.

    Never raises. Mail in a real mailbox is malformed in ways no specification predicts,
    and a scan that dies on message 3,000 of 5,000 has silently reported the other 2,000
    as the whole story.
    """
    try:
        msg = message_from_bytes(raw, policy=policy.default)
    except Exception:  # noqa: BLE001
        return None

    def header(name: str) -> str:
        try:
            return str(msg.get(name, "") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    pairs = []
    try:
        pairs = getaddresses([header("From")])
    except Exception:  # noqa: BLE001
        pairs = []
    from_name, from_addr = (pairs[0] if pairs else ("", ""))

    sent_at = header("Date")
    sent_on = None
    if sent_at:
        try:
            sent_on = parsedate_to_datetime(sent_at).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            # Admitted anyway, sorted last. Dropping mail for a bad Date header is
            # failure-as-absence in the feature whose whole job is to catch what you
            # missed.
            sent_on = None

    subject = _WS.sub(" ", header("Subject"))
    body = _body_of(msg)
    mid = header("Message-ID").strip("<>").strip()

    # `email` is tolerant to a fault: it turns an arbitrary byte string into a Message
    # with no headers rather than raising, so "it parsed" is not evidence that this was
    # mail. A file with no From, no Subject and no Date is a truncated spool file or
    # something that is not mail at all, and the scan should count it rather than skip
    # it silently — otherwise the degraded exit code can never fire.
    if not any((from_addr, subject, sent_at, mid)):
        return None

    if not mid:
        mid = synthetic_id(from_addr, sent_at, subject, len(raw))

    return Message(
        message_id=mid,
        from_addr=from_addr.lower(),
        from_name=from_name,
        subject=subject,
        sent_at=sent_at,
        sent_on=sent_on,
        body=body[:MAX_MAIL_CHARS],
        urls=tuple(_URL.findall(body)[:40]),
    )


def snippet(body: str) -> str:
    text = _WS.sub(" ", body or "").strip()
    return text[:MAX_SNIPPET_CHARS]


# -- narrowing ------------------------------------------------------------------------
def build_index(applications, companies=()) -> Index:
    """Everything the narrower is allowed to recognize.

    `applications` are rows (or dicts) with company, ats_job_id, title, url, status and
    updated_at. `companies` is `companies.yaml`, used only for careers-page domains — and
    only for companies you have actually applied to.
    """
    idx = Index()
    by_company_name = {c.name: c for c in companies}

    rows = sorted(
        applications,
        key=lambda a: (_status_rank(_get(a, "status")), _get(a, "updated_at") or ""),
        reverse=True,
    )
    for app in rows:
        company = _get(app, "company")
        job = _get(app, "ats_job_id")
        if not company or not job:
            continue
        idx.apps.setdefault(company, []).append(app)

        url = _get(app, "url")
        if url:
            idx.by_url.setdefault(normalize_url(url), (company, job))
            host = normalize_url(url).split("/")[0]
            domain = registrable_domain(host)
            if domain and domain not in ATS_DOMAINS:
                idx.by_domain.setdefault(domain, company)

        # A job id is only hunted for in prose when it looks like a vendor id. Short ones,
        # and the minted `manual:` ones, are not.
        if len(job) >= MIN_JOB_ID_CHARS and any(c.isdigit() for c in job) \
                and not job.startswith("manual:"):
            idx.by_job_id.setdefault(job.lower(), (company, job))

        name = normalize(company)
        if len(name) >= MIN_NAME_CHARS:
            idx.by_name.setdefault(name, company)

        cy = by_company_name.get(company)
        if cy is not None and cy.careers_page:
            domain = registrable_domain(normalize_url(cy.careers_page).split("/")[0])
            if domain and domain not in ATS_DOMAINS:
                idx.by_domain.setdefault(domain, company)

    applied = [_get(a, "applied_at") for a in applications if _get(a, "applied_at")]
    if applied:
        # Nothing that predates your first application can be a reply to one. This is the
        # cheap half of bounding a 50,000-message store; the rest is `--since`.
        idx.floor = min(applied)[:10]
    return idx


def _get(row, key: str):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _status_rank(status: Optional[str]) -> int:
    """Active applications are offered before closed ones. A reply is far more likely to
    be about something still in play than about a rejection from March."""
    from .store import CLOSED_STATUSES

    return 0 if status in CLOSED_STATUSES else 1


def narrow(message: Message, index: Index, since: Optional[str] = None) -> Optional[Candidate]:
    """Tie a message to something you applied to, or return None.

    First hit wins, strongest first, and which one fired is recorded — the review card
    shows it, because "we matched your company's own domain" and "your company's name was
    in the subject line" are not equally good reasons to believe anything.
    """
    floor = max([d for d in (index.floor, since) if d], default=None)
    if floor and message.sent_on and message.sent_on < floor:
        return None

    # 1. A URL you applied at, in the body. Resolves company and job at once.
    for url in message.urls:
        hit = index.by_url.get(normalize_url(url))
        if hit:
            return _bind(message, index, hit[0], hit[1], "job_url", url)

    # 2. A vendor job id, in the subject or body.
    hay = message.haystack.lower()
    for job_id, (company, job) in index.by_job_id.items():
        if job_id in hay:
            return _bind(message, index, company, job, "job_id", job_id)

    # 3. The company's own domain, as the sender. Never an ATS relay: that names the ATS.
    domain = registrable_domain(message.from_addr)
    if domain and domain not in ATS_DOMAINS:
        company = index.by_domain.get(domain)
        if company:
            return _bind(message, index, company, "", "company_domain", domain)

    # 4. The company's name — and only in the two places a sender says who they *are*.
    # Never the body: every newsletter in the world mentions Stripe there.
    display = tokens(message.from_name)
    for name, company in index.by_name.items():
        if _contains_tokens(display, name.split()):
            return _bind(message, index, company, "", "company_name", name)

    # 5. The company's name in the subject, which is the weakest signal here and the only
    # one that needs corroborating: a subject names who a message is *about*, and a
    # digest naming three employers you applied to is about none of your applications.
    # One application-shaped word is the corroboration, and it is still deterministic.
    subject_tokens = tokens(message.subject)
    if _looks_like_application_mail(message):
        for name, company in index.by_name.items():
            if _contains_tokens(subject_tokens, name.split()):
                return _bind(message, index, company, "", "company_subject", name)
    return None


def _looks_like_application_mail(message: Message) -> bool:
    hay = f"{message.subject}\n{message.body}".lower()
    return any(hint in hay for hint in APPLICATION_HINTS)


def _contains_tokens(haystack: list[str], needle: list[str]) -> bool:
    """Whole tokens in order, so "Ramp" never fires on "Rampart"."""
    if not needle or len(needle) > len(haystack):
        return False
    for i in range(len(haystack) - len(needle) + 1):
        if haystack[i:i + len(needle)] == needle:
            return True
    return False


def _bind(message, index, company, job, kind, evidence) -> Candidate:
    """Attach the message to one application if that can be decided without a model."""
    rows = index.apps.get(company, [])
    if not job:
        hay = normalize(message.haystack)
        for app in rows:
            title = normalize(_get(app, "title") or "")
            if title and title in hay:
                job, kind = _get(app, "ats_job_id"), "title"
                break
    if not job and len(rows) == 1:
        job, kind = _get(rows[0], "ats_job_id"), "sole_open"

    choices = tuple(_get(a, "ats_job_id") for a in rows[:MAX_CHOICES])
    return Candidate(
        message=message, company=company, ats_job_id=job or "",
        choices=choices, match_kind=kind, evidence=str(evidence)[:200],
    )
