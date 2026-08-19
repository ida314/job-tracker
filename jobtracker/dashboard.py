"""Render state.db as a single self-contained HTML page.

This is the counterpart to report.py. The report answers "what changed last night" and
is meant to be read once, in a terminal or an email. The dashboard answers "what should
I look at right now" and is meant to be kept open: the standing backlog of open matches,
the uncertain queue that needs a human, and the coverage the pipeline does NOT have.

Three constraints shape the implementation:

* **One file, no server, no network.** The output is a plain .html you can open with
  file://, mail to yourself, or keep in a tab. Nothing is fetched at view time, so it
  works offline and cannot break because a CDN moved. That rules out chart libraries;
  the one chart here is CSS.

* **Rows are rendered server-side, JS only hides them.** Filtering and sorting are
  progressive enhancement. With JavaScript off you still get every posting, as a table.
  The alternative — ship JSON and build the DOM — makes the page useless without JS and
  harder to diff.

* **Everything is escaped.** Titles, locations and company names arrive from third-party
  ATS APIs and are attacker-controllable in principle. They go through html.escape()
  without exception; url fields additionally get a scheme check.

The dashboard is a pure read. It never writes to the database — unlike `report`, which
marks manual companies as surfaced. Opening a view of your data should not mutate it.
"""

from __future__ import annotations

import html
import sqlite3
from collections import Counter
from datetime import date
from typing import Optional

from . import applications as apps_mod, rank as rank_mod, store
from . import build_version
from .criteria import Criteria
from .match import location_label, location_rank
from .models import Company

MANUAL_INTERVAL_DAYS = 7

# Palette. Light and dark are separately chosen steps of the same hues, not a filter
# flip. Tier chips use an ordinal blue ramp — tier 1 most prominent — and always carry
# the tier number as text, so the color is reinforcement and never the only signal.
#
# Tiers are colored in THREE BANDS, not seven steps. Seven steps do not fit: the blue
# ramp's usable range (no lighter than step 250 on light, no darker than 600 on dark)
# holds ten steps, and seven of them leaves adjacent pairs closer than the ~0.06 OKLab
# lightness gap the eye needs — verified with the palette validator, which fails that
# ramp on three adjacent pairs. Three bands pass every check in both modes.
#
# The banding is not a cosmetic compromise; it is the grouping CLAUDE.md already uses:
#   anchor   T1-T2  backend scale-ups and infra/devtools — where backend IS the product
#   applied  T3-T5  applied to, not anchored on (Big Tech, enterprise/PE)
#   research T6-T7  research interest, deliberately outside the new-grad pipeline
# The exact tier number is always printed inside the chip, so the band is reinforcement
# and the number carries the precision.
#
# Each band ships a paired ink because the ramp crosses the light/dark text flip at a
# different band in each mode: light runs dark→light, dark runs light→dark.
_CSS = """
:root {
  color-scheme: light;
  --page:        #f9f9f7;
  --surface:     #fcfcfb;
  --ink:         #0b0b0b;
  --ink-2:       #52514e;
  --muted:       #898781;
  --grid:        #e1e0d9;
  --rule:        #c3c2b7;
  --border:      rgba(11,11,11,0.10);
  --good:        #0ca30c;
  --warning:     #fab219;
  --serious:     #ec835a;
  --critical:    #d03b3b;
  --accent:      #2a78d6;
  /* light: dark -> light; ink flips to near-black at the research band */
  --band-anchor:   #0d366b; --band-anchor-ink:   #fcfcfb;
  --band-applied:  #2a78d6; --band-applied-ink:  #fcfcfb;
  --band-research: #86b6ef; --band-research-ink: #0b0b0b;
  --band-none:     #898781; --band-none-ink:     #fcfcfb;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page:    #0d0d0d;
    --surface: #1a1a19;
    --ink:     #ffffff;
    --ink-2:   #c3c2b7;
    --muted:   #898781;
    --grid:    #2c2c2a;
    --rule:    #383835;
    --border:  rgba(255,255,255,0.10);
    --accent:  #3987e5;
    /* dark: light -> dark; ink flips to near-white at the research band */
    --band-anchor:   #cde2fb; --band-anchor-ink:   #0b0b0b;
    --band-applied:  #5598e7; --band-applied-ink:  #0b0b0b;
    --band-research: #184f95; --band-research-ink: #fcfcfb;
    --band-none:     #898781; --band-none-ink:     #0b0b0b;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page:    #0d0d0d;
  --surface: #1a1a19;
  --ink:     #ffffff;
  --ink-2:   #c3c2b7;
  --muted:   #898781;
  --grid:    #2c2c2a;
  --rule:    #383835;
  --border:  rgba(255,255,255,0.10);
  --accent:  #3987e5;
  /* dark: light -> dark; ink flips to near-white at the research band */
  --band-anchor:   #cde2fb; --band-anchor-ink:   #0b0b0b;
  --band-applied:  #5598e7; --band-applied-ink:  #0b0b0b;
  --band-research: #184f95; --band-research-ink: #fcfcfb;
  --band-none:     #898781; --band-none-ink:     #0b0b0b;
}

* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 20px 64px;
  background: var(--page); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 21px; font-weight: 650; margin: 28px 0 2px; letter-spacing: -0.01em; }
h2 { font-size: 15px; font-weight: 650; margin: 34px 0 10px; letter-spacing: -0.005em; }
.sub { color: var(--ink-2); font-size: 13px; margin: 0 0 4px; }
.note { color: var(--muted); font-size: 12.5px; margin: 6px 0 0; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* --- stat tiles ------------------------------------------------------------ */
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
         gap: 10px; margin-top: 18px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
        padding: 13px 15px; }
.tile .k { font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.05em;
           color: var(--muted); }
.tile .v { font-size: 27px; font-weight: 600; margin-top: 3px; line-height: 1.1; }
.tile .n { font-size: 12px; color: var(--ink-2); margin-top: 2px; }

/* --- tier bar chart -------------------------------------------------------- */
.chart { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
         padding: 15px 17px; margin-top: 12px; }
.bar-row { display: grid; grid-template-columns: 74px 1fr 42px; align-items: center;
           gap: 10px; margin: 7px 0; }
.bar-row .lbl { font-size: 12.5px; color: var(--ink-2); }
.bar-track { background: var(--grid); border-radius: 4px; height: 13px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 0 4px 4px 0; min-width: 2px; }
.bar-row .num { font-size: 12.5px; color: var(--ink-2); text-align: right;
                font-variant-numeric: tabular-nums; }

/* --- filters --------------------------------------------------------------- */
.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
           margin: 20px 0 6px; }
.filters input[type=search], .filters select {
  font: inherit; font-size: 13.5px; padding: 6px 10px; border-radius: 8px;
  border: 1px solid var(--rule); background: var(--surface); color: var(--ink);
}
.filters input[type=search] { min-width: 250px; }
.chip { font-size: 12.5px; padding: 5px 10px; border-radius: 999px; cursor: pointer;
        border: 1px solid var(--rule); background: var(--surface); color: var(--ink-2);
        user-select: none; }
.chip[aria-pressed="true"] { background: var(--ink); color: var(--page);
                             border-color: var(--ink); }
.count { font-size: 12.5px; color: var(--muted); margin-left: auto;
         font-variant-numeric: tabular-nums; }

/* --- tables ---------------------------------------------------------------- */
table { width: 100%; border-collapse: collapse; background: var(--surface);
        border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
th { text-align: left; font-size: 11.5px; text-transform: uppercase;
     letter-spacing: 0.05em; color: var(--muted); font-weight: 600;
     padding: 9px 12px; border-bottom: 1px solid var(--rule); white-space: nowrap; }
td { padding: 9px 12px; border-bottom: 1px solid var(--grid); font-size: 13.5px;
     vertical-align: top; }
tr:last-child td { border-bottom: none; }
tbody tr:hover { background: color-mix(in srgb, var(--accent) 7%, transparent); }
td.co { font-weight: 550; white-space: nowrap; }
td.loc, td.why { color: var(--ink-2); font-size: 12.5px; }
td.seen { color: var(--muted); font-size: 12.5px; white-space: nowrap;
          font-variant-numeric: tabular-nums; }
.empty { padding: 18px; color: var(--muted); font-size: 13.5px; text-align: center;
         background: var(--surface); border: 1px solid var(--border);
         border-radius: 10px; }

/* --- chips / badges -------------------------------------------------------- */
.tier { display: inline-block; min-width: 26px; text-align: center; padding: 2px 6px;
        border-radius: 5px; font-size: 11.5px; font-weight: 600; }
.pin { display: inline-block; font-size: 10.5px; font-weight: 700; letter-spacing: 0.04em;
       padding: 1px 5px; border-radius: 4px; vertical-align: 1px; margin-right: 2px;
       background: var(--band-anchor); color: var(--band-anchor-ink); }
.badge { display: inline-flex; align-items: center; gap: 5px; font-size: 12px;
         padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border);
         white-space: nowrap; }
.badge .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }

details { margin-top: 8px; }
summary { cursor: pointer; font-size: 13.5px; color: var(--ink-2); padding: 4px 0; }
.cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 8px 22px; margin-top: 8px; }
.cols div { font-size: 13px; padding: 2px 0; }
footer { margin-top: 40px; padding-top: 14px; border-top: 1px solid var(--grid);
         color: var(--muted); font-size: 12px; }

/* -- tabs ------------------------------------------------------------------------
   Every panel is rendered server-side and present in the document; the script only
   toggles [hidden]. With JS off, `.tabs` is hidden and all panels show stacked, so
   the page degrades to exactly what it was before tabs existed. */
.tabs { display: none; gap: 4px; margin: 22px 0 6px; border-bottom: 1px solid var(--grid); }
.tab { appearance: none; background: none; border: 0; border-bottom: 2px solid transparent;
       color: var(--muted); font: inherit; font-size: 14px; padding: 8px 14px;
       cursor: pointer; margin-bottom: -1px; }
.tab:hover { color: var(--ink-2); }
.tab[aria-selected="true"] { color: var(--ink); border-bottom-color: var(--accent);
                             font-weight: 600; }
.tab .n { color: var(--muted); font-weight: 400; font-size: 12px; margin-left: 5px; }

/* -- today's picks ---------------------------------------------------------------- */
.picks { display: grid; gap: 14px; margin: 6px 0 26px; }
.pick { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
        padding: 16px 18px; display: grid; grid-template-columns: 34px 1fr; gap: 0 14px; }
.pick .rank { font-size: 26px; font-weight: 700; color: var(--rule); line-height: 1;
              grid-column: 1; grid-row: 1; }
/* Every content block is pinned to column 2. A row span on .rank would have to be kept
   in sync with the number of blocks, and .why and .prefill are both optional — undercount
   and auto-placement drops the button row into the 34px rank column, where it stacks
   vertically instead of running left to right. */
.pick > :not(.rank) { grid-column: 2; }
.pick h3 { margin: 0 0 3px; font-size: 17px; line-height: 1.3; }
.pick h3 a { color: var(--ink); text-decoration: none; }
.pick h3 a:hover { text-decoration: underline; }
.pick .meta { color: var(--muted); font-size: 12.5px; margin-bottom: 8px;
              display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.pick .why { font-size: 13.5px; color: var(--ink-2); line-height: 1.5;
             border-left: 2px solid var(--grid); padding-left: 11px; margin-bottom: 10px; }
.pick .terms { font-size: 11.5px; color: var(--muted); font-family: ui-monospace,
               SFMono-Regular, Menlo, monospace; margin-bottom: 10px; }
.pick .prefill { font-size: 12px; color: var(--ink-2); margin-bottom: 10px;
                 font-variant-numeric: tabular-nums; }
.pick .prefill.none { color: var(--muted); }
.pick .prefill.full { color: var(--good); }
.pick .prefill .need { color: var(--warning); }
.pick .act { display: flex; flex-wrap: wrap; gap: 7px; align-items: stretch; }
/* The anchor and the buttons share a box so the row lines up: same border box, same
   line-height. Without the transparent border the anchor sits 2px shorter than its
   neighbours. */
.pick .act a.apply, .pick .act button { display: inline-flex; align-items: center;
                     border: 1px solid transparent; border-radius: 6px; padding: 6px 11px;
                     line-height: 1.4; white-space: nowrap; }
.pick .act a.apply { background: var(--accent); color: #fcfcfb; text-decoration: none;
                     padding-inline: 14px; font-size: 13px; font-weight: 600; }
/* Reads as one of the buttons, not as a second Apply — it goes to a screen, not a job. */
.pick .act a.viewwin { color: var(--ink-2); border-color: var(--rule); font-size: 12.5px;
                       text-decoration: none; }
.pick .act a.viewwin:hover { color: var(--ink); border-color: var(--ink-2); }
.pick .act button { appearance: none; background: var(--page); color: var(--ink-2);
                    border-color: var(--rule); font: inherit;
                    font-size: 12.5px; line-height: 1.4; cursor: pointer; }
.pick .act button:hover { color: var(--ink); border-color: var(--ink-2); }
.pick .act button[disabled] { opacity: .5; cursor: default; }
/* Whatever came back from /api/apply-to. Written by JS into an empty server-rendered
   node, so a page with JS off never shows a stray empty line. */
.pick .applymsg { font-size: 12px; color: var(--serious); margin-top: 8px; }
.pick .applymsg:empty { display: none; }
.pick .score { font-variant-numeric: tabular-nums; }
.gap { color: var(--serious); font-size: 12.5px; margin: -14px 0 24px; }

/* Which resume this posting attaches. The name renders in both modes; only the controls
   are gated on `serve`, because only a live server has anywhere to POST them. */
.pick .resume { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
                margin-top: 6px; font-size: 12px; color: var(--muted); }
.pick .resume .rname.own { color: var(--ink-2); }
.pick .resume input[type=file] { font: inherit; font-size: 11.5px; max-width: 210px; }
.pick .resume button { appearance: none; background: var(--page); color: var(--ink-2);
                       border: 1px solid var(--rule); border-radius: 6px;
                       padding: 3px 9px; font: inherit; font-size: 11.5px;
                       cursor: pointer; }
.pick .resume button:hover { color: var(--ink); border-color: var(--ink-2); }
.pick .resume button[disabled] { opacity: .5; cursor: default; }

/* The rest of the ranking. A <details>, so it opens with no script at all — and not a
   table, so the filter JS can never reach into the Today tab. */
.rest { margin: 0 0 26px; }
.rest > summary { cursor: pointer; font-size: 13px; color: var(--ink-2);
                  padding: 8px 0; }
.rest > summary:hover { color: var(--ink); }
.restco { margin: 10px 0 4px; }
.restco h3 { display: flex; align-items: center; gap: 8px; font-size: 13px;
             margin: 14px 0 6px; }
.restco h3 .n { color: var(--muted); font-weight: 400; font-size: 12px; }
.restlist { list-style: none; margin: 0; padding: 0; }
.restlist li { display: flex; align-items: baseline; gap: 9px; padding: 4px 0;
               border-top: 1px solid var(--grid); font-size: 13px; }
.restlist .rn { color: var(--muted); font-size: 11.5px; min-width: 22px;
                font-variant-numeric: tabular-nums; }
.restlist .meta { color: var(--muted); font-size: 11.5px; }

/* -- grouped tables ------------------------------------------------------------------
   Rows render visible and JS collapses them on load — the `.tabs` rule applied to
   groups. Filtering owns `hidden`; collapsing owns `.closed`. Two owners of one property
   is how they would drift, so they are two properties that compose. */
tbody[hidden], tr[hidden] { display: none; }
tbody.grp.closed tr[data-search] { display: none; }
tr.cohead th { text-transform: none; letter-spacing: 0; color: var(--ink);
               font-size: 12.5px; font-weight: 600; padding: 7px 12px;
               background: var(--grid); }
tr.cohead .coname { margin-left: 2px; }
tr.cohead .n { color: var(--muted); font-weight: 400; margin-left: 4px;
               font-variant-numeric: tabular-nums; }
/* Hidden until JS confirms it is running, exactly like .tabs. With JS off the company
   name is a caption and every row under it is already on screen, so a button that could
   not collapse anything would be a dead control in a mailed file. */
.cotoggle { display: none; }
.js-groups .cotoggle { display: inline-block; appearance: none; background: none;
                       border: 0; padding: 0 0 0 10px; color: var(--accent);
                       font: inherit; font-size: 12.5px; cursor: pointer; }

/* "Your inbox says something happened." Only rendered when there is something. */
.banner.mail { background: var(--surface); border: 1px solid var(--border);
               border-left: 3px solid var(--accent); border-radius: 8px;
               padding: 9px 13px; font-size: 13px; margin: 10px 0 14px; }

/* -- applications ------------------------------------------------------------------ */
/* Lives here rather than in server.py because both surfaces render it: the read-only
   tab in the static file and the editable page under `serve`, which concatenates this
   stylesheet. One definition means the two cannot drift apart visually. */
.apps { display: grid; gap: 10px; margin: 6px 0 26px; }
.app { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
       padding: 13px 16px; }
/* The left rule is the section's urgency, carried onto every row in it. */
.app.urgent { border-left: 3px solid var(--serious); }
.app.stale  { border-left: 3px solid var(--warning); }
.app.done   { opacity: .78; }
.app h3 { margin: 0 0 3px; font-size: 15.5px; line-height: 1.35; font-weight: 550; }
.app h3 a { color: var(--ink); text-decoration: none; }
.app h3 a:hover { text-decoration: underline; }
.app .co { color: var(--ink-2); font-weight: 400; }
.app .meta { color: var(--muted); font-size: 12.5px; display: flex; flex-wrap: wrap;
             gap: 8px; align-items: center; font-variant-numeric: tabular-nums; }
.app .note { font-size: 13px; color: var(--ink-2); margin-top: 7px;
             border-left: 2px solid var(--grid); padding-left: 10px; }
/* Status pills. One hue per stage of the funnel, and the word is always present — the
   color is reinforcement, never the encoding, same rule as the tier chips. */
.st { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 11.5px;
      font-weight: 600; letter-spacing: .01em; white-space: nowrap;
      border: 1px solid transparent; }
.st-applied   { background: var(--grid);    color: var(--ink-2); }
.st-oa        { background: var(--band-research); color: var(--band-research-ink); }
.st-screen    { background: var(--band-applied);  color: var(--band-applied-ink); }
.st-interview { background: var(--band-anchor);   color: var(--band-anchor-ink); }
.st-offer     { background: var(--good);    color: var(--page); }
.st-rejected  { background: transparent;    color: var(--muted); border-color: var(--rule); }
.st-withdrawn { background: transparent;    color: var(--muted); border-color: var(--rule); }
.app .due        { color: var(--ink-2); }
.app .due.overdue{ color: var(--critical); font-weight: 600; }
.app .due.today  { color: var(--serious); font-weight: 600; }
.app .due.soon   { color: var(--warning); }
.app .quiet { color: var(--warning); }
.app .src { font-size: 10.5px; text-transform: uppercase; letter-spacing: .05em;
            color: var(--muted); border: 1px solid var(--rule); border-radius: 4px;
            padding: 0 4px; }
/* The event log. Two columns so the dates line up into a readable timeline. */
.tl { margin: 8px 0 0; display: grid; grid-template-columns: max-content max-content 1fr;
      gap: 2px 12px; font-size: 12.5px; }
.tl .d { color: var(--muted); font-variant-numeric: tabular-nums; }
.tl .s { color: var(--ink-2); }
.tl .n { color: var(--muted); }
h2 .sub { font-size: 12.5px; font-weight: 400; color: var(--muted); margin-left: 8px; }
/* Also defined in server._EXTRA_CSS, which is emitted after this sheet and therefore
   wins on /tuning, /settings and /applications. This rule is what styles the class on
   the dashboard page, which loads _CSS alone. */
.note { font-size: 12.5px; color: var(--ink-2); }
.note a { color: var(--accent); }

/* -- the build stamp ---------------------------------------------------------------- */
/* Small, monospaced, and always in the same place on every surface, so two tabs can be
   compared without hunting. Never the loudest thing in the header — it answers a
   question you only ask occasionally. */
.ver { font-size: 11.5px; font-weight: 500; color: var(--muted); vertical-align: 3px;
       white-space: nowrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.ver .rev { color: var(--ink-2); }
/* A working-tree render is a different kind of thing from a published build, not a
   lesser one — but it must be impossible to mistake for a sha at a glance. */
.ver .rev.tree { font-style: italic; font-family: inherit; }
"""

_JS = """
// Tabs. Panels are server-rendered and all present; this only toggles [hidden].
// Runs first and independently of the filter block below, so a page with no filter
// bar (no criteria passed) still tabs correctly.
(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab'));
  if (!tabs.length) return;
  var bar = document.querySelector('.tabs');
  if (bar) bar.style.display = 'flex';   // only shown once JS is confirmed running

  function show(name) {
    tabs.forEach(function (t) {
      t.setAttribute('aria-selected', t.dataset.panel === name ? 'true' : 'false');
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-panel-body]'), function (p) {
      p.hidden = p.dataset.panelBody !== name;
    });
  }
  tabs.forEach(function (t) {
    t.addEventListener('click', function () { show(t.dataset.panel); });
  });
  show(tabs[0].dataset.panel);
})();

// Disposition buttons. Only present when rendered by `serve` — the static file has
// none, because there is nothing there for them to POST to.
(function () {
  var buttons = Array.prototype.slice.call(document.querySelectorAll('.pick [data-act]'));
  if (!buttons.length) return;

  buttons.forEach(function (b) {
    b.addEventListener('click', function () {
      var card = b.closest('.pick');
      var row = card.querySelectorAll('[data-act]');
      Array.prototype.forEach.call(row, function (x) { x.disabled = true; });
      fetch('/api/disposition', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          company: b.dataset.company,
          ats_job_id: b.dataset.job,
          action: b.dataset.act,
          days: 7
        })
      }).then(function (r) { return r.json(); }).then(function (res) {
        if (!res.ok) {
          Array.prototype.forEach.call(row, function (x) { x.disabled = false; });
          b.textContent = res.error || 'failed';
          return;
        }
        // Reload rather than splice the next pick in by hand: the server has already
        // recomputed the queue, and re-rendering from it is the one version of the
        // truth. Cheap on a local page, and it cannot drift from the database.
        location.reload();
      }).catch(function () {
        Array.prototype.forEach.call(row, function (x) { x.disabled = false; });
        b.textContent = 'failed';
      });
    });
  });
})();

// "Open prefilled". Same rule as the disposition buttons — present only under `serve`.
// The handler belongs in this file, beside the markup that emits the button: it used to
// live in the tuning page's script, which the dashboard never loads, so every click did
// nothing whatsoever. A button whose handler is on another page is indistinguishable
// from a broken browser.
(function () {
  var buttons = Array.prototype.slice.call(
    document.querySelectorAll('.pick button.apply-to'));
  if (!buttons.length) return;

  buttons.forEach(function (b) {
    var label = b.textContent;
    var card = b.closest('.pick');
    var note = card.querySelector('.applymsg');

    b.addEventListener('click', function () {
      b.disabled = true;
      b.textContent = 'Opening…';
      if (note) note.textContent = '';
      fetch('/api/apply-to', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({company: b.dataset.company, ats_job_id: b.dataset.job})
      }).then(function (r) { return r.json(); }).then(function (res) {
        if (res.ok) {
          // Straight to the page you fill it in on. Same tab, and a navigation rather
          // than a window.open(): after an await the user gesture is gone and a popup
          // blocker eats it, which would read as another dead button.
          //
          // The window itself is still opening on the server as this navigates. That is
          // fine — /apply renders the session immediately and reports its phase, which
          // is the progress report this button never had.
          b.textContent = 'Opening…';
          location.href = res.href || '/apply';
          return;
        }
        fail(res.error || 'failed');
      }).catch(function () { fail('the server did not answer'); });
    });

    // Every reason this can fail is something only you can fix — no answer bank, no
    // browser installed, a window already open. Saying so on the card beats a button
    // that sits on "Opening…" over a browser that never opens.
    function fail(message) {
      b.disabled = false;
      b.textContent = label;
      if (note) note.textContent = message;
    }
  });
})();

// "Rebuild prefill", and this posting's own resume. Handled here because they are
// rendered here — the same rule the block above records the hard way. The base64 read is
// duplicated from server._JS deliberately: sharing it would put this handler in a script
// only the settings, tuning and applications pages emit, and a handler on a page that is
// never loaded is exactly the bug that rule exists to prevent.
(function () {
  var buttons = Array.prototype.slice.call(document.querySelectorAll(
    '.pick button.pick-rebuild, .pick button.pick-attach, .pick button.pick-detach'));
  if (!buttons.length) return;

  // Text into nodes the server rendered. Never markup: the escaping lives in Python.
  function paint(card, res) {
    var line = card.querySelector('.prefill');
    if (line && typeof res.fields === 'number') {
      var counts = line.querySelector('.counts');
      var tail = line.querySelector('.tail');
      if (counts) counts.textContent =
        'prefill ' + (res.fields - res.gaps) + '/' + res.fields + ' fields';
      if (tail) {
        tail.textContent = res.gaps ? ' · ' + res.gaps + ' need you'
                                    : ' · nothing left to type';
        tail.classList.toggle('need', res.gaps > 0);
      }
      line.className = res.gaps ? 'prefill' : 'prefill full';
    }
    var name = card.querySelector('.resume .rname');
    if (name && res.filename) {
      name.textContent = 'resume for this posting: ' + res.filename;
      name.classList.add('own');
    }
  }

  function post(url, body, b, card, label) {
    var note = card.querySelector('.applymsg');
    if (note) note.textContent = '';
    b.disabled = true;
    b.textContent = 'Working…';
    fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); }).then(function (res) {
      b.disabled = false;
      b.textContent = label;
      if (!res.ok) { if (note) note.textContent = res.error || 'failed'; return; }
      paint(card, res);
      // Attaching or clearing changes which buttons belong on the card, and that is the
      // server's call to make, not this script's.
      if (url.indexOf('posting-resume') !== -1) location.reload();
    }).catch(function () {
      b.disabled = false;
      b.textContent = label;
      if (note) note.textContent = 'the server did not answer';
    });
  }

  buttons.forEach(function (b) {
    var card = b.closest('.pick');
    var label = b.textContent;
    var ident = {company: b.dataset.company, ats_job_id: b.dataset.job};

    b.addEventListener('click', function () {
      if (b.classList.contains('pick-rebuild')) {
        post('/api/prefill', ident, b, card, label);
        return;
      }
      if (b.classList.contains('pick-detach')) {
        post('/api/posting-resume/clear', ident, b, card, label);
        return;
      }
      // The file input is scoped to this card, not looked up by id: there are three
      // cards on the page and an id would be three elements sharing one name.
      var input = card.querySelector('input.pickfile');
      var file = input && input.files && input.files[0];
      var note = card.querySelector('.applymsg');
      if (!file) { if (note) note.textContent = 'choose a file first'; return; }
      var reader = new FileReader();
      reader.onload = function () {
        post('/api/posting-resume', {
          company: ident.company, ats_job_id: ident.ats_job_id,
          filename: file.name, content_b64: reader.result.split(',')[1]
        }, b, card, label);
      };
      reader.readAsDataURL(file);
    });
  });
})();

// Grouping and filtering in one block, because both decide whether a row is on screen.
// Splitting them would give two owners to `hidden`, which is how they would drift.
(function () {
  var tables = Array.prototype.slice.call(
    document.querySelectorAll('table[data-filterable]'));
  if (!tables.length) return;

  // A data row is one carrying data-search. Group heads do not, which is what keeps them
  // out of "N of M shown" — that number means postings, and it must not move.
  function rowsOf(node) {
    return Array.prototype.slice.call(node.querySelectorAll('tr[data-search]'));
  }

  // Collapse on load; never expand on click. With JS off every row is already rendered
  // visible, so this direction is the only one that degrades to a working page.
  tables.forEach(function (t) {
    t.classList.add('js-groups');
    Array.prototype.forEach.call(t.tBodies, function (b) {
      if (b.classList.contains('grp')) b.dataset.closed = '1';
    });
    t.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.cotoggle') : null;
      if (!btn) return;
      var body = btn.closest('tbody');
      body.dataset.closed = body.dataset.closed === '1' ? '0' : '1';
      apply();
    });
  });

  var q = document.getElementById('q');
  var atsSel = document.getElementById('ats');
  var locSel = document.getElementById('loc');   // absent when no criteria were passed
  var chips = Array.prototype.slice.call(document.querySelectorAll('.chip[data-tier]'));

  function activeTiers() {
    var on = chips.filter(function (c) { return c.getAttribute('aria-pressed') === 'true'; });
    return on.length ? on.map(function (c) { return c.dataset.tier; }) : null;  // null = all
  }

  function apply() {
    var text = q ? (q.value || '').toLowerCase().trim() : '';
    var ats = atsSel ? atsSel.value : '';
    var tiers = activeTiers();
    // "" = anywhere; otherwise a '|'-separated set of rank names, so "US (incl. NYC)"
    // is expressed as "nyc|us" rather than needing its own comparison.
    var locs = locSel && locSel.value ? locSel.value.split('|') : null;
    // Any active filter forces the matching groups open. A collapsed page under a typed
    // search reads as "nothing found", which is the one thing this page may never say
    // while it is holding rows that match.
    var filtering = !!(text || ats || locs || tiers);

    tables.forEach(function (t) {
      var rows = rowsOf(t), shown = 0;
      rows.forEach(function (row) {
        var ok = (!text || row.dataset.search.indexOf(text) !== -1)
              && (!ats || row.dataset.ats === ats)
              && (!locs || locs.indexOf(row.dataset.loc) !== -1)
              && (!tiers || tiers.indexOf(row.dataset.tier) !== -1);
        row.hidden = !ok;
        if (ok) shown++;
      });
      Array.prototype.forEach.call(t.tBodies, function (b) {
        if (!b.classList.contains('grp')) return;
        var mine = rowsOf(b);
        var vis = mine.filter(function (r) { return !r.hidden; }).length;
        b.hidden = vis === 0;                      // an empty group is not a heading
        b.classList.toggle('closed', b.dataset.closed === '1' && !filtering);
        var open = !b.classList.contains('closed');
        var n = b.querySelector('.cohead .n');
        if (n) n.textContent = (vis === mine.length)
          ? mine.length + (mine.length === 1 ? ' role' : ' roles')
          : vis + ' of ' + mine.length;
        var btn = b.querySelector('.cotoggle');
        if (btn) {
          btn.setAttribute('aria-expanded', open ? 'true' : 'false');
          btn.textContent = open ? 'Hide' : 'Show';
        }
      });
      var out = document.getElementById(t.dataset.countTarget);
      if (out) out.textContent = shown + ' of ' + rows.length + ' shown';
      var none = document.getElementById(t.dataset.emptyTarget);
      if (none) none.hidden = shown !== 0;
    });
  }

  chips.forEach(function (c) {
    c.addEventListener('click', function () {
      c.setAttribute('aria-pressed', c.getAttribute('aria-pressed') === 'true' ? 'false' : 'true');
      apply();
    });
  });
  if (q) q.addEventListener('input', apply);
  if (atsSel) atsSel.addEventListener('change', apply);
  if (locSel) locSel.addEventListener('change', apply);
  apply();
})();
"""


def build_dashboard(
    conn: sqlite3.Connection,
    companies: list[Company],
    today: str,
    criteria: Criteria | None = None,
    interactive: bool = False,
    view_url: str = "",
) -> str:
    """Return a complete HTML document. Pure read — never writes to `conn`.

    With `criteria`, postings are ordered by location preference (NYC, then the rest of
    the US, then unknown, then abroad) and the location filter appears. Location never
    removes a row from the page — it only decides reading order.

    `interactive` adds the applied/skip/snooze buttons to Today's picks. It is only
    ever True when rendered by `serve`, because those buttons POST — the file written
    by `jobtracker dashboard` must stay a self-contained, offline, read-only artifact,
    and dead buttons in it would be worse than no buttons.

    `view_url` points at wherever the browser this host opens can be watched — set when
    `serve` runs somewhere with no screen. It is only a link, and only alongside the
    button, so the static file never carries it.
    """
    by_name = {c.name: c for c in companies}
    matches = _by_location(store.open_postings_by_verdict(conn, "match"), criteria)
    uncertain = _by_location(store.open_postings_by_verdict(conn, "uncertain"), criteria)
    counts = store.counts_by_verdict(conn)
    run = store.last_run(conn)
    unhealthy = store.unhealthy_boards(conn)
    proposals = {p["company"]: p for p in store.open_proposals(conn)}

    ranked = store.ranked_matches(conn)
    # One list, split — never two queries. The rest has to come out of the same filtered
    # set the picks did, or a job you applied to this morning reappears below them.
    ranked_open = rank_mod.available(ranked, today)
    picks, rest = ranked_open[:3], ranked_open[3:]
    unranked = sum(1 for r in ranked if r["score"] is None)
    plans = store.plans_by_posting(conn)
    overrides = store.posting_resumes(conn)
    pending_mail = store.pending_mail_count(conn)

    # Read straight off `applications` rather than joining through `postings`, which is
    # what lets a hand-entered job — no board, no verdict, no posting row — show up here
    # at all. Two queries, not N+1: the events arrive keyed by application.
    apps = store.all_applications(conn)
    events_by = store.events_by_application(conn)

    parts: list[str] = []
    parts.append("<!doctype html>")
    parts.append('<html lang="en"><head><meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append(f"<title>Job tracker — {html.escape(today)}</title>")
    parts.append(f"<style>{_CSS}</style>")
    parts.append('</head><body><div class="wrap">')

    _header(parts, today, run, companies, pending_mail, interactive)
    _tabs(parts, picks, apps, matches, uncertain, unhealthy)

    # Today first, and by itself: the point of the page is to shorten the distance
    # between opening it and applying to something.
    parts.append('<section data-panel-body="today">')
    _picks(parts, picks, by_name, unranked, today, interactive, criteria, plans,
           view_url, rest, overrides)
    parts.append("</section>")

    parts.append('<section data-panel-body="applications" hidden>')
    _applications(parts, apps, events_by, today, by_name)
    if interactive:
        # Under `serve` there is somewhere to send you. The static file gets no link,
        # because a file:// page pointing at a server that is not running is worse than
        # no link at all.
        parts.append(
            '<p class="note"><a href="/applications">Add or update an application →</a></p>'
        )
    parts.append("</section>")

    parts.append('<section data-panel-body="all" hidden>')
    _tiles(parts, matches, uncertain, counts, companies, unhealthy, run, criteria)
    _tier_chart(parts, matches, by_name)
    _filters(parts, matches + uncertain, by_name, criteria)
    _table(parts, "Open matches", matches, by_name, "matches", False, criteria)
    _table(parts, "Uncertain — needs a human", uncertain, by_name, "uncertain", True, criteria)
    parts.append("</section>")

    parts.append('<section data-panel-body="boards" hidden>')
    _boards(parts, unhealthy, by_name, proposals)
    _manual(parts, companies)
    parts.append("</section>")

    parts.append(
        '<footer>Generated by <code>jobtracker dashboard</code> from state.db. '
        "Static snapshot — re-run to refresh. This page never writes to the database."
        "</footer>"
    )
    parts.append("</div>")
    parts.append(f"<script>{_JS}</script>")
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


# -- sections ----------------------------------------------------------------------
def _tabs(parts, picks, applications, matches, uncertain, unhealthy) -> None:
    """Four tabs. Hidden until JS confirms it is running — see the CSS note.

    Order is the priority order: what to do now, what you already did and still owe
    something to, then everything, then plumbing. Applications sits second because the
    outer loop is work you have already committed to, which outranks the raw corpus.
    """
    parts.append('<nav class="tabs" role="tablist">')
    for name, label, count in (
        ("today", "Today", len(picks)),
        ("applications", "Applications", len(applications)),
        ("all", "All postings", len(matches) + len(uncertain)),
        ("boards", "Boards", len(unhealthy)),
    ):
        badge = f'<span class="n">{count}</span>' if count else ""
        parts.append(
            f'<button class="tab" role="tab" data-panel="{name}" '
            f'aria-selected="false">{html.escape(label)}{badge}</button>'
        )
    parts.append("</nav>")


def _picks(parts, picks, by_name, unranked, today, interactive, criteria=None,
           plans=None, view_url="", rest=(), overrides=None) -> None:
    """The three to apply to today.

    Deliberately not a `data-filterable` table. The filter JS selects
    `table[data-filterable]`, so a tier or location filter set on the All postings tab
    would otherwise silently empty a curated list the user did not ask to filter.
    """
    parts.append("<h2>Apply to these today</h2>")
    if not picks:
        parts.append(
            '<div class="empty">Nothing queued. Run <code>jobtracker rank</code> '
            "after <code>check</code>, or you have dispositioned everything.</div>"
        )
    else:
        parts.append('<div class="picks">')
        for i, row in enumerate(picks, 1):
            _pick(parts, i, row, by_name, today, interactive, criteria, plans,
                  view_url, overrides)
        parts.append("</div>")

    if unranked:
        # The blind spot, stated. These are open matches the model has not read, and
        # they are excluded from the picks above — a silently short list would be
        # indistinguishable from having found nothing.
        parts.append(
            f'<p class="gap">{unranked} open match(es) are unranked and not shown — '
            "the model has not read them yet. Run <code>jobtracker rank</code>.</p>"
        )

    _rest_of_ranking(parts, rest, by_name, today, criteria, plans)


def _rest_of_ranking(parts, rest, by_name, today, criteria=None, plans=None) -> None:
    """Everything the ranker scored below today's three, grouped by company.

    `<details>` rather than a JS drawer: it is native disclosure, it opens with no script
    at all, and — unlike a table — it cannot be caught by the filter JS, which the today
    panel must never be. Grouped in encounter order over the score-sorted list, so the
    company with the best role comes first and nothing is re-sorted.

    No buttons, in either mode. A rest row is a link and its numbers; the disposition and
    prefill controls stay on the three cards above, which is what keeps
    `.pick [data-act]` meaning exactly "one of today's picks".
    """
    if not rest:
        return
    # The number is carried alongside the row rather than looked up later: `sqlite3.Row`
    # compares by value, so `rest.index(row)` would hand two identical rows the same
    # position, and the ranking is the one thing this drawer is for.
    groups: dict[str, list] = {}
    for n, row in enumerate(rest, 4):
        groups.setdefault(row["company"], []).append((n, row))

    roles = "role" if len(rest) == 1 else "roles"
    firms = "company" if len(groups) == 1 else "companies"
    parts.append('<details class="rest">')
    parts.append(
        f"<summary>The rest of the ranking — {len(rest)} {roles} "
        f"at {len(groups)} {firms}</summary>"
    )
    parts.append(
        '<p class="note">Scored below today\'s three. A list, not a queue — the buttons '
        "live on a pick.</p>"
    )
    for company, rows in groups.items():
        tier = _tier_of(company, by_name)
        var = _band_var(tier)
        parts.append('<div class="restco">')
        parts.append(
            f'<h3><span class="tier" style="background:var({var});'
            f'color:var({var}-ink)">T{tier if tier is not None else "?"}</span>'
            f'{html.escape(company)} <span class="n">{len(rows)} '
            f'{"role" if len(rows) == 1 else "roles"}</span></h3>'
        )
        parts.append('<ul class="restlist">')
        for n, row in rows:
            loc = row["location"] or "location unspecified"
            is_nyc = criteria is not None and location_rank(row["location"], criteria) == 0
            pin = '<span class="pin">NYC</span> ' if is_nyc else ""
            bits = [f'{pin}{html.escape(loc)}', f'score {row["score"]:.1f}']
            plan = (plans or {}).get((row["company"], row["ats_job_id"]))
            if plan is not None and plan["fields"]:
                bits.append(f'prefill {plan["fields"] - plan["gaps"]}/{plan["fields"]} fields')
            parts.append(
                f'<li><span class="rn">{n}</span>'
                f'<a href="{_safe_url(row["url"])}" target="_blank" rel="noopener">'
                f'{html.escape(row["title"])}</a>'
                f'<span class="meta">{" · ".join(bits)}</span></li>'
            )
        parts.append("</ul></div>")
    parts.append("</details>")


def _pick(parts, i, row, by_name, today, interactive, criteria=None, plans=None,
          view_url="", overrides=None) -> None:
    tier = _tier_of(row["company"], by_name)
    var = _band_var(tier)
    days = rank_mod.days_since(row["posted_on"], today)
    age = f"posted {days}d ago" if days is not None else "posted date unknown"
    loc = row["location"] or "location unspecified"
    # Same rule the tables use — one definition of "NYC", from criteria.yaml.
    is_nyc = criteria is not None and location_rank(row["location"], criteria) == 0
    pin = '<span class="pin">NYC</span> ' if is_nyc else ""

    parts.append('<article class="pick">')
    parts.append(f'<div class="rank">{i}</div>')
    parts.append(
        f'<h3><a href="{_safe_url(row["url"])}" target="_blank" rel="noopener">'
        f'{html.escape(row["title"])}</a></h3>'
    )
    parts.append(
        f'<div class="meta">{pin}<span class="tier" style="background:var({var});'
        f'color:var({var}-ink)">T{tier if tier is not None else "?"}</span>'
        f'<span>{html.escape(row["company"])}</span><span>·</span>'
        f'<span>{html.escape(loc)}</span><span>·</span><span>{html.escape(age)}</span>'
        f'<span>·</span><span class="score">score {row["score"]:.1f}</span></div>'
    )
    if row["why"]:
        parts.append(f'<div class="why">{html.escape(row["why"])}</div>')
    parts.append(
        f'<div class="terms">fit {html.escape(row["backend_fit"])} · '
        f'growth {html.escape(row["growth"])} · '
        f'entry risk {html.escape(row["entry_risk"])}</div>'
    )

    _prefill_line(parts, row, plans)
    _resume_line(parts, row, interactive, overrides)

    parts.append('<div class="act">')
    parts.append(
        f'<a class="apply" href="{_safe_url(row["url"])}" target="_blank" '
        f'rel="noopener">Apply</a>'
    )
    if interactive:
        c = html.escape(row["company"], quote=True)
        j = html.escape(row["ats_job_id"], quote=True)
        # Only under `serve`. The static file must stay offline and read-only, and a
        # button that cannot drive a browser is worse than no button — the same rule
        # the disposition buttons follow.
        parts.append(
            f'<button class="apply-to" data-company="{c}" data-job="{j}">'
            "Open prefilled</button>"
        )
        # Re-plan this one posting against the answers and resume as they stand now.
        # Rules only, no model and no network — see `server._rebuild_plan`.
        parts.append(
            f'<button class="pick-rebuild" data-company="{c}" data-job="{j}">'
            "Rebuild prefill</button>"
        )
        if view_url:
            # `serve` is running where you cannot see its screen, so the window it opens
            # needs somewhere to be watched. Deliberately a plain link to a viewer this
            # app does not run, start, or check: a dead link is legible, whereas a
            # dashboard that thinks it manages a remote desktop is a second thing to
            # debug at 2am. _safe_url keeps a javascript: scheme out of the href.
            parts.append(
                f'<a class="viewwin" href="{_safe_url(view_url)}" target="_blank" '
                'rel="noopener">View window</a>'
            )
        for action, label in (
            ("applied", "I applied"), ("skipped", "Skip"), ("snoozed", "Snooze 7d"),
        ):
            parts.append(
                f'<button data-act="{action}" data-company="{c}" data-job="{j}">'
                f"{label}</button>"
            )
    parts.append("</div>")
    if interactive:
        # Where the apply-to handler writes what came back. Empty and invisible until
        # something has to be said; rendered here rather than created in JS so the
        # script only ever sets text, never markup.
        parts.append('<div class="applymsg"></div>')
    parts.append("</article>")


def _prefill_line(parts, row, plans) -> None:
    """How much of this application we can fill in, and what still needs the user.

    Rendered in the static file too, without the button. The counts are the useful part
    even offline: they say whether opening this one will take thirty seconds or ten
    minutes, which is exactly the question the Today tab exists to answer.
    """
    plan = (plans or {}).get((row["company"], row["ats_job_id"]))
    if plan is None:
        parts.append(
            '<div class="prefill none">no prefill yet — '
            "<code>jobtracker work --task prefill</code></div>"
        )
        return
    filled = plan["fields"] - plan["gaps"]
    cls = "prefill" if plan["gaps"] else "prefill full"
    # Two spans, so the Rebuild handler only ever sets *text* into nodes that were
    # rendered here. A script that writes markup into a card is a script that has to
    # re-implement the escaping this file already does.
    tail = (
        f'<span class="tail need"> · {plan["gaps"]} need you</span>'
        if plan["gaps"] else '<span class="tail"> · nothing left to type</span>'
    )
    parts.append(
        f'<div class="{cls}"><span class="counts">prefill {filled}/{plan["fields"]}'
        f" fields</span>{tail}</div>"
    )


def _resume_line(parts, row, interactive, overrides=None) -> None:
    """Which resume this posting will attach, and — under `serve` — how to change it.

    The name renders in both modes for the same reason the prefill counts do: knowing
    which PDF is about to go out under your name is useful offline. Only the controls are
    gated on `interactive`, because only a live server has anywhere to POST them.
    """
    row_override = (overrides or {}).get((row["company"], row["ats_job_id"]))
    parts.append('<div class="resume">')
    if row_override is not None:
        parts.append(
            '<span class="rname own">resume for this posting: '
            f'{html.escape(row_override["filename"])}</span>'
        )
    else:
        parts.append('<span class="rname">resume: the one in Settings</span>')
    if interactive:
        c = html.escape(row["company"], quote=True)
        j = html.escape(row["ats_job_id"], quote=True)
        parts.append(
            "<input type=\"file\" class=\"pickfile\" accept=\".pdf,.docx\" "
            'aria-label="Resume for this posting">'
            f'<button class="pick-attach" data-company="{c}" data-job="{j}">'
            "Use for this posting</button>"
        )
        if row_override is not None:
            parts.append(
                f'<button class="pick-detach" data-company="{c}" data-job="{j}">'
                "Use the default</button>"
            )
    parts.append("</div>")


def version_chip() -> str:
    """The build this page was rendered by, as a chip.

    Public rather than underscored because `server.py` puts the same chip on the pages
    it renders itself — the point is that every surface answers the version question the
    same way, so two tabs can be compared at a glance.

    Absence stays meaningful, exactly as `build_version()` intends. An image run stamps
    `JOBTRACKER_REVISION` and reads `v0.2.0 · 7b41744f`; a working-tree run has no single
    commit to name and says so in words. It must never invent a sha here: this chip's
    only job is to prove which build you are looking at, and a guessed one would make it
    worthless precisely when it matters. On gx10 the two are genuinely different
    substrates — the nightly pipeline runs the published image, `serve` runs the repo
    venv because the image ships no browser — so "working tree" is the correct and
    useful answer there, not a degraded one.
    """
    version = build_version()
    if "+" in version:
        base, _, rev = version.partition("+")
        detail = f'<span class="rev">{html.escape(rev)}</span>'
    else:
        base, detail = version, '<span class="rev tree">working tree</span>'
    return f'<span class="ver">v{html.escape(base)} · {detail}</span>'


def _header(parts, today, run, companies, pending_mail=0, interactive=False) -> None:
    parts.append(
        f"<h1>Job tracker — {html.escape(today)} {version_chip()}</h1>"
    )
    _mail_banner(parts, pending_mail, interactive)
    if run is None:
        parts.append('<p class="sub">No run recorded yet. Run <code>jobtracker check</code>.</p>')
        return
    api = sum(1 for c in companies if c.check_method == "api")
    manual = sum(1 for c in companies if c.check_method == "manual")
    parts.append(
        f'<p class="sub">Last run {html.escape(str(run["started_at"]))} — '
        f'{run["ok"]} of {run["companies"]} boards healthy, '
        f'{run["new_postings"]} new postings, {run["matches"]} new matches.</p>'
    )
    parts.append(
        f'<p class="note">Coverage: {api} boards checked automatically, '
        f"{manual} manual-only companies that are never scraped (see below).</p>"
    )


def _mail_banner(parts, pending: int, interactive: bool) -> None:
    """"Your inbox says something happened" — a count, and where to go about it.

    Derived from the pending proposals rather than from a stored "seen" flag. A flag
    would have to be written by a GET, and rendering a page must not mutate what it
    renders; a flag written by JS would never be written at all with JS off. So the
    acknowledgement is accepting or dismissing a proposal, not glancing at the banner —
    which is also the honest behaviour for the one feature meant to catch what you missed.

    Nothing at all when there is nothing pending: a zero-state line here would be a
    permanent fixture that stops being read long before it has something to say.
    """
    if not pending:
        return
    what = "email suggests" if pending == 1 else "emails suggest"
    if interactive:
        parts.append(
            f'<p class="banner mail">{pending} {what} an application moved — '
            '<a href="/applications">review</a>.</p>'
        )
    else:
        # No link in the static file: a file:// page pointing at a server that may not be
        # running is worse than no link. The count still earns its place offline — it
        # says whether opening the app is worth it.
        parts.append(
            f'<p class="banner mail">{pending} {what} an application moved — '
            "<code>jobtracker mail</code>.</p>"
        )


def _by_location(rows, criteria):
    """Stable-sort rows by location preference. Never filters — only reorders.

    The store already returns newest-first, and Python's sort is stable, so within a
    location band the recency order is preserved.
    """
    if criteria is None:
        return list(rows)
    return sorted(rows, key=lambda r: location_rank(r["location"], criteria))


def _tiles(parts, matches, uncertain, counts, companies, unhealthy, run, criteria=None) -> None:
    stale = _days_since(run["started_at"][:10]) if run else None
    # A run older than 2 days is the failure this whole tile exists to catch: every
    # other number on the page keeps looking fine when the pipeline has simply stopped.
    #
    # Clamped at zero because the container stamps started_at in UTC while this renders
    # in the host's local date. West of UTC that makes a run from twenty minutes ago
    # read as -1 days, which displayed as "-1d / yesterday". Anything not in the past
    # is "today".
    if stale is None:
        run_note, run_val = "never run", "—"
    elif stale <= 0:
        run_note, run_val = "today", "0d"
    else:
        run_note, run_val = ("stale" if stale > 1 else "yesterday"), f"{stale}d"

    tiles = [
        ("Open matches", str(len(matches)), "worth an application"),
    ]
    if criteria is not None:
        nyc = sum(1 for r in matches if location_rank(r["location"], criteria) == 0)
        us = sum(1 for r in matches if location_rank(r["location"], criteria) == 1)
        tiles.append(("In NYC", str(nyc), f"+{us} elsewhere in the US"))
    tiles += [
        ("Uncertain", str(len(uncertain)), "needs a human read"),
        ("Rejected", f"{counts.get('reject', 0):,}", "by the rules, auditable"),
        ("Boards flagged", str(len(unhealthy)), "not all are problems"),
        ("Last run", run_val, run_note),
    ]
    parts.append('<div class="tiles">')
    for k, v, n in tiles:
        parts.append(
            f'<div class="tile"><div class="k">{html.escape(k)}</div>'
            f'<div class="v">{html.escape(v)}</div>'
            f'<div class="n">{html.escape(n)}</div></div>'
        )
    parts.append("</div>")


def _tier_chart(parts, matches, by_name) -> None:
    """Open matches by tier — magnitude across an ordered category, so: bars.

    Tier order is meaningful (1 is the anchor, 7 is research-interest), so the bars stay
    in tier order rather than sorting by count. Every bar carries its tier label and its
    count directly, so the three-band color is reinforcement and never the encoding.
    """
    if not matches:
        return
    tally = Counter(_tier_of(r["company"], by_name) for r in matches)
    if not tally:
        return
    top = max(tally.values())
    parts.append("<h2>Open matches by tier</h2>")
    parts.append('<div class="chart">')
    for tier in sorted(tally, key=lambda t: (t == "—", t)):
        n = tally[tier]
        label = f"Tier {tier}" if tier != "—" else "Untiered"
        var = _band_var(tier)
        pct = 100.0 * n / top
        parts.append(
            f'<div class="bar-row"><div class="lbl">{html.escape(label)}</div>'
            f'<div class="bar-track"><div class="bar-fill" '
            f'style="width:{pct:.1f}%;background:var({var})"></div></div>'
            f'<div class="num">{n}</div></div>'
        )
    parts.append("</div>")
    parts.append(
        '<p class="note">Shading groups tiers the way the strategy does: '
        "<strong>T1–T2</strong> the anchor (backend scale-ups, infra/devtools), "
        "<strong>T3–T5</strong> applied to but not anchored on, "
        "<strong>T6–T7</strong> research interest outside the new-grad pipeline.</p>"
    )


def _filters(parts, rows, by_name, criteria=None) -> None:
    tiers = sorted({_tier_of(r["company"], by_name) for r in rows}, key=lambda t: (t == "—", t))
    ats = sorted({by_name[r["company"]].ats for r in rows if r["company"] in by_name})
    parts.append('<div class="filters">')
    parts.append(
        '<input type="search" id="q" placeholder="Filter by title, company, location…" '
        'aria-label="Filter postings">'
    )
    parts.append('<select id="ats" aria-label="Filter by ATS"><option value="">All ATS</option>')
    for a in ats:
        parts.append(f'<option value="{html.escape(a)}">{html.escape(a)}</option>')
    parts.append("</select>")
    # Location is a filter you opt into, not one applied for you — the default is "all",
    # matching the rule that geography never removes anything on its own.
    if criteria is not None:
        parts.append('<select id="loc" aria-label="Filter by location">')
        for value, label in (
            ("", "Anywhere"),
            ("nyc", "NYC only"),
            ("nyc|us", "US (incl. NYC)"),
            ("unknown", "Unspecified"),
            ("non-us", "Outside the US"),
        ):
            parts.append(f'<option value="{value}">{html.escape(label)}</option>')
        parts.append("</select>")
    for t in tiers:
        label = f"T{t}" if t != "—" else "untiered"
        parts.append(
            f'<button type="button" class="chip" data-tier="{html.escape(str(t))}" '
            f'aria-pressed="false">{html.escape(label)}</button>'
        )
    parts.append("</div>")
    note = (
        "No tier selected means all tiers. Filters apply to both tables below; "
        "with JavaScript disabled every row is shown unfiltered."
    )
    if criteria is not None:
        note += (
            " Rows are already ordered NYC first, then the rest of the US — location "
            "changes the order, never the contents."
        )
    parts.append(f'<p class="note">{note}</p>')


def _table(parts, heading, rows, by_name, ident, reason: bool, criteria=None) -> None:
    parts.append(
        f'<h2>{html.escape(heading)} '
        f'<span class="count" id="{ident}-count">{len(rows)}</span></h2>'
    )
    if not rows:
        parts.append('<div class="empty">Nothing here.</div>')
        return
    parts.append(
        f'<table data-filterable data-count-target="{ident}-count" '
        f'data-empty-target="{ident}-empty">'
    )
    # Tier and company moved into the group head, which spans the row. `colspan` and the
    # header come out of the same `reason` flag, in one place, so they cannot disagree.
    cols = 4 if reason else 3
    parts.append("<thead><tr><th>Role</th>"
                 "<th>Location</th>" + ("<th>Why uncertain</th>" if reason else "")
                 + "<th>First seen</th></tr></thead>")

    # Grouped by iterating the already location-sorted rows, so dict insertion order puts
    # each company where its best-placed role already was. NYC-first survives grouping
    # with no second comparator to keep in step with `_by_location`.
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["company"], []).append(r)

    for company_name, group in groups.items():
        company = by_name.get(company_name)
        tier = _tier_of(company_name, by_name)
        var = _band_var(tier)
        ats = company.ats if company else ""
        parts.append('<tbody class="grp">')
        parts.append(
            f'<tr class="cohead"><th colspan="{cols}" scope="colgroup">'
            f'<span class="tier" style="background:var({var});color:var({var}-ink)">'
            f'{html.escape(str(tier))}</span>'
            f'<span class="coname">{html.escape(company_name)}</span>'
            f'<span class="n">{len(group)} '
            f'{"role" if len(group) == 1 else "roles"}</span>'
            '<button type="button" class="cotoggle" aria-expanded="true">Hide</button>'
            "</th></tr>"
        )
        for r in group:
            loc = r["location"] or ""
            rank = location_rank(loc, criteria) if criteria is not None else None
            loc_key = location_label(rank) if rank is not None else ""
            # Company, tier and ats stay in the search blob even though their cells moved
            # into the head above — otherwise typing a company name would stop matching
            # its own rows, which is the first thing anyone tries.
            search = " ".join(
                x.lower() for x in (r["company"], r["title"], loc, str(tier), ats) if x
            )
            parts.append(
                f'<tr data-tier="{html.escape(str(tier))}" data-ats="{html.escape(ats)}" '
                f'data-loc="{html.escape(loc_key)}" '
                f'data-search="{html.escape(search)}">'
            )
            parts.append(f'<td><a href="{_safe_url(r["url"])}" target="_blank" '
                         f'rel="noopener noreferrer">{html.escape(r["title"])}</a></td>')
            # The NYC pin is a marker on the single most-preferred band, not a per-row
            # colored scale — four location colors beside seven tier chips would be noise.
            pin = '<span class="pin">NYC</span> ' if rank == 0 else ""
            parts.append(f'<td class="loc">{pin}{html.escape(loc) or "—"}</td>')
            if reason:
                parts.append(f'<td class="why">{html.escape(r["reason"] or "")}</td>')
            parts.append(
                f'<td class="seen">{html.escape((r["first_seen"] or "")[:10])}</td>'
            )
            parts.append("</tr>")
        parts.append("</tbody>")
    parts.append("</table>")
    parts.append(f'<div class="empty" id="{ident}-empty" hidden>No rows match the filters.</div>')


_STATUS_STYLE = {
    "ok": ("good", "✓", "healthy"),
    "suspect_empty": ("warning", "!", "reachable but empty"),
    "fetch_failed": ("serious", "×", "could not fetch"),
    "identity_drift": ("critical", "⚠", "wrong company's board"),
}


def _applications(parts, apps, events_by, today, by_name) -> None:
    """Everything you applied to, grouped by what needs doing.

    **Read-only, and deliberately without an `interactive` flag.** Every control lives on
    `/applications` under `serve`. Two reasons, both of them existing rules here: the
    static file must stay an offline artifact where a button could not work, and a
    button's handler belongs in the file that renders the button — this module ships no
    application handlers, so it must ship no application buttons.

    Also deliberately not a `table[data-filterable]`. The filter JS selects those, so a
    tier or location filter left set on the All postings tab would silently empty the
    list of things you actually did — the same trap `_picks` documents.
    """
    stats = apps_mod.summary(apps, events_by)
    parts.append('<h2>Applications <span class="count">' f'{stats["total"]}</span></h2>')

    if not apps:
        parts.append(
            '<div class="empty">Nothing recorded yet. Apply to one of today\'s picks, '
            "or add a job by hand from the Applications page under "
            "<code>jobtracker serve</code>.</div>"
        )
        return

    parts.append('<div class="tiles">')
    for k, v, n in (
        ("Total", str(stats["total"]), "applications recorded"),
        ("Active", str(stats["active"]), "still in play"),
        ("Interviewing", str(stats["interviewing"]), "at an interview stage"),
        ("Offers", str(stats["offers"]), "reached an offer"),
        ("Response rate", f'{stats["response_rate"]}%',
         f'{stats["responded"]} ever replied'),
    ):
        parts.append(
            f'<div class="tile"><div class="k">{html.escape(k)}</div>'
            f'<div class="v">{html.escape(v)}</div>'
            f'<div class="n">{html.escape(n)}</div></div>'
        )
    parts.append("</div>")

    groups = apps_mod.group(apps, events_by, today)
    for key, heading, blurb in (
        ("needs_action", "Needs action", "a date has come due, or nobody has moved in "
                                         f"{store.STALE_AFTER_DAYS} days"),
        ("active", "Active", "applied, waiting"),
        ("closed", "Closed", "offer, rejection, or withdrawn"),
    ):
        rows = groups[key]
        if not rows:
            continue
        parts.append(
            f"<h2>{html.escape(heading)} "
            f'<span class="count">{len(rows)}</span>'
            f'<span class="sub">{html.escape(blurb)}</span></h2>'
        )
        parts.append('<div class="apps">')
        for app in rows:
            _application(parts, app, events_by, today, by_name)
        parts.append("</div>")


def _application(parts, app, events_by, today, by_name) -> None:
    events = events_by.get((app["company"], app["ats_job_id"]), [])
    state = apps_mod.action_state(app, today)
    stale = apps_mod.is_stale(app, today)

    cls = "app"
    if state in ("overdue", "today", "soon"):
        cls += " urgent"
    elif stale:
        cls += " stale"
    if apps_mod.is_closed(app):
        cls += " done"
    parts.append(f'<article class="{cls}">')

    title = html.escape(app["title"])
    href = _safe_url(app["url"])
    # A manual entry often has no URL at all, so the title is plain text rather than a
    # dead '#' anchor that looks clickable and is not.
    heading = (
        f'<a href="{href}" target="_blank" rel="noopener">{title}</a>'
        if href != "#" else title
    )
    parts.append(
        f'<h3>{heading} <span class="co">· {html.escape(app["company"])}</span></h3>'
    )

    meta: list[str] = []
    status = app["status"]
    repeats = apps_mod.round_counts(events).get(status, 0)
    times = f" ×{repeats}" if repeats > 1 else ""
    meta.append(
        f'<span class="st st-{html.escape(status, quote=True)}">'
        f"{html.escape(status)}{html.escape(times)}</span>"
    )

    tier = _tier_of(app["company"], by_name)
    if tier != "—":
        var = _band_var(tier)
        meta.append(
            f'<span class="tier" style="background:var({var});color:var({var}-ink)">'
            f"T{html.escape(str(tier))}</span>"
        )
    if app["location"]:
        meta.append(html.escape(app["location"]))

    applied_days = apps_mod.days_since(app["applied_at"], today)
    if applied_days is not None:
        meta.append(f"applied {_ago(applied_days)}")
    moved = apps_mod.days_since(app["updated_at"], today)
    if stale and moved is not None:
        meta.append(f'<span class="quiet">no movement in {moved}d</span>')

    if state:
        meta.append(_due_label(app, today, state))
    if app["source"] == "manual":
        meta.append('<span class="src">manual</span>')
    parts.append(f'<div class="meta">{" · ".join(meta)}</div>')

    if app["note"]:
        parts.append(f'<div class="note">{html.escape(app["note"])}</div>')

    # More than the one creation event is the only case where a timeline says anything
    # the status pill did not already say.
    if len(events) > 1:
        parts.append(
            f"<details><summary>History ({len(events)})</summary>"
            '<div class="tl">'
        )
        for event in events:
            parts.append(
                f'<span class="d">{html.escape(apps_mod.day_of(event["at"]) or "")}</span>'
                f'<span class="s">{html.escape(event["status"])}</span>'
                f'<span class="n">{html.escape(event["note"] or "")}</span>'
            )
        parts.append("</div></details>")
    parts.append("</article>")


def _ago(days: int) -> str:
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days}d ago"


def _due_label(app, today: str, state: str) -> str:
    """The next-action cell: what is owed, and when.

    The note is shown alongside the date because a bare date is not a reminder — "2 days
    overdue" does not tell you what you were supposed to do.
    """
    until = apps_mod.days_until(app["next_action"], today)
    if until is None:
        return ""
    if until < 0:
        when = f"{-until}d overdue"
    elif until == 0:
        when = "due today"
    elif until == 1:
        when = "due tomorrow"
    else:
        when = f"due in {until}d"
    what = app["next_action_note"] or "follow up"
    return (
        f'<span class="due {html.escape(state, quote=True)}">'
        f"{html.escape(what)} — {html.escape(when)}</span>"
    )


def _boards(parts, unhealthy, by_name, proposals=None) -> None:
    parts.append(f"<h2>Flagged boards ({len(unhealthy)})</h2>")
    if not unhealthy:
        parts.append('<div class="empty">All boards healthy.</div>')
        return
    parts.append(
        '<p class="note">A flag is not automatically a bug. <code>suspect_empty</code> '
        "means the slug is right and the board genuinely has zero open reqs — dbt Labs and "
        "Root Insurance sit here permanently, and the tier-7 lakehouse companies are "
        "expected to. Escalation happens only after repeated empties on a board that was "
        "once populated. <code>identity_drift</code> is always serious: the board no longer "
        "belongs to the company we think it does, and its postings were discarded.</p>"
    )
    parts.append("<table><thead><tr><th>Company</th><th>Status</th><th>Board</th>"
                 "<th>Detail</th><th>Proposed fix</th></tr></thead><tbody>")
    proposals = proposals or {}
    for r in unhealthy:
        status = r["last_status"]
        color, icon, word = _STATUS_STYLE.get(status, ("muted", "•", status))
        company = by_name.get(r["company"])
        slug = f"{company.ats}/{company.slug}" if company else "—"
        alert = " · escalated" if r["alerting"] else ""
        parts.append(
            f'<tr><td class="co">{html.escape(r["company"])}</td>'
            f'<td><span class="badge"><span class="dot" '
            f'style="background:var(--{color})"></span>{icon} {html.escape(word)}'
            f"{alert}</span></td>"
            f'<td class="loc"><code>{html.escape(slug)}</code></td>'
            f'<td class="why">{html.escape(r["detail"] or "—")}</td>'
            f'<td class="why">{_proposal_cell(proposals.get(r["company"]))}</td></tr>'
        )
    parts.append("</tbody></table>")


def _proposal_cell(proposal) -> str:
    """A verified repair, shown but never actionable from here.

    Deliberately no apply button, even under `serve`. Applying rewrites companies.yaml,
    which is curated, git-tracked data — that belongs behind a command whose diff you
    read, not behind a click. Every value is escaped: the board name comes from a
    third-party ATS.
    """
    if proposal is None:
        return "—"
    target = html.escape(f"{proposal['to_ats']}/{proposal['to_slug']}")
    board = html.escape(proposal["board_name"] or "no board name")
    weak = (
        ' <span class="badge">⚠ provenance only</span>'
        if proposal["evidence_kind"] == "provenance"
        else ""
    )
    return (
        f"<code>{target}</code>{weak}<br>"
        f'<span class="muted">{proposal["job_count"]} jobs · {board} · '
        f'verified {html.escape(proposal["verified_at"])}</span>'
    )


def _manual(parts, companies) -> None:
    manual = sorted(
        (c for c in companies if c.check_method == "manual"),
        key=lambda c: (c.tier if c.tier is not None else 99, c.name),
    )
    parts.append(f"<h2>Never scraped — check by hand ({len(manual)})</h2>")
    parts.append(
        '<p class="note">These run Workday, Gem, bespoke portals or token-gated boards '
        "with no keyless JSON API. The pipeline deliberately does not touch them, because "
        "surfacing a gap in coverage is honest and reporting zero for an unchecked company "
        "is not.</p>"
    )
    parts.append("<details><summary>Show the list</summary><div class=\"cols\">")
    for c in manual:
        tier = f"T{c.tier}" if c.tier is not None else "—"
        link = (
            f'<a href="{_safe_url(c.careers_page)}" target="_blank" '
            f'rel="noopener noreferrer">{html.escape(c.name)}</a>'
            if c.careers_page
            else html.escape(c.name)
        )
        parts.append(f"<div>{html.escape(tier)} · {link}</div>")
    parts.append("</div></details>")


# -- helpers -----------------------------------------------------------------------
def _tier_of(company_name: str, by_name: dict[str, Company]):
    company = by_name.get(company_name)
    return company.tier if company and company.tier is not None else "—"


def _band_var(tier) -> str:
    """Map a tier to its CSS color band. See the palette comment for why three, not seven.

    Returns the base variable name; the paired ink is always `<name>-ink`.
    """
    if not isinstance(tier, int):
        return "--band-none"
    if tier <= 2:
        return "--band-anchor"
    if tier <= 5:
        return "--band-applied"
    return "--band-research"


def _safe_url(url: Optional[str]) -> str:
    """Escape a URL, and refuse any scheme that is not http(s).

    Posting URLs come from third-party APIs. A `javascript:` URL in an href would run
    when clicked; dropping to '#' is the boring, correct handling.
    """
    if not url:
        return "#"
    lowered = url.strip().lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return "#"
    return html.escape(url.strip(), quote=True)


def _days_since(iso_day: str) -> Optional[int]:
    try:
        return (date.today() - date.fromisoformat(iso_day)).days
    except (ValueError, TypeError):
        return None
