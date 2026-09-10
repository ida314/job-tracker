"""Suite-wide fixtures.

**The prefill/browser half is switched off in production and on for this suite.**
`config.PREFILL_ENABLED` defaults to False since 2026-09-10 — applications are typed by
hand — and the feature is mothballed rather than deleted, so the tests that describe it
have to keep running against it or it rots into something nobody can turn back on.

That is the `sdk_installed` rule: force the world you mean rather than reading whichever
one the environment happens to be in. The switched-*off* world is not left to this
default either — it has its own tests, in `test_prefill_switch.py`, which set the flag
the other way and assert what each surface says.
"""

import pytest

from jobtracker import config


@pytest.fixture(autouse=True)
def prefill_switched_on(monkeypatch):
    """Every test runs with the prefill half enabled unless it says otherwise.

    `monkeypatch.setattr` rather than the environment variable, because
    `PREFILL_ENABLED` is read once at import; `config.prefill_off()` reads the attribute
    at the point of use, which is what makes patching it enough.
    """
    monkeypatch.setattr(config, "PREFILL_ENABLED", True)
