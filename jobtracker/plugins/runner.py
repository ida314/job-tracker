"""Drive one plugin's incremental read. The only module here that opens a socket.

Same split as `tasks/runner.py`: the plugin owns URLs, parsing and cursor arithmetic, and
this owns the loop, the page cap and the `Fetcher`. Everything below is a rule about how
a *cursor* behaves, and each one exists because getting it wrong loses messages silently.
"""

from __future__ import annotations

import logging

from .base import Plugin, PluginFetch

log = logging.getLogger("jobtracker.plugins")

# 20 pages x 100 messages. A backstop, not a filter: hitting it is a fact about the run
# and is logged as one, the same way `fetch.MAX_PAGES` is.
MAX_PAGES = 20


def _fetch_page(plugin: Plugin, fetcher, url: str):
    """One page, in whatever format this board publishes. `(status, payload, error)`.

    The only place the two fetches are chosen between, so a plugin never holds a socket
    and the runner never learns a payload's shape.
    """
    if plugin.page_format == "text":
        return fetcher.fetch_text(url, plugin.auth_headers())
    return fetcher.fetch_json(url, plugin.auth_headers())


def _nothing(result: PluginFetch) -> PluginFetch:
    """Drop what a failed snapshot read had collected so far.

    The cursor walk keeps its partial pages — each one is a complete statement about its
    own window, and the cursor simply does not advance past the failure. A snapshot has
    no windows: half a listing is not a smaller listing, it is the same shape a board
    that emptied would present. `_run_plugins` already refuses to import anything from an
    unhealthy read, and this makes the object itself unable to say otherwise.
    """
    result.postings = []
    result.closed_ids = []
    return result


def _collect_snapshot(
    plugin: Plugin, fetcher, settings: dict, urls: list, today: str
) -> PluginFetch:
    """Read a board that republishes its whole listing. No cursor, fixed pages.

    The counterpart to the cursor walk below, and the differences are all consequences of
    one fact: there is no window to lose here, so nothing has to be remembered between
    runs. What does carry over from that loop is the refusal to guess — a failed page or
    a shape we do not understand ends the read with an error rather than being folded in
    as "fewer jobs today", because a snapshot that came back short is exactly how a board
    would appear to have emptied.

    Ids are de-duplicated across pages: a snapshot board's pages overlap by construction
    (YC's role and location listings share jobs), and importing one twice would be two
    rows for one req inside a single board.
    """
    group = plugin.company(settings).name
    result = PluginFetch(plugin=plugin.name, snapshot=True)
    seen: set = set()

    for url in urls:
        status, payload, error = _fetch_page(plugin, fetcher, url)
        if error:
            result.error = f"{error}" if status is None else f"HTTP {status}: {error}"
            return _nothing(result)
        shape = plugin.page_error(payload)
        if shape:
            result.error = shape
            return _nothing(result)

        postings, unparsed, skipped = plugin.parse_page(group, payload, settings, today)
        for posting in postings:
            if posting.ats_job_id in seen:
                continue
            seen.add(posting.ats_job_id)
            result.postings.append(posting)
        result.unparsed += unparsed
        result.skipped += skipped
        result.read += len(plugin.page_ids(payload))
        result.closed_ids.extend(plugin.closed_ids(payload))

    result.ok = True
    result.imported = len(result.postings)
    return result


def collect(
    plugin: Plugin, fetcher, settings: dict, state: dict, today: str
) -> PluginFetch:
    """Read everything the feed has produced since the stored cursor.

    Three rules, and all three are about not losing messages:

      * **A failed read never advances the cursor.** A failure means we do not know what
        arrived; stamping the cursor anyway would skip that window permanently, with no
        error left behind to find it by. DESIGN.md 7.3 applied to a cursor.
      * **A 200 whose shape we do not understand is a failure, not an empty page.** Zero
        new items is the normal answer here, so it is exactly the reading that must not
        be reachable by accident — `plugin.page_error` is what draws that line.
      * **The cursor advances to the last item the poll decided about**, imported or
        deliberately skipped, and never past one it failed on. Both halves matter and
        they pull in opposite directions: a cursor that only moved for imported items
        would stall forever on a channel whose recent traffic is all conversation, while
        one that moved past a failed write would drop that message on the floor.
    """
    urls = plugin.page_urls(settings, today)
    if urls is not None:
        return _collect_snapshot(plugin, fetcher, settings, urls, today)

    group = plugin.company(settings).name
    after = state.get("cursor") or plugin.first_cursor(settings, today)
    first_read = not state.get("cursor")

    result = PluginFetch(plugin=plugin.name, first_read=first_read)
    seen: set = set()
    cursor = after

    for page_no in range(MAX_PAGES):
        status, payload, error = _fetch_page(
            plugin, fetcher, plugin.page_url(settings, cursor)
        )
        if error:
            result.error = f"{error}" if status is None else f"HTTP {status}: {error}"
            return result
        shape = plugin.page_error(payload)
        if shape:
            result.error = shape
            return result

        postings, unparsed, skipped = plugin.parse_page(group, payload, settings, today)
        result.postings.extend(postings)
        result.unparsed += unparsed
        result.skipped += skipped

        ids = plugin.page_ids(payload)
        result.read += len(ids)
        fresh = [i for i in ids if i not in seen]
        seen.update(ids)

        nxt = plugin.page_cursor(payload)
        if nxt:
            cursor = nxt

        # Two stopping rules besides the cap. A short page is the end of the feed. A page
        # that adds no id we do not already hold is the Nvidia lesson from `_fetch_paged`
        # carried over prophylactically: it costs one set and it terminates any vendor
        # that wraps around or stalls instead of ending, neither of which errors.
        if plugin.page_size and len(ids) < plugin.page_size:
            break
        if not fresh:
            log.debug("%s: page %d added no new ids; stopping", plugin.name, page_no + 1)
            break
    else:
        log.warning(
            "%s: stopped at the %d-page cap with more possibly waiting; "
            "it will resume from the stored cursor next run",
            plugin.name, MAX_PAGES,
        )

    result.ok = True
    result.imported = len(result.postings)
    # Persisted even when it did not move. `first_cursor` is derived from `today`, so a
    # channel that stays quiet would otherwise recompute a floor that slides forward one
    # day every night — and any message older than the window would be skipped without
    # ever having been read. Writing it once pins the floor where the first read put it.
    if cursor:
        result.cursor = {"cursor": cursor}
    return result
