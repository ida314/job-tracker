"""`jobtracker audit-pins`: removing the model pins nothing on the page supports.

A bulk remover of overrides, so the first test is the one that matters: it can only see
`llm` rows. The rest pin down the table in `audit.py` and the write path's promise that
a cleared pin re-derives from the rules and leaves the outer loop alone.
"""

import hashlib

import pytest

from jobtracker import audit, cli, config, store
from jobtracker.criteria import load_criteria

TODAY = "2026-11-06"

NEW_GRAD = "Software Engineer, New Grad"            # rules: match
NO_LEVEL = "Software Engineer, Distributed Systems"  # rules: uncertain
FEED_STUB = "Employer: Acme\nLocation: New York, NY\nPosted: 2026-10-01"
PROSE = ("We are hiring engineers with 0-2 years of experience to build our storage "
         "layer. Recent graduates are welcome to apply.")


@pytest.fixture
def criteria():
    return load_criteria(config.CRITERIA_YAML)


def _posting(conn, jid, title, description=None, source=None, company="Acme"):
    conn.execute(
        "INSERT INTO postings (company, ats_job_id, title, location, url, first_seen, "
        "last_seen, description, description_source) "
        "VALUES (?, ?, ?, '', 'https://example.com/j', ?, ?, ?, ?)",
        (company, jid, title, TODAY, TODAY, description, source),
    )
    conn.execute(
        "INSERT INTO verdicts (company, ats_job_id, verdict, reason, decided_by, decided_at) "
        "VALUES (?, ?, 'uncertain', 'no_level_signal', 'rules', ?)",
        (company, jid, TODAY),
    )


def _pin(conn, jid, decision, reason, decided_by="llm", company="Acme"):
    assert store.set_override(conn, company, jid, decision, TODAY,
                              reason=reason, decided_by=decided_by)
    conn.execute(
        "UPDATE verdicts SET verdict=?, reason=?, decided_by=? "
        "WHERE company=? AND ats_job_id=?",
        (decision, f"override:{reason}", decided_by, company, jid),
    )


def _db(tmp_path, build):
    path = tmp_path / "state.db"
    conn = store.connect(path)
    build(conn)
    conn.commit()
    conn.close()
    return path


def _audit(path, *flags):
    args = cli.build_parser().parse_args(["audit-pins", "--db", str(path), *flags])
    return cli.cmd_audit_pins(args)


def _overrides(path):
    conn = store.connect(path)
    try:
        return {k: (r["decision"], r["decided_by"]) for k, r in store.load_overrides(conn).items()}
    finally:
        conn.close()


def _row(jid, title, decision, reason, description=None, source=None):
    return {"company": "Acme", "ats_job_id": jid, "title": title, "decision": decision,
            "reason": reason, "description": description, "description_source": source,
            "url": "u", "location": ""}


def test_audit_pins_never_touches_a_human_override(tmp_path):
    """A human ruling with an unjustifiable-looking reason is not even a candidate.

    Every row here would be cleared if the audit could see it — a feed row whose title
    the rules cannot match, a `not_entry` with no quote — and each is yours.
    """
    def build(conn):
        _posting(conn, "1", NO_LEVEL, FEED_STUB, "feed")
        _pin(conn, "1", "match", "llm:entry+eng:generic", decided_by="human")
        _posting(conn, "2", NO_LEVEL, PROSE, "ats")
        _pin(conn, "2", "reject", "llm:not_entry:", decided_by="human")
        _posting(conn, "3", NO_LEVEL, FEED_STUB, "feed")
        _pin(conn, "3", "match", "llm:entry+eng:generic")   # the one llm pin

    path = _db(tmp_path, build)
    conn = store.connect(path)
    assert [r["ats_job_id"] for r in store.llm_pins(conn)] == ["3"]
    conn.close()

    assert _audit(path, "--write") == 0
    assert _overrides(path) == {("Acme", "1"): ("match", "human"),
                                ("Acme", "2"): ("reject", "human")}


def test_audit_is_dry_by_default(tmp_path, capsys):
    def build(conn):
        _posting(conn, "1", NO_LEVEL, FEED_STUB, "feed")
        _pin(conn, "1", "match", "llm:entry+eng:generic")
        _posting(conn, "2", NEW_GRAD, FEED_STUB, "feed")
        _pin(conn, "2", "match", "llm:entry+eng:generic")

    path = _db(tmp_path, build)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    assert _audit(path) == 0
    out = capsys.readouterr().out
    assert "audited 2 llm pin(s)" in out and "dry run" in out
    assert "kept                      1" in out
    assert "cleared → will re-derive  1" in out
    assert NO_LEVEL[:40] in out                       # the sample names the title
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before

    assert _audit(path, "--write") == 0
    assert set(_overrides(path)) == {("Acme", "2")}


def test_feed_entry_pin_kept_only_if_title_matches(criteria):
    """Metadata states no level, so an `entry` reading off it is the title's or nothing."""
    rows = [
        _row("1", NEW_GRAD, "match", "llm:entry+eng:generic", FEED_STUB, "feed"),
        _row("2", NO_LEVEL, "match", "llm:entry+eng:generic", FEED_STUB, "feed"),
        # A quote does not rescue a feed row: the stub can only ever quote metadata.
        _row("3", NO_LEVEL, "match", "llm:entry+eng:generic:New York", FEED_STUB, "feed"),
    ]
    found = {f.ats_job_id: f for f in audit.classify_pins(rows, criteria)}
    assert found["1"].action == audit.KEEP
    assert found["2"].action == audit.CLEAR
    assert found["3"].action == audit.CLEAR
    assert "uncertain" in found["2"].why


def test_not_entry_pin_without_grounded_evidence_is_cleared(criteria):
    rows = [
        _row("1", NO_LEVEL, "reject", "llm:not_entry:5+ years of experience", PROSE, "ats"),
        _row("2", NO_LEVEL, "reject", "llm:not_entry:", PROSE, "ats"),
        # Grounded, whitespace-normalized and casefolded, the check the reading passed.
        _row("3", NO_LEVEL, "reject", "llm:not_entry:0-2  YEARS of experience", PROSE,
             "prose"),
        # In the title counts too; and any source, feed included.
        _row("4", "Staffing Engineer, 7+ years", "reject", "llm:not_entry:7+ years",
             FEED_STUB, "feed"),
    ]
    found = {f.ats_job_id: f for f in audit.classify_pins(rows, criteria)}
    assert found["1"].action == audit.CLEAR and found["1"].why == "quote not found"
    assert found["2"].action == audit.CLEAR and found["2"].why == "no quote"
    assert found["3"].action == audit.KEEP
    assert found["4"].action == audit.KEEP


def test_prose_entry_pins_follow_their_quote_or_stand_pre_grounding(criteria):
    rows = [
        _row("1", NO_LEVEL, "match", "llm:entry+role:backend:recent graduates", PROSE, "ats"),
        _row("2", NO_LEVEL, "match", "llm:entry+role:backend:new grad program", PROSE, "ats"),
        _row("3", NO_LEVEL, "match", "llm:entry+eng:generic", PROSE, "prose"),  # pre-01
    ]
    found = {f.ats_job_id: f for f in audit.classify_pins(rows, criteria)}
    assert [found[j].action for j in "123"] == [audit.KEEP, audit.CLEAR, audit.KEEP]


def test_reason_parsing_reads_the_detail_colon_as_no_evidence():
    assert audit.parse_reason("llm:entry+eng:generic") == ("entry", "")
    assert audit.parse_reason("llm:entry+role:backend") == ("entry", "")
    assert audit.parse_reason("llm:entry+non_engineering:new grad") == ("entry", "new grad")
    assert audit.parse_reason("llm:entry+role:sre:0-2 years: ok") == ("entry", "0-2 years: ok")
    assert audit.parse_reason("llm:not_entry:8+ years") == ("not_entry", "8+ years")
    assert audit.parse_reason("something else")[0] == "unknown"


def test_cleared_row_rederives_and_keeps_its_application(tmp_path, capsys):
    def build(conn):
        _posting(conn, "1", NO_LEVEL, FEED_STUB, "feed")
        _pin(conn, "1", "match", "llm:entry+eng:generic")
        store.record_application(conn, "Acme", "1", NO_LEVEL, "applied", TODAY)
        store.record_decision(conn, "Acme", "1", NO_LEVEL, "match", TODAY)
        store.record_level_reading(conn, "Acme", "1", "entry", "x", "abc", TODAY)

    path = _db(tmp_path, build)
    assert _audit(path, "--write") == 0
    assert "cleared 1 pin(s); 1 verdict(s) moved" in capsys.readouterr().out

    conn = store.connect(path)
    try:
        v = conn.execute("SELECT verdict, decided_by FROM verdicts WHERE ats_job_id='1'"
                         ).fetchone()
        assert (v["verdict"], v["decided_by"]) == ("uncertain", "rules")
        assert store.load_overrides(conn) == {}
        app = conn.execute("SELECT status FROM applications WHERE ats_job_id='1'").fetchone()
        assert app["status"] == "applied"
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM level_readings").fetchone()[0] == 1
    finally:
        conn.close()


def test_audit_pins_is_never_scheduled():
    """By hand only: no nightly command reaches it."""
    import inspect

    for name in ("cmd_check", "cmd_work", "cmd_prepare"):
        src = inspect.getsource(getattr(cli, name))
        assert "audit" not in src, name
