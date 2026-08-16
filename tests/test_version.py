"""The build stamp.

This exists for one operational question: is the host running the commit CI last
published? A nightly batch job gives you almost nothing to answer it with — a stale
image runs for the same 32 seconds, prints the same report, and exits 0. So the
revision has to reach the running process, and it has to be absent rather than wrong
when there is no build to name.
"""

import jobtracker
from jobtracker import build_version, telemetry


def test_bare_version_outside_a_build(monkeypatch):
    # A working-tree run has no single commit to claim. Reporting the plain package
    # version is the honest answer; inventing one would make the stamp untrustworthy
    # exactly where it is used to prove a deploy landed.
    monkeypatch.delenv("JOBTRACKER_REVISION", raising=False)
    assert build_version() == jobtracker.__version__
    assert "+" not in build_version()


def test_revision_is_appended_and_truncated(monkeypatch):
    monkeypatch.setenv("JOBTRACKER_REVISION", "0123456789abcdef0123456789abcdef01234567")
    assert build_version() == f"{jobtracker.__version__}+0123456789ab"


def test_empty_revision_does_not_produce_a_dangling_plus(monkeypatch):
    # `ARG GIT_SHA=""` with nothing passed renders the ENV as an empty string rather
    # than leaving it unset, so "unset" is not the only absent case to handle. A bare
    # trailing "+" would read as a truncated sha rather than as no sha at all.
    for blank in ("", "   ", "\n"):
        monkeypatch.setenv("JOBTRACKER_REVISION", blank)
        assert build_version() == jobtracker.__version__


def test_telemetry_reports_the_build_not_just_the_package_version(monkeypatch):
    # service.version is the half of this that a dashboard can group by. If it drops
    # back to the package version, every build since 0.1.0 looks identical in Grafana
    # and the resource attribute stops answering the question it exists for.
    monkeypatch.setenv("JOBTRACKER_REVISION", "abc123def456789")
    assert telemetry._version() == build_version()
    assert telemetry._version().endswith("+abc123def456")


# -- the version has to be one number, and it has to be on the page -------------------
def test_version_matches_pyproject():
    """Two places that can disagree eventually do, and this string's only value is that
    you can trust it when comparing what two machines are running."""
    import pathlib
    import tomllib

    root = pathlib.Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text())
    assert data["project"]["version"] == jobtracker.__version__


def test_version_chip_shows_the_build_when_there_is_one(monkeypatch):
    from jobtracker import dashboard

    monkeypatch.setenv("JOBTRACKER_REVISION", "7b41744fd94715e91d6dbe2622d9fe61428b9b94")
    chip = dashboard.version_chip()
    assert f"v{jobtracker.__version__}" in chip
    assert "7b41744fd947" in chip
    assert "working tree" not in chip


def test_version_chip_never_invents_a_sha(monkeypatch):
    """The chip exists to prove which build you are looking at. A guessed revision would
    make it worthless exactly where it is used — and on gx10 the working-tree case is a
    real, expected substrate (`serve` runs the venv, because the image ships no browser),
    not a degraded one. It says so in words rather than in hex."""
    from jobtracker import dashboard

    monkeypatch.delenv("JOBTRACKER_REVISION", raising=False)
    chip = dashboard.version_chip()
    assert f"v{jobtracker.__version__}" in chip
    assert "working tree" in chip
    # Nothing in it may look like a commit.
    import re
    assert not re.search(r"\b[0-9a-f]{7,}\b", chip)


def test_every_surface_carries_the_chip(tmp_path):
    """One place on every page, so two tabs can be compared without hunting."""
    from jobtracker import config, dashboard, server, store
    from jobtracker.criteria import load_criteria

    conn = store.connect(":memory:")
    assert 'class="ver"' in dashboard.build_dashboard(conn, [], "2026-08-16")
    assert 'class="ver"' in server.render_applications(conn, [], "2026-08-16")
    assert 'class="ver"' in server.render_tuning(conn, load_criteria(config.CRITERIA_YAML))
    assert 'class="ver"' in server.render_settings(conn, tmp_path / "answers.yaml")
    conn.close()
