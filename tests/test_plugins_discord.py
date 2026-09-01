"""Reading one Discord channel: snowflakes, paging, and the format registry.

The properties here, and what each protects:

  * **Snowflake arithmetic**, because it is what lets the first read cover an exact
    window with no extra API call and no stored state.
  * **The two failure modes that answer 200 with nothing.** Missing Read Message History
    returns an empty list, not a 403; a missing MESSAGE CONTENT intent returns every
    message with the text stripped. Both look exactly like a quiet channel, and reporting
    either as "no jobs" is the most expensive silence this plugin could produce.
  * **The formats.** `cscareers` reads a real bot's template; `generic` never guesses an
    employer; a format that raises falls through rather than killing the poll.

Everything runs against recorded payloads. No network.
"""

from jobtracker.plugins import discord as dmod
from jobtracker.plugins.discord.formats import base as fmt_base


def _msg(mid="1300000000000000001", content="", bot=True,
         ts="2026-08-30T12:00:00.000000+00:00", embeds=None):
    return {
        "id": mid,
        "timestamp": ts,
        "author": {"bot": bot, "username": "jobs-bot"},
        "content": content,
        "embeds": embeds or [],
    }


SAMPLE = (
    "## [Software Developer Associate @ Artera]"
    "(<https://jobs.lever.co/artera-2/eae88c70-fbf5-4525-890c-d3f9377418b0>)\n"
    "### Locations:  Seattle, WA\n"
    "### Sponsorship: `Unknown`\n"
    "Posted on: July 31, 2026"
)

SETTINGS = {"channel_id": "111", "guild_id": "222", "label": "jobs", "backfill_days": 14}


def _parse_one(message, today="2026-08-31"):
    posts, unparsed, skipped = dmod.Discord().parse_page(
        "Discord: #jobs", [message], SETTINGS, today
    )
    return posts[0] if posts else None


# -- snowflakes ----------------------------------------------------------------------
def test_a_snowflake_built_from_a_date_round_trips_to_that_day():
    assert dmod.day_of(dmod.snowflake_at("2026-08-31", 0)) == "2026-08-31"
    assert dmod.day_of(dmod.snowflake_at("2026-08-31", 14)) == "2026-08-17"


def test_the_first_read_covers_exactly_the_configured_window_with_no_api_call():
    """A Discord id contains its own timestamp, so "everything since the 17th" is
    expressible as an id without asking Discord what ids existed then."""
    cursor = dmod.Discord().first_cursor({"backfill_days": 30}, "2026-08-31")
    assert dmod.day_of(cursor) == "2026-08-01"


def test_a_snowflake_sorts_in_the_same_order_as_the_days_it_encodes():
    older = int(dmod.snowflake_at("2026-08-01"))
    newer = int(dmod.snowflake_at("2026-08-31"))
    assert older < newer


def test_an_unreadable_cursor_reads_as_no_day_rather_than_raising():
    assert dmod.day_of("not-a-snowflake") is None
    assert dmod.day_of("") is None


# -- the two 200-with-nothing failures ------------------------------------------------
def test_a_discord_error_object_is_a_failure_not_an_empty_page():
    """Missing access answers with a JSON object, not a list."""
    err = dmod.Discord().page_error({"message": "Missing Access", "code": 50001})
    assert err and "Missing Access" in err


def test_a_page_where_no_message_has_content_is_an_intent_error_not_an_empty_channel():
    """Without MESSAGE CONTENT every message arrives, correctly authored, with the text
    stripped — so every format returns None and a channel full of jobs reports zero."""
    page = [_msg(mid="1", content=""), _msg(mid="2", content="")]
    err = dmod.Discord().page_error(page)
    assert err and "MESSAGE CONTENT" in err


def test_one_empty_message_is_ordinary_and_is_not_an_intent_error():
    """An attachment-only post is normal. A whole page of blanks is configuration."""
    page = [_msg(mid="1", content=""), _msg(mid="2", content="a real post")]
    assert dmod.Discord().page_error(page) is None


def test_an_empty_page_is_a_usable_answer():
    """Nothing new is the normal reading for an incremental feed."""
    assert dmod.Discord().page_error([]) is None


def test_the_cursor_is_read_off_the_raw_page_not_off_the_postings():
    page = [_msg(mid="10"), _msg(mid="30"), _msg(mid="20")]
    assert dmod.Discord().page_cursor(page) == "30"
    assert dmod.Discord().page_cursor([]) is None


# -- who gets imported ---------------------------------------------------------------
def test_only_messages_from_bots_are_imported():
    posts, unparsed, skipped = dmod.Discord().parse_page(
        "G", [_msg(mid="1", content=SAMPLE), _msg(mid="2", content="nice find!", bot=False)],
        SETTINGS, "2026-08-31",
    )
    assert len(posts) == 1 and skipped == 1


def test_a_human_message_is_skipped_not_counted_as_unreadable():
    """Two different facts. "Skipped" is a message we understood and declined;
    "unreadable" is one the formats could not parse, and only the second is a signal
    that the bot changed shape."""
    _, unparsed, skipped = dmod.Discord().parse_page(
        "G", [_msg(mid="2", content="hello", bot=False)], SETTINGS, "2026-08-31"
    )
    assert (unparsed, skipped) == (0, 1)


def test_a_bot_message_with_nothing_readable_is_counted_as_unreadable():
    _, unparsed, skipped = dmod.Discord().parse_page(
        "G", [_msg(mid="2", content="   ")], SETTINGS, "2026-08-31"
    )
    assert (unparsed, skipped) == (1, 0)


# -- the cscareers format ------------------------------------------------------------
def test_the_cscareers_format_reads_role_employer_url_location_and_the_posted_date():
    post = _parse_one(_msg(content=SAMPLE))
    assert post.title == "Artera — Software Developer Associate"
    assert post.url == "https://jobs.lever.co/artera-2/eae88c70-fbf5-4525-890c-d3f9377418b0"
    assert post.location == "Seattle, WA"
    assert post.posted_on == "2026-07-31"


def test_the_posted_on_line_beats_the_discord_message_timestamp():
    """The message reached Discord on the 30th of August; the req opened on the 31st of
    July. Filing it under the message date would make every import look brand new and
    float stale reqs to the top of the ranking."""
    post = _parse_one(_msg(content=SAMPLE, ts="2026-08-30T12:00:00+00:00"))
    assert post.posted_on == "2026-07-31"


def test_the_angle_brackets_that_suppress_the_embed_do_not_reach_the_stored_url():
    post = _parse_one(_msg(content=SAMPLE))
    assert "<" not in post.url and ">" not in post.url


def test_the_message_id_is_the_stable_posting_id():
    """Discord's own identifier, monotonic, and the same thing the cursor is made of —
    so no hashing is needed, unlike the aggregator's synthetic ids."""
    post = _parse_one(_msg(mid="1300000000000000042", content=SAMPLE))
    assert post.ats_job_id == "1300000000000000042"


def test_a_cscareers_message_missing_a_field_yields_an_empty_field_not_an_exception():
    trimmed = "## [Backend Engineer @ Acme](<https://jobs.lever.co/acme/x1>)"
    post = _parse_one(_msg(content=trimmed))
    assert post.title == "Acme — Backend Engineer"
    assert post.location == "" and post.posted_on is None


def test_a_restyled_field_line_still_parses():
    """The bot restyling `### Locations:` to `**Locations:**` next cycle must not turn
    every message unparseable at once."""
    restyled = (
        "## [Backend Engineer @ Acme](<https://jobs.lever.co/acme/x1>)\n"
        "**Locations:** Austin, TX\n"
        "**Posted on:** August 2, 2026"
    )
    post = _parse_one(_msg(content=restyled))
    assert post.location == "Austin, TX" and post.posted_on == "2026-08-02"


def test_a_heading_of_any_depth_is_still_a_headline():
    deep = "#### [Backend Engineer @ Acme](<https://jobs.lever.co/acme/x1>)"
    assert _parse_one(_msg(content=deep)).title == "Acme — Backend Engineer"


def test_an_unparseable_date_is_none_not_today():
    """A missing date reading as "posted now" would invert the ranking it informs."""
    bad = (
        "## [Backend Engineer @ Acme](<https://jobs.lever.co/acme/x1>)\n"
        "Posted on: sometime last spring"
    )
    assert _parse_one(_msg(content=bad)).posted_on is None


def test_a_headline_with_no_at_separator_falls_through_to_generic():
    """Inventing an employer from half a string is not an option; falling through is."""
    no_sep = "## [Backend Engineer](<https://jobs.lever.co/acme/x1>)"
    post = _parse_one(_msg(content=no_sep))
    assert post.title == "Backend Engineer"  # generic: no employer prefix


# -- embeds --------------------------------------------------------------------------
def test_the_same_posting_parses_identically_from_an_embed_and_from_markdown():
    """An embed's title-plus-url is a markdown headline in another costume. Rendering it
    back that way is what stops "the bot switched to embeds" silently emptying the feed."""
    embed = _msg(mid="1300000000000000001", content="", embeds=[{
        "title": "Software Developer Associate @ Artera",
        "url": "https://jobs.lever.co/artera-2/eae88c70-fbf5-4525-890c-d3f9377418b0",
        "fields": [
            {"name": "Locations", "value": "Seattle, WA"},
            {"name": "Sponsorship", "value": "Unknown"},
            {"name": "Posted on", "value": "July 31, 2026"},
        ],
    }])
    from_embed = _parse_one(embed)
    from_markdown = _parse_one(_msg(mid="1300000000000000001", content=SAMPLE))
    assert from_embed.title == from_markdown.title
    assert from_embed.url == from_markdown.url
    assert from_embed.location == from_markdown.location
    assert from_embed.posted_on == from_markdown.posted_on


# -- the generic fallback -------------------------------------------------------------
def test_the_generic_format_never_guesses_an_employer():
    """A guessed employer becomes a company name in a tracker whose whole discipline is
    that identity is verified rather than inferred."""
    post = _parse_one(_msg(content="New grad SWE at Zeta https://boards.greenhouse.io/zeta/jobs/7"))
    assert post.title == "New grad SWE at Zeta"
    assert " — " not in post.title


def test_the_generic_format_dates_by_the_message_because_it_has_nothing_better():
    post = _parse_one(_msg(content="SWE https://x.example/1", ts="2026-08-30T12:00:00+00:00"))
    assert post.posted_on == "2026-08-30"


def test_a_message_with_no_link_still_gets_a_url_back_to_itself():
    """`postings.url` is NOT NULL, and a permalink is honest about where it came from."""
    post = _parse_one(_msg(mid="55", content="Hiring backend engineers, DM me"))
    assert post.url == "https://discord.com/channels/222/111/55"


def test_a_bare_pasted_link_does_not_become_its_own_job_title():
    post = _parse_one(_msg(content="https://jobs.lever.co/acme/x1\nBackend Engineer at Acme"))
    assert post.title == "Backend Engineer at Acme"


def test_a_format_that_raises_falls_through_to_generic_instead_of_failing_the_poll():
    """"Never raises" is a promise one malformed date can break inside strptime, and the
    cost of believing it would be a poll that dies half way through a channel."""
    class _Exploding(fmt_base.Format):
        name = "exploding"

        def matches(self, message, text):
            return True

        def parse(self, message, ctx):
            raise ValueError("boom")

    fmt_base.register(_Exploding())
    try:
        post = _parse_one(_msg(content="Backend Engineer https://x.example/1"))
        assert post is not None and post.title == "Backend Engineer"
    finally:
        fmt_base._FORMATS[:] = [
            f for f in fmt_base._FORMATS if f.name != "exploding"
        ]


def test_the_fallback_sorts_last_whatever_the_import_order():
    assert [f.name for f in fmt_base.formats()][-1] == "generic"


# -- what the rest of the pipeline sees ------------------------------------------------
def test_the_message_text_is_the_cached_description():
    post = _parse_one(_msg(content=SAMPLE))
    assert "Software Developer Associate" in post.description
    assert "Sponsorship" in post.description


def test_sponsorship_rides_in_the_description_and_reaches_neither_title_nor_criteria():
    """A sponsorship rule would be a gate applied before any title is read — the
    `locations_exclude` mistake, which discarded 390 postings unseen."""
    post = _parse_one(_msg(content=SAMPLE))
    assert "Sponsorship" in post.description
    assert "Sponsorship" not in post.title


def test_an_imported_posting_matches_the_existing_rules_with_no_criteria_changes():
    from jobtracker.criteria import load_criteria
    from jobtracker.match import match

    post = _parse_one(_msg(content=SAMPLE))
    verdict = match(post, load_criteria("criteria.yaml"))
    assert verdict.decision.value == "match"


def test_the_employer_is_folded_into_the_title_the_way_the_aggregator_does_it():
    """One feed, many employers, one diff namespace — and `match.py` reads titles only,
    so the employer has to be in the title to be visible at all."""
    post = _parse_one(_msg(content=SAMPLE))
    assert post.title.startswith("Artera — ")
    assert post.company == "Discord: #jobs"


def test_a_dedupe_key_is_derivable_from_the_link_the_bot_posted():
    from jobtracker import dedupe

    post = _parse_one(_msg(content=SAMPLE))
    assert dedupe.dedupe_key(post.url) == (
        "lever:artera-2:eae88c70-fbf5-4525-890c-d3f9377418b0"
    )


def test_a_garbage_payload_parses_to_nothing_rather_than_raising():
    for junk in [None, {}, "a string", [None], [{"no": "id"}]]:
        assert dmod.Discord().parse_page("G", junk, SETTINGS, "2026-08-31")[0] == []
