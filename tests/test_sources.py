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
