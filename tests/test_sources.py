"""Each adapter normalizes its vendor JSON shape into Posting objects."""

from jobtracker.sources import get_source


def test_greenhouse_parse_and_identity():
    src = get_source("greenhouse")
    raw = {
        "jobs": [
            {
                "id": 12345,
                "title": "Software Engineer, New Grad",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
                "location": {"name": "New York, NY"},
                "updated_at": "2026-07-01T00:00:00Z",
            },
            {"id": None, "title": "broken"},  # skipped
        ]
    }
    postings = src.parse_jobs("Acme", raw)
    assert len(postings) == 1
    p = postings[0]
    assert p.ats_job_id == "12345"
    assert p.title == "Software Engineer, New Grad"
    assert p.location == "New York, NY"
    assert p.url.endswith("/12345")
    assert src.parse_identity({"name": "Acme Inc"}) == "Acme Inc"
    assert src.parse_identity({}) is None


def test_ashby_parse_and_identity_from_jobs():
    src = get_source("ashby")
    raw = {
        "jobs": [
            {
                "id": "abc-1",
                "title": "Backend Engineer",
                "jobUrl": "https://jobs.ashbyhq.com/ramp/abc-1",
                "location": "New York",
                "isRemote": True,
                "publishedAt": "2026-07-01",
            }
        ]
    }
    postings = src.parse_jobs("Ramp", raw)
    assert postings[0].ats_job_id == "abc-1"
    assert "Remote" in postings[0].location
    assert src.identity_from_jobs(raw) == "ramp"  # org slug from jobUrl path


def test_lever_parse_and_identity():
    src = get_source("lever")
    raw = [
        {
            "id": "lev-1",
            "text": "Platform Engineer",
            "hostedUrl": "https://jobs.lever.co/matterport/lev-1",
            "categories": {"location": "Remote - US"},
            "createdAt": 1700000000000,
        }
    ]
    postings = src.parse_jobs("Matterport", raw)
    assert postings[0].title == "Platform Engineer"
    assert postings[0].location == "Remote - US"
    assert src.identity_from_jobs(raw) == "matterport"


def test_parsers_tolerate_garbage():
    for ats in ("greenhouse", "ashby", "lever"):
        src = get_source(ats)
        assert src.parse_jobs("X", None) == []
        assert src.parse_jobs("X", {"jobs": "nope"} if ats != "lever" else []) == []


# A hand-built fixture mirroring the SimplifyJobs README <table>: a header row, an open
# role carrying a Simplify UUID, a ↳ continuation that inherits the employer, a 🔒 closed
# row (must be dropped), and a multi-location row.
_AGGREGATOR_HTML = """
<table>
<tr><th>Company</th><th>Role</th><th>Location</th><th>Application</th><th>Age</th></tr>
<tr>
<td><strong><a href="https://simplify.jobs/c/NVIDIA">🔥 NVIDIA</a></strong></td>
<td>Software Engineer, New Grad</td>
<td>Santa Clara, CA</td>
<td><div align="center"><a href="https://nvidia.com/apply/42"><img alt="Apply"></a> <a href="https://simplify.jobs/p/dcb78e15-a5c5-4a55-89dc-2420d65990d0"><img alt="Simplify"></a></div></td>
<td>2d</td>
</tr>
<tr>
<td>↳</td>
<td>Backend Engineer New Grad 🎓</td>
<td>Redmond, WA</br>Austin, TX</td>
<td><div><a href="https://nvidia.com/apply/43"><img alt="Apply"></a></div></td>
<td>2d</td>
</tr>
<tr>
<td><strong><a href="https://simplify.jobs/c/Fidelity">Fidelity</a></strong></td>
<td>Mainframe Software Engineer 1</td>
<td>Columbus, GA</td>
<td>🔒</td>
<td>3d</td>
</tr>
</table>
"""


def test_aggregator_parses_open_rows_and_carries_employer():
    src = get_source("aggregator")
    postings = src.parse_jobs("Simplify New-Grad-Positions", _AGGREGATOR_HTML)

    # The 🔒 Fidelity row is closed and dropped; two open roles remain.
    assert len(postings) == 2

    p0 = postings[0]
    assert p0.company == "Simplify New-Grad-Positions"  # the feed, not the employer
    assert p0.title == "NVIDIA — Software Engineer, New Grad"  # employer moved into title
    assert p0.ats_job_id == "dcb78e15-a5c5-4a55-89dc-2420d65990d0"  # stable Simplify UUID
    assert p0.url == "https://nvidia.com/apply/42"  # the Apply link, not the Simplify one
    assert p0.location == "Santa Clara, CA"

    p1 = postings[1]
    assert p1.title == "NVIDIA — Backend Engineer New Grad"  # ↳ inherited the employer
    assert p1.location == "Redmond, WA; Austin, TX"  # </br> split
    assert p1.ats_job_id != p0.ats_job_id  # no Simplify UUID → hashed, still distinct


def test_aggregator_tolerates_garbage():
    src = get_source("aggregator")
    assert src.parse_jobs("X", None) == []
    assert src.parse_jobs("X", "") == []
    assert src.parse_jobs("X", "<table><tr><td>only one cell</td></tr></table>") == []
