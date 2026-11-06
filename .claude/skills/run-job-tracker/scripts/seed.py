"""Add sample picks and applications to a preview's private database copy.

Run by preview.py with the project's venv, so `jobtracker.store` is importable and the copy gets
the same migrations a real connect applies. Everything written says it is a sample, because a
made-up ranking on a real job title reads as a real judgment otherwise.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobtracker import store

RANKED = 12  # picks take three; the rest fills the "rest of the ranking" drawer
SAMPLE_WHY = "Sample ranking from the preview, not a real judgment."
SAMPLE_NOTE = "Sample application from the preview."
SAMPLE_DESCRIPTION = ("Sample description from the preview, not this employer's text. "
                      "You would build distributed storage and platform services here. ") * 12
# Backdated: an application dated today uses up one of the day's pick slots, so three of them
# would empty the Today tab with no error.
APPLIED_DAYS_AGO = 16
MOVED_DAYS_AGO = 5
STATUSES = ("applied", "interview", "rejected")


def main(db: str) -> None:
    if Path(db).resolve() == (Path.cwd() / "data" / "state.db").resolve():
        raise SystemExit("seed.py refuses to write to the real data/state.db")

    conn = store.connect(db)
    moment = datetime.now(timezone.utc)
    applied = (moment - timedelta(days=APPLIED_DAYS_AGO)).isoformat()
    moved = (moment - timedelta(days=MOVED_DAYS_AGO)).isoformat()
    now = moment.isoformat()

    rows = conn.execute(
        """SELECT p.company, p.ats_job_id, p.title, p.url, p.location
             FROM postings p
             JOIN verdicts v ON p.company = v.company AND p.ats_job_id = v.ats_job_id
            WHERE v.verdict = 'match' AND p.closed_at IS NULL
            ORDER BY p.company, p.ats_job_id
            LIMIT ?""",
        (RANKED,),
    ).fetchall()
    if not rows:
        print("seed: no open matches in this database, so nothing to rank")
        return

    # Only an empty table is seeded, so a database with real rankings or applications is never
    # mixed with samples that look just like them.
    if conn.execute("SELECT COUNT(*) FROM rankings WHERE score IS NOT NULL").fetchone()[0]:
        print("seed: rankings already present, leaving them")
    else:
        for i, row in enumerate(rows):
            key = (row["company"], row["ats_job_id"])
            conn.execute(
                "UPDATE postings SET description = COALESCE(NULLIF(description, ''), ?),"
                " posted_on = COALESCE(posted_on, ?) WHERE company = ? AND ats_job_id = ?",
                (SAMPLE_DESCRIPTION, applied[:10], *key),
            )
            conn.execute(
                "INSERT OR REPLACE INTO rankings (company, ats_job_id, backend_fit, growth,"
                " entry_risk, why, prose_hash, judged_at, score, scored_at)"
                " VALUES (?, ?, 'strong', 'high', 'low', ?, 'preview-sample', ?, ?, ?)",
                (*key, SAMPLE_WHY, now, 90 - i, now),
            )
        print(f"seed: ranked {len(rows)} postings with sample judgments")

    if conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]:
        print("seed: applications already present, leaving them")
    else:
        # From the tail, so the top of the ranking stays available as picks.
        chosen = rows[3:][-len(STATUSES):]
        for row, status in zip(chosen, STATUSES):
            key = (row["company"], row["ats_job_id"])
            store.record_application(conn, *key, row["title"], "applied", applied, SAMPLE_NOTE,
                                     url=row["url"], location=row["location"])
            store.add_application_event(conn, *key, "applied", applied, SAMPLE_NOTE)
            if status != "applied":
                store.record_application(conn, *key, row["title"], status, moved, SAMPLE_NOTE)
                store.add_application_event(conn, *key, status, moved, SAMPLE_NOTE)
        print(f"seed: added {len(chosen)} sample applications")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1])
