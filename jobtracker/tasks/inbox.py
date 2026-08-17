"""Task: read one email and say which application stage it is evidence of.

The fifth bounded model role (DESIGN.md §8), and it is bounded twice over. The
deterministic narrower in `mail.py` has already decided *that* the message is about
something you applied to; this only decides *what it means*. And what it may say is an
enum — one of the seven statuses or `none`, plus one of the applications it was offered,
plus a quote it must be able to point at in the message.

    jobtracker mail        read the maildir, narrow, cache   -> mail_candidates
    work --task inbox      this: read one, propose one thing -> mail_proposals
    /applications          you accept                        -> advance_application

Nothing here writes to `applications`. A proposal is a claim awaiting your ruling, and
accepting it is a separate, human action — the same shape `repair` has for slugs, and for
the same reason: the model is an exception handler, never the main loop.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from .. import store
from ..maildir import is_maildir
from .base import Task, TaskContext, TaskUnit, register

log = logging.getLogger("jobtracker.tasks.inbox")

# The seven statuses, plus the answer that is not one.
NO_NEWS = "none"

SCHEMA_NAME = "mail_reading"

SYSTEM_PROMPT = """\
You read ONE email a job applicant received and report only which application stage it is
evidence of.

Rules:
- "none" is a correct and common answer. A job alert, a newsletter, an application
  receipt, a scheduling link with no outcome, a recruiter cold-email about a different
  role, or anything you are unsure about is "none". A wrong reading puts a false entry in
  a history the applicant relies on; "none" costs nothing.
- You never advise, summarize, or compose. `quote` must be ONE sentence copied verbatim
  from the email that shows the stage. If there is no such sentence, answer "none".
- `application` must be one of the ids you were offered, or "none" if the email does not
  say which role it is about.
- An interview invitation is "interview" however many rounds in it is. A request to
  schedule a first call is "screen". A take-home or coding assessment is "oa".
"""

MAX_BODY_CHARS = 4000


def schema_for(choices: list[str]) -> dict:
    """The model may point at an application it was offered, and at nothing else.

    Same shape as `prefill`'s answer-key enum, and for the same reason: an id it invented
    would name a row that does not exist, and the failure would be silent.
    """
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": [*store.APPLICATION_STATUSES, NO_NEWS],
            },
            "application": {"type": "string", "enum": [*choices, NO_NEWS]},
            "quote": {"type": "string"},
        },
        "required": ["status", "application", "quote"],
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class Reading:
    status: Optional[str]     # None = this is not application news
    application: str = ""
    quote: str = ""


_WS = re.compile(r"\s+")


def _flat(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip()


def parse_reading(text: Optional[str], choices, body: str) -> Optional[Reading]:
    """The model's answer, or None for "no answer" — which writes nothing.

    Four refusals, and the fourth is the one that matters. The quote is the only free text
    the model produces here, so it has to be *grounded*: it must appear in the message it
    was shown. That is `repair`'s rule that a proposed slug must appear on the page it was
    read from, and it is what keeps a fabricated rejection off the review list.
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    status = data.get("status")
    if status not in (*store.APPLICATION_STATUSES, NO_NEWS):
        return None
    if status == NO_NEWS:
        # An answer, not a failure: the message was read and it is not application news.
        return Reading(status=None)

    application = data.get("application") or NO_NEWS
    if application not in (*list(choices), NO_NEWS):
        return None

    quote = _flat(str(data.get("quote") or ""))
    if not quote or quote not in _flat(body):
        return None
    return Reading(
        status=status,
        application="" if application == NO_NEWS else application,
        quote=quote,
    )


class InboxTask(Task):
    name = "inbox"
    # Last. `level` -> `judge` -> `prefill` is a dependency chain and this is not in it:
    # it consumes nothing they produce and produces nothing they consume. So the ordering
    # rule does not decide this number, and the honest reason is starvation — the mail
    # queue refills from an external stream on its own schedule, and ahead of `level` a
    # busy inbox would keep the pipeline's own stages permanently waiting.
    priority = 40
    summary = "read replies from employers and propose what they mean"

    def unavailable_reason(self, ctx: TaskContext) -> Optional[str]:
        # A path, inspected — never opened. Reading the maildir is `jobtracker mail`'s
        # job; this task is a pure read of what that recorded, which is what keeps an
        # unmounted volume from quietly shrinking the queue.
        if not ctx.maildir:
            return "no maildir configured (set $JOBTRACKER_MAILDIR)"
        if not is_maildir(ctx.maildir):
            return f"{ctx.maildir} is not a maildir"
        return None

    def pending(self, conn, ctx, limit=None):
        keys = store.application_keys(conn)
        units: list[TaskUnit] = []
        for row in store.unread_mail_candidates(conn, limit=None):
            if not row["body"]:
                # Nothing to read is a guaranteed no-op, and counting it would overstate a
                # backlog no model could drain.
                continue
            live = [
                job for job in json.loads(row["choices"] or "[]")
                if (row["company"], job) in keys
            ]
            if not live:
                # The application was deleted since the scan. Work the task can no longer
                # do, so it is not queued work.
                continue
            units.append(TaskUnit(
                task=self.name,
                company=row["company"],
                ats_job_id=row["ats_job_id"] or "",
                # The message id, because the message IS the question. It also keeps two
                # ambiguous messages at one company — both carrying ats_job_id='' — from
                # sharing an `ident`, which would charge one's failures to the other and
                # let the router collapse two questions onto one answer.
                unit_key=row["message_id"],
                title=row["subject"] or "(no subject)",
                payload={
                    "body": row["body"],
                    "from": row["from_addr"],
                    "from_name": row["from_name"],
                    "sent_on": row["sent_on"],
                    "choices": live,
                    "titles": {
                        job: (store.get_application(conn, row["company"], job) or {})["title"]
                        for job in live
                    },
                },
            ))
            if limit is not None and len(units) >= limit:
                break
        return units

    async def run(self, unit, client, ctx):
        payload = unit.payload
        choices = payload["choices"]
        titles = payload.get("titles", {})
        offered = "\n".join(f"  {job} — {titles.get(job, '')}" for job in choices)
        text = await client.complete(
            system=SYSTEM_PROMPT,
            user=(
                f"From: {payload['from_name']} <{payload['from']}>\n"
                f"Date: {payload.get('sent_on') or 'unknown'}\n"
                f"Subject: {unit.title}\n\n"
                f"Applications at {unit.company} this could be about:\n{offered}\n\n"
                f"Email:\n{payload['body'][:MAX_BODY_CHARS]}"
            ),
            schema=schema_for(list(choices)),
            schema_name=SCHEMA_NAME,
            idempotency_key=unit.idempotency_key(),
            max_tokens=256,
        )
        return parse_reading(text, choices, payload["body"])

    def apply(self, conn, unit, reading: Reading, ctx):
        # Marked read first and unconditionally. "Read, and it is not application news" is
        # an answer worth keeping: without it every newsletter that squeaked through the
        # narrower would be re-read on every run forever.
        store.mark_mail_read(conn, unit.unit_key, ctx.today)
        if reading.status is None:
            return "nothing"

        job = reading.application or unit.ats_job_id
        if job:
            app = store.get_application(conn, unit.company, job)
            if app is None:
                return "gone"
            # A message repeating the stage you are already at is not news. `interview` is
            # exempt because it is one repeatable status by design — a second invitation is
            # a genuine second round, and suppressing it is the mistake that folding the
            # event append into the upsert would make.
            if app["status"] == reading.status and reading.status != "interview":
                return "already"

        store.record_mail_proposal(
            conn,
            message_id=unit.unit_key,
            company=unit.company,
            ats_job_id=job,
            status=reading.status,
            evidence=reading.quote[:300],
            now=ctx.today,
        )
        log.info("mail proposes %s at %s — %s",
                 reading.status, unit.company, unit.title[:60])
        return f"proposes {reading.status}"


register(InboxTask())
