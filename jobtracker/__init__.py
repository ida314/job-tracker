"""Deterministic backend new-grad job-discovery pipeline.

See DESIGN.md for the full architecture. The short version: fetching, parsing,
diffing, and storing are ordinary tested code; the language model is reserved for
genuine ambiguity and repair (both deferred out of v1).
"""

__version__ = "0.1.0"
