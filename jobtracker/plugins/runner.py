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
    group = plugin.company(settings).name
    after = state.get("cursor") or plugin.first_cursor(settings, today)
    first_read = not state.get("cursor")

    result = PluginFetch(plugin=plugin.name, first_read=first_read)
    seen: set = set()
    cursor = after

    for page_no in range(MAX_PAGES):
        status, payload, error = fetcher.fetch_json(
            plugin.page_url(settings, cursor), plugin.auth_headers()
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
