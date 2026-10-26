"""Offline tests: filters, salary parsing, and a full mocked run (no network, no API key)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import agent  # noqa: E402

PROFILE = agent.load_yaml(agent.CONFIG_DIR / "profile.yaml")


def test_title_filter():
    ok = ["Senior Product Marketing Manager", "Sr. Product Marketing Manager, AI",
          "Director of Product Marketing", "Head of Product Marketing", "Principal PMM, Data Cloud",
          "Group Product Marketing Manager"]
    bad = ["Product Marketing Manager", "Associate Product Marketing Manager", "Senior Product Manager",
           "VP, Product Marketing", "Senior Content Marketing Manager"]
    assert all(agent.title_ok(t, PROFILE) for t in ok)
    assert not any(agent.title_ok(t, PROFILE) for t in bad)


def test_location_filter():
    assert agent.location_ok("Remote - United States", False, PROFILE)
    assert agent.location_ok("San Francisco, CA", False, PROFILE)
    assert agent.location_ok("San Luis Obispo, CA (Hybrid)", False, PROFILE)
    assert agent.location_ok("Austin, TX", True, PROFILE)          # remote-flagged US job
    assert agent.location_ok("Remote, US or Canada", False, PROFILE)
    assert not agent.location_ok("Remote - UK", False, PROFILE)
    assert not agent.location_ok("New York, NY", False, PROFILE)   # on-site outside target areas
    assert not agent.location_ok("Remote, Germany", False, PROFILE)


def test_salary():
    assert agent.parse_salary("The base range is $150,000 - $210,000 per year") == (150000, 210000)
    assert agent.parse_salary("$140K–$180K plus equity") == (140000, 180000)
    assert agent.parse_salary("Pay: $50 - $70 per hour") is None
    assert agent.parse_salary("Zone A $160,000 to $200,000; Zone B $136,000 to $170,000") == (136000, 200000)
    assert agent.salary_ok((110000, 130000), PROFILE) is False
    assert agent.salary_ok((120000, 150000), PROFILE) is True
    assert agent.salary_ok(None, PROFILE) is True


def test_extract_json():
    assert agent.extract_json('Here you go:\n```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert agent.extract_json('prefix {"x": [1,2]} suffix') == {"x": [1, 2]}


def test_full_run_mocked(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(agent, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)

    def fake_gh(slug, profile):
        if slug != "databricks":
            return None
        return [None, {"title": "Senior Product Marketing Manager, Data Governance", "location": "San Francisco, CA",
                       "is_remote": False, "url": "https://databricks.com/job/1", "posted": "2026-10-20",
                       "description": "Own GTM for governance. $170,000 - $230,000", "salary": (170000, 230000)}]

    monkeypatch.setattr(agent, "FETCHERS", {"greenhouse": fake_gh, "lever": lambda s, p: None,
                                            "ashby": lambda s, p: None})

    def fake_ask(system, prompt, max_tokens=8000, web_searches=0):
        if "Find currently open job postings" in prompt:
            return json.dumps([
                {"company": "Workday", "title": "Director, Product Marketing - AI", "location": "Remote, US",
                 "url": "https://workday.wd5.myworkdayjobs.com/job/123", "posted": "2026-10-22",
                 "salary_text": "$180,000 - $250,000", "company_stage": "Public", "industry": "HR tech",
                 "summary": "Lead AI messaging."},
                {"company": "FakeCo", "title": "Senior PMM", "location": "Remote", "url": "https://made-up.example/x",
                 "summary": "Hallucinated."}]), {"workday.wd5.myworkdayjobs.com/job/123"}
        if "most significant news" in prompt:
            return json.dumps([{"headline": "Big data deal", "outlet": "Reuters", "url": "https://reuters.com/a",
                                "date": "2026-10-24", "summary": "S.", "why_it_matters": "W.",
                                "pr_or_news": "news"}]), {"reuters.com/a"}
        if "### JOB 0" in prompt:
            n = prompt.count("### JOB ")
            return json.dumps([{"job": i, "role_level": 25, "domain": 20, "evidence": 20, "logistics": 8,
                                "trajectory": 8, "why_fit": ["a"], "gaps": ["b"], "positioning": "p",
                                "resume_edits": ["e1", "e2", "e3"], "keywords": ["k"]} for i in range(n)]), set()
        return json.dumps({"positioning_markdown": "### What the market is asking for\n- x",
                           "linkedin_post": "Twenty years ago you called a travel agent... https://reuters.com/a",
                           "post_source_url": "https://reuters.com/a", "post_angle": "a"}), set()

    monkeypatch.setattr(agent, "ask_claude", fake_ask)
    monkeypatch.setattr(sys, "argv", ["agent.py", "--dry-run"])
    assert agent.main() == 0

    report = next((tmp_path / "reports").glob("*.md")).read_text()
    assert "Data Governance" in report and "Workday" in report
    assert "FakeCo" not in report  # unverifiable link dropped
    assert "**81**" in report
    seen = json.loads((tmp_path / "state" / "seen_jobs.json").read_text())
    assert len(seen) == 2

    # Second run: same jobs show up as "still open", not new.
    agent.RUN_NOTES.clear()
    monkeypatch.setattr(agent, "TODAY", agent.TODAY + agent.dt.timedelta(days=3))
    assert agent.main() == 0
    report2 = sorted((tmp_path / "reports").glob("*.md"))[-1].read_text()
    assert "0 new roles scored" in report2 and "Still open from earlier" in report2
