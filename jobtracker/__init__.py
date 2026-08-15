"""Deterministic backend new-grad job-discovery pipeline.

See DESIGN.md for the full architecture. The short version: fetching, parsing,
diffing, and storing are ordinary tested code; the language model is confined to three
bounded roles off the main loop — resolving genuine ambiguity (`resolve`), ranking
against a profile (`rank`), and reading a careers page when a board's slug breaks
(`repair`). In all three it may read, never decide.
"""

__version__ = "0.1.0"
