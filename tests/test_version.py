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
