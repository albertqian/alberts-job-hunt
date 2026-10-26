#!/usr/bin/env python3
"""
Job Search Agent
----------------
Runs on GitHub Actions (Tue + Fri). Each run:
  1. Pulls open roles from your watchlist companies' public job boards (Greenhouse/Lever/Ashby).
  2. Uses Claude + web search to discover matching roles at other B2B enterprise SaaS companies
     and to check watchlist companies that have no public job-board API.
  3. Filters by title, seniority, location and salary floor; skips roles already reported.
  4. Scores each new role 0-100 against your resume, with positioning and resume-tailoring advice.
  5. Reads news RSS feeds (config/news_feeds.yaml), picks this week's top stories, writes a positioning guide and a LinkedIn post in your voice.
  6. Emails the brief and saves it to reports/ (job history goes to state/).

Usage:
  python src/agent.py               # full run, sends email
  python src/agent.py --dry-run     # full run, no email (report still saved)
  python src/agent.py --no-search   # watchlist APIs only, no web search (cheap test)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROFILE_DIR = ROOT / "profile"
STATE_DIR = ROOT / "state"
REPORTS_DIR = ROOT / "reports"

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
HTTP_TIMEOUT = 25
HTTP_HEADERS = {"User-Agent": "personal-job-search-agent/1.0"}
STATE_RETENTION_DAYS = 60
TODAY = dt.date.today()

RUN_NOTES: list[str] = []


def note(msg: str) -> None:
    print(msg, flush=True)
    RUN_NOTES.append(msg)


# ---------------------------------------------------------------------------
# Loading config and profile
# ---------------------------------------------------------------------------

def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_profile_text() -> dict:
    samples_dir = PROFILE_DIR / "writing_samples"
    samples = []
    if samples_dir.exists():
        for p in sorted(samples_dir.glob("*.md")):
            samples.append(p.read_text(encoding="utf-8"))
    return {
        "resume": (PROFILE_DIR / "resume.md").read_text(encoding="utf-8"),
        "style_guide": (PROFILE_DIR / "style_guide.md").read_text(encoding="utf-8"),
        "writing_samples": "\n\n---\n\n".join(samples),
    }


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            note(f"Could not parse {path.name}; starting fresh.")
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# Text helpers and filters
# ---------------------------------------------------------------------------

def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def job_key(company: str, title: str) -> str:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return f"{norm(company)}|{norm(title)}"


def title_ok(title: str, profile: dict) -> bool:
    t = (title or "").lower()
    tf = profile["title_filter"]
    if not any(re.search(rx, t) for rx in tf["must_match"]):
        return False
    if any(re.search(rx, t) for rx in tf.get("exclude", [])):
        return False
    return any(re.search(rx, t) for rx in tf["seniority"])


US_RX = re.compile(r"\b(us|usa|u\.s\.?|united states|north america|americas|anywhere in the us)\b")


def location_ok(location: str, is_remote: bool, profile: dict) -> bool:
    loc = (location or "").lower()
    lp = profile["locations"]
    if any(area in loc for area in lp.get("hybrid_areas", [])):
        return True
    remote = is_remote or "remote" in loc
    if not (remote and lp.get("remote_us", True)):
        return False
    if US_RX.search(loc):
        return True
    for marker in lp.get("non_us_markers", []):
        if re.search(rf"\b{re.escape(marker.lower())}\b", loc):
            return False
    # Remote with a US city/state or no region given: keep.
    return True


_MONEY = r"\$\s?(\d{2,3}(?:,\d{3})+(?:\.\d+)?|\d{2,3}(?:\.\d+)?\s?[kK])"
SALARY_RX = re.compile(_MONEY + r"\s*(?:-|–|—|to)\s*(?:USD\s*)?" + _MONEY)


def _money_to_num(s: str) -> float:
    s = s.replace(",", "").replace(" ", "").lower()
    return float(s[:-1]) * 1000 if s.endswith("k") else float(s)


def parse_salary(text: str) -> tuple[float, float] | None:
    """Return (min, max) of annual USD base ranges found in text, or None."""
    lows, highs = [], []
    for a, b in SALARY_RX.findall(text or ""):
        lo, hi = _money_to_num(a), _money_to_num(b)
        if 40_000 <= lo <= 1_000_000 and 40_000 <= hi <= 1_000_000 and hi >= lo:
            lows.append(lo)
            highs.append(hi)
    if not lows:
        return None
    return min(lows), max(highs)


def fmt_salary(rng) -> str:
    if not rng:
        return "not listed"
    lo, hi = rng
    return f"${lo/1000:,.0f}K–${hi/1000:,.0f}K"


def salary_ok(rng, profile: dict) -> bool:
    floor = profile.get("min_base_salary_usd") or 0
    return rng is None or rng[1] >= floor


# ---------------------------------------------------------------------------
# Public job-board APIs (free, no key)
# ---------------------------------------------------------------------------

def _get(url: str, **params):
    try:
        r = requests.get(url, params=params or None, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except (requests.RequestException, ValueError):
        return None


def fetch_greenhouse(slug: str, profile: dict) -> list[dict] | None:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not data or not data.get("jobs"):
        return None
    out = []
    for j in data["jobs"]:
        if not title_ok(j.get("title"), profile):
            out.append(None)  # keeps "board exists" signal
            continue
        detail = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{j['id']}") or {}
        desc = html_to_text(detail.get("content", ""))
        out.append({
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "is_remote": False,
            "url": j.get("absolute_url", ""),
            "posted": (j.get("updated_at") or "")[:10],
            "description": desc,
            "salary": parse_salary(desc),
        })
    return out


def fetch_lever(slug: str, profile: dict) -> list[dict] | None:
    data = _get(f"https://api.lever.co/v0/postings/{slug}", mode="json")
    if not isinstance(data, list) or not data:
        return None
    out = []
    for j in data:
        cats = j.get("categories") or {}
        locs = [cats.get("location") or ""] + list(cats.get("allLocations") or [])
        desc = (j.get("descriptionPlain") or "") + "\n" + "\n".join(
            html_to_text(l.get("content", "")) for l in (j.get("lists") or []))
        desc += "\n" + (j.get("additionalPlain") or "")
        sal = None
        sr = j.get("salaryRange") or {}
        if sr.get("min") and sr.get("max") and (sr.get("interval") in (None, "per-year-salary")):
            sal = (float(sr["min"]), float(sr["max"]))
        out.append({
            "title": j.get("text", ""),
            "location": "; ".join(dict.fromkeys(l for l in locs if l)),
            "is_remote": (j.get("workplaceType") == "remote"),
            "url": j.get("hostedUrl", ""),
            "posted": dt.datetime.fromtimestamp(j.get("createdAt", 0) / 1000).date().isoformat()
            if j.get("createdAt") else "",
            "description": desc.strip(),
            "salary": sal or parse_salary(desc),
        })
    return out


def fetch_ashby(slug: str, profile: dict) -> list[dict] | None:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", includeCompensation="true")
    if not data or not data.get("jobs"):
        return None
    out = []
    for j in data["jobs"]:
        locs = [j.get("location") or ""] + [
            (s or {}).get("location", "") for s in (j.get("secondaryLocations") or [])]
        sal = None
        for comp in ((j.get("compensation") or {}).get("summaryComponents") or []):
            if comp.get("compensationType") == "Salary" and comp.get("minValue") and comp.get("maxValue"):
                sal = (float(comp["minValue"]), float(comp["maxValue"]))
                break
        desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", ""))
        out.append({
            "title": j.get("title", ""),
            "location": "; ".join(dict.fromkeys(l for l in locs if l)),
            "is_remote": bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
            "url": j.get("jobUrl", ""),
            "posted": (j.get("publishedAt") or "")[:10],
            "description": desc,
            "salary": sal or parse_salary(desc),
        })
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def slug_candidates(company: dict) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]", "", company["name"].lower()).strip()
    cands = [company.get("slug"), base.replace(" ", ""), base.replace(" ", "-")]
    return [c for c in dict.fromkeys(cands) if c]


def fetch_watchlist(watchlist: list[dict], profile: dict, ats_cache: dict):
    """Returns (jobs, companies_without_api)."""
    jobs, no_api = [], []
    for company in watchlist:
        name = company["name"]
        cached = ats_cache.get(name)
        if company.get("ats"):
            attempts = [(company["ats"], s) for s in slug_candidates(company)]
        elif cached and cached.get("ats"):
            attempts = [(cached["ats"], cached["slug"])]
        else:
            attempts = [(ats, s) for s in slug_candidates(company) for ats in FETCHERS]

        found = None
        for ats, slug in attempts:
            result = FETCHERS[ats](slug, profile)
            if result is not None:
                found = (ats, slug, result)
                break
            time.sleep(0.2)

        if not found:
            ats_cache[name] = {"ats": None, "checked": TODAY.isoformat()}
            no_api.append(company)
            continue

        ats, slug, result = found
        ats_cache[name] = {"ats": ats, "slug": slug, "checked": TODAY.isoformat()}
        kept = 0
        for j in result:
            if not j or not title_ok(j["title"], profile):
                continue
            if not location_ok(j["location"], j["is_remote"], profile):
                continue
            if not salary_ok(j["salary"], profile):
                note(f"Filtered (below comp floor): {name} — {j['title']} ({fmt_salary(j['salary'])})")
                continue
            j.update(company=name, industry=company.get("industry", ""), source=f"{ats} board")
            jobs.append(j)
            kept += 1
        print(f"  {name}: {ats}/{slug} → {kept} matching roles", flush=True)
    return jobs, no_api


# ---------------------------------------------------------------------------
# Claude helpers
# ---------------------------------------------------------------------------

_client = None


def claude():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def _collect_urls(obj, bucket: set) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "url" and isinstance(v, str):
                bucket.add(normalize_url(v))
            else:
                _collect_urls(v, bucket)
    elif isinstance(obj, list):
        for v in obj:
            _collect_urls(v, bucket)


def normalize_url(u: str) -> str:
    try:
        p = urlparse(u.strip())
        return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}"
    except Exception:
        return u


def extract_json(text: str):
    """Pull the last JSON object/array out of a model reply."""
    fences = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    candidates = list(reversed(fences)) + [text]
    for c in candidates:
        c = c.strip()
        pairs = sorted((("[", "]"), ("{", "}")), key=lambda oc: (c.find(oc[0]) == -1, c.find(oc[0])))
        for opener, closer in pairs:
            start, end = c.find(opener), c.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(c[start:end + 1])
                except json.JSONDecodeError:
                    continue
    return None


def ask_claude(system: str, prompt: str, max_tokens: int = 8000, web_searches: int = 0,
               tool_overrides: dict | None = None):
    """Call Claude, optionally with web search. Returns (text, set_of_urls_seen_in_search)."""
    messages = [{"role": "user", "content": prompt}]
    kwargs = dict(model=MODEL, max_tokens=max_tokens, system=system, messages=messages)
    if web_searches:
        kwargs["tools"] = [{
            "type": "web_search_20260318",
            "name": "web_search",
            "max_uses": web_searches,
            "allowed_callers": ["direct"],
            "user_location": {"type": "approximate", "country": "US",
                              "region": "California", "timezone": "America/Los_Angeles"},
            **(tool_overrides or {}),
        }]
    texts, urls = [], set()
    for _ in range(8):
        for attempt in range(3):
            try:
                resp = claude().messages.create(**kwargs)
                break
            except Exception as e:  # rate limits / transient errors
                if attempt == 2:
                    raise
                note(f"Claude call failed ({type(e).__name__}); retrying.")
                time.sleep(10 * (attempt + 1))
        dumped = resp.model_dump()
        for block in dumped.get("content", []):
            if block.get("type") == "web_search_tool_result":
                _collect_urls(block.get("content"), urls)
            elif block.get("type") == "text":
                texts.append(block.get("text", ""))
                _collect_urls(block.get("citations") or [], urls)
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    return "\n".join(texts), urls


def verify_links(items: list[dict], seen_urls: set, url_field: str = "url") -> list[dict]:
    """Keep items whose link came from real search results; flag same-domain links."""
    seen_domains = {u.split("/")[0] for u in seen_urls}
    kept = []
    for it in items:
        u = normalize_url(it.get(url_field) or "")
        if not u:
            continue
        if u in seen_urls:
            it["link_check"] = "verified"
        elif u.split("/")[0] in seen_domains:
            it["link_check"] = "domain verified, check link"
        else:
            note(f"Dropped item with unverifiable link: {it.get(url_field)}")
            continue
        kept.append(it)
    return kept


# ---------------------------------------------------------------------------
# Discovery and news (web search)
# ---------------------------------------------------------------------------

def discover_jobs(profile: dict, no_api: list[dict], api_companies: list[str]) -> list[dict]:
    loc = profile["locations"]
    check_names = ", ".join(f"{c['name']} ({c.get('domain', '')})" for c in no_api[:15]) or "none"
    system = ("You are a meticulous job-search researcher. You only report job postings you actually "
              "found in search results during this conversation, with the exact URL from those results. "
              "Never invent or guess URLs, salaries or dates.")
    prompt = f"""Today is {TODAY.isoformat()}. Find currently open job postings that match this search.

ROLES: {', '.join(profile['target_titles'])} (product marketing, senior IC or director level).
COMPANIES: {profile['company_profile'].strip()}
INDUSTRIES: {'; '.join(profile['industries'])}
LOCATIONS: Remote (United States), or hybrid/on-site in the SF Bay Area or California Central Coast (San Luis Obispo / Santa Barbara area).
POSTED: within roughly the last 14 days where the date is visible.

Part A — check these watchlist companies' careers pages for matching roles: {check_names}
Part B — find other matching companies and roles, up to {profile['discovery_target_count']} in total.
Skip these companies (already covered): {', '.join(api_companies) or 'none'}.

Prefer links to the company's careers page or its applicant tracking system (greenhouse.io, lever.co,
ashbyhq.com, myworkdayjobs.com, smartrecruiters.com, icims.com). Use LinkedIn or other job boards
only when no direct link exists. Skip postings that look closed or expired.

Return ONLY a JSON array, no commentary. Each item:
{{"company": "", "title": "", "location": "", "url": "", "posted": "YYYY-MM-DD or unknown",
  "salary_text": "as written in the posting, or empty", "company_stage": "e.g. Fortune 500 / public / Series C",
  "industry": "", "summary": "3-4 sentences: what the role owns, key requirements, notable details"}}"""
    text, urls = ask_claude(system, prompt, max_tokens=8000, web_searches=profile["discovery_max_searches"])
    items = extract_json(text)
    if not isinstance(items, list):
        note("Discovery search returned no parseable results.")
        return []
    items = verify_links(items, urls)
    jobs = []
    for it in items:
        title = it.get("title", "")
        if not title_ok(title, profile):
            continue
        location = it.get("location", "")
        if not location_ok(location, False, profile):
            continue
        sal = parse_salary(it.get("salary_text", ""))
        if not salary_ok(sal, profile):
            note(f"Filtered (below comp floor): {it.get('company')} — {title} ({fmt_salary(sal)})")
            continue
        jobs.append({
            "company": it.get("company", ""), "title": title, "location": location,
            "is_remote": "remote" in location.lower(), "url": it.get("url", ""),
            "posted": it.get("posted", ""), "description": it.get("summary", ""),
            "salary": sal, "industry": it.get("industry", ""),
            "company_stage": it.get("company_stage", ""),
            "source": f"web search ({it.get('link_check')})",
        })
    return jobs


RSS_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}


def fetch_feed(url: str):
    import feedparser
    try:
        r = requests.get(url, headers=RSS_HEADERS, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        parsed = feedparser.parse(r.content)
        if parsed.bozo and not parsed.entries:
            return None, "not a valid feed"
        return parsed.entries, None
    except requests.RequestException as e:
        return None, type(e).__name__


def _entry_date(entry) -> dt.date | None:
    for field in ("published_parsed", "updated_parsed"):
        t = entry.get(field)
        if t:
            return dt.date(t.tm_year, t.tm_mon, t.tm_mday)
    return None


def collect_rss_stories(cfg: dict) -> list[dict]:
    cutoff = TODAY - dt.timedelta(days=cfg.get("lookback_days", 7))
    kw = cfg.get("keywords") or []
    kw_rx = re.compile(r"\b(" + "|".join(re.escape(k) for k in kw) + r")\b", re.I) if kw else None
    stories, seen_titles = [], set()
    for feed in cfg.get("feeds", []):
        entries, err = fetch_feed(feed["url"])
        label = f"{feed['outlet']} {feed.get('section', '')}".strip()
        if err:
            note(f"News feed failed: {label} ({err})")
            continue
        kept = 0
        for e in entries:
            date = _entry_date(e)
            if date and date < cutoff:
                continue
            title = e.get("title", "")
            if "news.google.com" in feed["url"]:
                title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title).strip()
            summary = html_to_text(e.get("summary", ""))[:400]
            if "news.google.com" in feed["url"]:
                summary = ""  # Google News summaries just repeat the headline
            if kw_rx and not kw_rx.search(f"{title} {summary}"):
                continue
            key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
            if not key or key in seen_titles:
                continue
            seen_titles.add(key)
            stories.append({"headline": title, "outlet": feed["outlet"], "url": e.get("link", ""),
                            "date": date.isoformat() if date else "", "feed_summary": summary})
            kept += 1
        print(f"  {label}: {len(entries)} stories, {kept} on-topic", flush=True)
    return stories


def pick_news(stories: list[dict], profile: dict, cfg: dict) -> list[dict]:
    """Have Claude choose the most relevant stories. Links/headlines come from the feeds."""
    if not stories:
        return []
    stories = sorted(stories, key=lambda s: s["date"], reverse=True)[:120]
    n = cfg.get("stories_to_pick", 5)
    topics = "\n".join(f"- {t}" for t in profile["news_topics"])
    listing = "\n".join(f"[{i}] {s['outlet']} ({s['date'] or 'recent'}): {s['headline']}"
                        + (f" — {s['feed_summary']}" if s["feed_summary"] else "")
                        for i, s in enumerate(stories))
    system = ("You are an industry analyst for a B2B product marketer working in data/AI platforms, "
              "decision intelligence and HR tech. Separate real industry developments from vendor PR.")
    prompt = f"""Today is {TODAY.isoformat()}. From the stories below, pick the {n} most significant for these topics:
{topics}

Prefer stories with clear market impact (M&A, funding, major launches, earnings that signal a shift,
regulation, layoffs or hiring trends, enterprise AI adoption) and avoid near-duplicates.
Use only the information given; don't add facts that aren't in the headline or summary.

STORIES:
{listing}

Return ONLY a JSON array of {n} items:
[{{"id": <number in brackets>, "summary": "1-2 sentences based on the headline/summary",
   "why_it_matters": "1-2 sentences for a B2B product marketer", "pr_or_news": "news | vendor PR"}}]"""
    text, _ = ask_claude(system, prompt, max_tokens=3000)
    picks = extract_json(text)
    if not isinstance(picks, list):
        note("News ranking returned unparseable output; using the most recent stories.")
        picks = [{"id": i} for i in range(min(n, len(stories)))]
    out = []
    for p in picks:
        try:
            s = dict(stories[int(p["id"])])
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        s["summary"] = p.get("summary") or s["feed_summary"]
        s["why_it_matters"] = p.get("why_it_matters", "")
        s["pr_or_news"] = p.get("pr_or_news", "news")
        out.append(s)
    return out[:n]


def search_news_fallback(profile: dict, cfg: dict, needed: int) -> list[dict]:
    domains = cfg.get("fallback_web_search_domains") or []
    system = ("You are an industry analyst. Report only stories you found in search results in this "
              "conversation, with their exact URLs. Separate real industry developments from vendor PR.")
    topics = "\n".join(f"- {t}" for t in profile["news_topics"])
    prompt = f"""Today is {TODAY.isoformat()}. Find the {needed} most significant news stories from the past 7 days on:
{topics}

Return ONLY a JSON array:
[{{"headline": "", "outlet": "", "url": "", "date": "YYYY-MM-DD", "summary": "2 sentences",
   "why_it_matters": "1-2 sentences for a B2B product marketer", "pr_or_news": "news | vendor PR"}}]"""
    messages_tool_override = {"allowed_domains": domains} if domains else {}
    text, urls = ask_claude(system, prompt, max_tokens=4000, web_searches=profile["news_max_searches"],
                            tool_overrides=messages_tool_override)
    items = extract_json(text)
    if not isinstance(items, list):
        note("Fallback news search returned no parseable results.")
        return []
    return verify_links(items, urls)


def fetch_news(profile: dict, allow_search: bool) -> list[dict]:
    cfg = load_yaml(CONFIG_DIR / "news_feeds.yaml")
    print("Reading news feeds…")
    stories = collect_rss_stories(cfg)
    news = pick_news(stories, profile, cfg)
    minimum = cfg.get("min_stories_before_fallback", 3)
    if len(news) < minimum:
        if allow_search:
            note(f"Only {len(news)} usable stories from feeds; topping up with web search on "
                 f"{', '.join(cfg.get('fallback_web_search_domains') or [])}.")
            try:
                news += search_news_fallback(profile, cfg, cfg.get("stories_to_pick", 5) - len(news))
            except Exception as e:
                note(f"Fallback news search failed: {e}")
        else:
            note(f"Only {len(news)} usable stories from feeds (web search fallback skipped).")
    return news


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

RUBRIC = """Score each job 0-100 as the SUM of five sub-scores (be strict and consistent; most jobs
should land between 45 and 85; reserve 85+ for roles where the candidate is a near-perfect match):
- role_level (0-30): title, seniority and scope vs. the candidate's experience. A Director role
  requiring prior people management the resume doesn't show should lose points here.
- domain (0-25): overlap between the company's product/market and the candidate's domains
  (decision intelligence, data/AI platforms, cloud partnerships, HR tech content).
- evidence (0-25): how directly the resume's accomplishments prove the job's top requirements.
- logistics (0-10): location fit and listed compensation vs. a $FLOOR base floor
  (unknown comp = 6/10 at most).
- trajectory (0-10): whether the role advances the candidate's career (scope, AI relevance, brand).

Verdict: "Apply now" (80+), "Worth a look" (65-79), "Stretch" (50-64), "Skip" (<50)."""


def score_jobs(jobs: list[dict], profile: dict, texts: dict) -> list[dict]:
    rubric = RUBRIC.replace("$FLOOR", f"${profile['min_base_salary_usd']:,}")
    system = f"""You are a senior product-marketing career coach and recruiter. You assess job fit
honestly — no flattery — and give specific, actionable advice tied to the candidate's real resume.

CANDIDATE HEADLINE: {profile['headline'].strip()}

PROOF POINTS:
{chr(10).join('- ' + p for p in profile['proof_points'])}

RESUME:
{texts['resume']}

{rubric}"""
    scored = []
    batch_size = 5
    for i in range(0, len(jobs), batch_size):
        batch = jobs[i:i + batch_size]
        listing = []
        for idx, j in enumerate(batch):
            listing.append(
                f"### JOB {idx}\nCompany: {j['company']} ({j.get('company_stage') or j.get('industry', '')})\n"
                f"Title: {j['title']}\nLocation: {j['location']}\nSalary: {fmt_salary(j['salary'])}\n"
                f"Description:\n{(j.get('description') or '')[:3500]}")
        prompt = ("\n\n".join(listing) + """

For EACH job return a JSON array item:
{"job": <index>, "role_level": n, "domain": n, "evidence": n, "logistics": n, "trajectory": n,
 "verdict": "", "why_fit": ["...", "..."], "gaps": ["...", "..."],
 "positioning": "1-2 sentences: the story the candidate should tell for THIS role",
 "resume_edits": ["3 specific edits, each naming the resume bullet to change and how"],
 "keywords": ["5 keywords from the posting to mirror"]}
Return ONLY the JSON array.""")
        try:
            text, _ = ask_claude(system, prompt, max_tokens=6000)
        except Exception as e:
            note(f"Scoring batch failed: {e}")
            continue
        results = extract_json(text)
        if not isinstance(results, list):
            note("Scoring batch returned unparseable output; skipped.")
            continue
        for r in results:
            try:
                j = batch[int(r["job"])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            caps = {"role_level": 30, "domain": 25, "evidence": 25, "logistics": 10, "trajectory": 10}
            subs = {k: max(0, min(cap, int(r.get(k, 0) or 0))) for k, cap in caps.items()}
            j["subscores"] = subs
            j["score"] = sum(subs.values())
            j["verdict"] = verdict_for(j["score"])
            for k in ("why_fit", "gaps", "positioning", "resume_edits", "keywords"):
                j[k] = r.get(k)
            scored.append(j)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def verdict_for(score: int) -> str:
    if score >= 80:
        return "Apply now"
    if score >= 65:
        return "Worth a look"
    if score >= 50:
        return "Stretch"
    return "Skip"


# ---------------------------------------------------------------------------
# Positioning guide + LinkedIn post
# ---------------------------------------------------------------------------

def write_guide_and_post(scored: list[dict], news: list[dict], profile: dict, texts: dict) -> dict:
    top = scored[:10]
    jobs_brief = "\n".join(
        f"- {j['company']} — {j['title']} (score {j['score']}): keywords {', '.join(j.get('keywords') or [])}"
        for j in top) or "- (no new roles this run)"
    news_brief = "\n".join(
        f"- {n.get('headline')} ({n.get('outlet')}, {n.get('date')}) {n.get('url')}\n  {n.get('summary')} "
        f"Why it matters: {n.get('why_it_matters')}" for n in news) or "- (no news found)"
    li = profile["linkedin"]
    system = f"""You are a product-marketing career strategist and ghostwriter for {profile['candidate_name']}.
Be direct and specific. No flattery.

RESUME:
{texts['resume']}

STYLE GUIDE:
{texts['style_guide']}

WRITING SAMPLES (match this voice):
{texts['writing_samples']}"""
    prompt = f"""This week's top matching roles:
{jobs_brief}

This week's industry news:
{news_brief}

Produce two things.

1) "positioning_markdown": a concise markdown guide (300-450 words) with these ### sections:
   ### What the market is asking for — 3-4 bullets on recurring requirements across these roles
   ### Your positioning this week — a one-line positioning statement plus 2-3 supporting bullets
   ### Resume headline and summary — a rewritten headline (1 line) and summary (2-3 sentences)
   ### Gaps to close — 2-3 bullets with a concrete action each (e.g., a proof point to add, a skill to show)

2) "linkedin_post": a LinkedIn post following the STYLE GUIDE's LinkedIn rules, grounded in ONE news
   item above (include its URL in the post). Point of view: {li['point_of_view'].strip()}
   Stance: thought leadership only — no job-search signals, no mention of the candidate's current
   employer or its products.
   Also return "post_source_url" and "post_angle" (one sentence on why this angle builds the candidate's
   brand for the roles above).

Return ONLY JSON: {{"positioning_markdown": "", "linkedin_post": "", "post_source_url": "", "post_angle": ""}}"""
    text, _ = ask_claude(system, prompt, max_tokens=5000)
    out = extract_json(text)
    if not isinstance(out, dict):
        note("Guide/post generation returned unparseable output.")
        return {"positioning_markdown": "_Not generated this run._", "linkedin_post": "", "post_source_url": "",
                "post_angle": ""}
    banned = [t for t in li.get("banned_terms", [])
              if re.search(rf"\b{re.escape(t)}\b", out.get("linkedin_post", ""), flags=re.I)]
    out["warnings"] = banned
    return out


# ---------------------------------------------------------------------------
# Report + email
# ---------------------------------------------------------------------------

def md_escape(s: str) -> str:
    return (s or "").replace("|", "/").replace("\n", " ")


def build_report(scored, still_open, news, guide, stats) -> str:
    date_str = TODAY.strftime("%a %b %-d, %Y")
    L = [f"# Job Search Brief — {date_str}", ""]
    L.append(f"**{len(scored)} new roles scored** · {stats['still_open']} still open from earlier · "
             f"{stats['watchlist_checked']} watchlist companies checked · "
             f"{stats['filtered']} dropped for listed pay below your floor")
    L.append("")

    L += ["## Positioning guide", "", guide.get("positioning_markdown", ""), ""]

    L += ["## New roles", ""]
    if scored:
        L += ["| Score | Verdict | Role | Company | Location | Base comp |",
              "|---|---|---|---|---|---|"]
        for j in scored:
            L.append(f"| **{j['score']}** | {j['verdict']} | [{md_escape(j['title'])}]({j['url']}) | "
                     f"{md_escape(j['company'])} | {md_escape(j['location'])[:60]} | {fmt_salary(j['salary'])} |")
        L.append("")
        for j in [x for x in scored if x["score"] >= 50][:8]:
            s = j["subscores"]
            L += [f"### {j['score']} · {j['company']} — [{j['title']}]({j['url']})",
                  f"*{j['location']} · comp {fmt_salary(j['salary'])} · posted {j.get('posted') or 'unknown'} · "
                  f"source: {j['source']}*", "",
                  f"Fit breakdown: role/level {s['role_level']}/30 · domain {s['domain']}/25 · "
                  f"evidence {s['evidence']}/25 · logistics {s['logistics']}/10 · trajectory {s['trajectory']}/10", "",
                  f"**Positioning:** {j.get('positioning') or ''}", "",
                  "**Why you fit**"] + [f"- {x}" for x in (j.get("why_fit") or [])] + [
                  "", "**Gaps**"] + [f"- {x}" for x in (j.get("gaps") or [])] + [
                  "", "**Tailor your resume**"] + [f"- {x}" for x in (j.get("resume_edits") or [])] + [
                  "", f"**Keywords to mirror:** {', '.join(j.get('keywords') or [])}", ""]
    else:
        L += ["No new matching roles this run.", ""]

    if still_open:
        L += ["## Still open from earlier briefs", "",
              "| Score | Role | Company | First seen |", "|---|---|---|---|"]
        for j in still_open[:10]:
            L.append(f"| {j.get('score', '–')} | [{md_escape(j['title'])}]({j['url']}) | "
                     f"{md_escape(j['company'])} | {j['first_seen']} |")
        L.append("")

    L += ["## Industry news this week", ""]
    for n in news:
        tag = " *(vendor PR)*" if "pr" in (n.get("pr_or_news") or "").lower() else ""
        L += [f"**[{n.get('headline')}]({n.get('url')})** — {n.get('outlet')}, {n.get('date')}{tag}  ",
              f"{n.get('summary')} *Why it matters:* {n.get('why_it_matters')}", ""]
    if not news:
        L += ["No news pulled this run.", ""]

    L += ["## LinkedIn post draft", ""]
    if guide.get("warnings"):
        L += [f"> ⚠️ Check before posting — draft mentions: {', '.join(guide['warnings'])}", ""]
    post = guide.get("linkedin_post") or "_Not generated this run._"
    L += ["```", post, "```", "",
          f"*Source:* {guide.get('post_source_url', '')}  ",
          f"*Why this angle:* {guide.get('post_angle', '')}", ""]

    if RUN_NOTES:
        L += ["---", "<details><summary>Run notes</summary>", ""] + [f"- {n}" for n in RUN_NOTES] + ["", "</details>"]
    return "\n".join(L)


EMAIL_CSS = """body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1f2328;max-width:820px;
margin:0 auto;padding:16px;line-height:1.5}h1{font-size:22px}h2{font-size:18px;border-bottom:1px solid #d0d7de;
padding-bottom:4px;margin-top:28px}h3{font-size:15px;margin-bottom:4px}table{border-collapse:collapse;width:100%;
font-size:13px}th,td{border:1px solid #d0d7de;padding:6px;text-align:left;vertical-align:top}th{background:#f6f8fa}
pre{background:#f6f8fa;padding:12px;white-space:pre-wrap;font-family:inherit;font-size:14px;border-radius:6px}
blockquote{border-left:4px solid #d4a72c;margin:0;padding:4px 12px;background:#fff8c5}a{color:#0969da}"""


def send_email(subject: str, report_md: str) -> None:
    import markdown as md
    user = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to_addrs = [a.strip() for a in os.getenv("EMAIL_TO", user).split(",") if a.strip()]
    body = md.markdown(report_md, extensions=["tables", "fenced_code"])
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to_addrs)
    msg.attach(MIMEText(report_md, "plain", "utf-8"))
    msg.attach(MIMEText(f"<html><head><style>{EMAIL_CSS}</style></head><body>{body}</body></html>", "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, password)
        s.sendmail(user, to_addrs, msg.as_string())
    print(f"Email sent to {', '.join(to_addrs)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't send email")
    ap.add_argument("--no-search", action="store_true", help="skip web search (watchlist APIs only)")
    args = ap.parse_args()

    profile = load_yaml(CONFIG_DIR / "profile.yaml")
    watchlist = load_yaml(CONFIG_DIR / "watchlist.yaml").get("companies", [])
    texts = load_profile_text()
    seen = load_json(STATE_DIR / "seen_jobs.json", {})
    ats_cache = load_json(STATE_DIR / "ats_cache.json", {})

    print(f"Checking {len(watchlist)} watchlist companies…")
    jobs, no_api = fetch_watchlist(watchlist, profile, ats_cache)
    api_companies = [c["name"] for c in watchlist if c not in no_api]
    if no_api:
        note("No public job-board API found for: " + ", ".join(c["name"] for c in no_api)
             + " (checked via web search instead).")

    if not args.no_search:
        print("Discovering roles via web search…")
        try:
            jobs += discover_jobs(profile, no_api, api_companies)
        except Exception as e:
            note(f"Discovery search failed: {e}")

    # News runs on every run (RSS feeds are free); web search is only a fallback.
    try:
        news = fetch_news(profile, allow_search=not args.no_search)
    except Exception as e:
        note(f"News step failed: {e}")
        news = []

    filtered_count = sum(1 for n in RUN_NOTES if n.startswith("Filtered"))

    # De-duplicate within this run, then split new vs. previously seen.
    unique = {}
    for j in jobs:
        unique.setdefault(job_key(j["company"], j["title"]), j)
    new_jobs, still_open = [], []
    for key, j in unique.items():
        if key in seen:
            seen[key]["last_seen"] = TODAY.isoformat()
            if seen[key]["first_seen"] != TODAY.isoformat():
                still_open.append(seen[key])
        else:
            new_jobs.append((key, j))
    still_open.sort(key=lambda x: x.get("score") or 0, reverse=True)

    # Watchlist (direct API) roles first, then discovered roles; cap for cost.
    new_jobs.sort(key=lambda kj: 0 if "board" in kj[1]["source"] else 1)
    to_score = new_jobs[: profile.get("max_jobs_to_score", 20)]
    if len(new_jobs) > len(to_score):
        note(f"{len(new_jobs) - len(to_score)} additional new roles not scored this run (max_jobs_to_score).")

    print(f"Scoring {len(to_score)} new roles…")
    scored = score_jobs([j for _, j in to_score], profile, texts) if to_score else []

    for key, j in to_score:
        seen[key] = {
            "company": j["company"], "title": j["title"], "url": j["url"],
            "first_seen": TODAY.isoformat(), "last_seen": TODAY.isoformat(),
            "score": j.get("score"), "verdict": j.get("verdict"), "source": j["source"],
        }

    print("Writing positioning guide and LinkedIn post…")
    try:
        guide = write_guide_and_post(scored, news, profile, texts)
    except Exception as e:
        note(f"Guide/post generation failed: {e}")
        guide = {"positioning_markdown": "_Not generated this run._", "linkedin_post": ""}

    stats = {"still_open": len(still_open), "watchlist_checked": len(watchlist), "filtered": filtered_count}
    report = build_report(scored, still_open, news, guide, stats)

    REPORTS_DIR.mkdir(exist_ok=True)
    report_path = REPORTS_DIR / f"{TODAY.isoformat()}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"Report saved to reports/{report_path.name}")

    cutoff = (TODAY - dt.timedelta(days=STATE_RETENTION_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v.get("last_seen", "") >= cutoff}
    save_json(STATE_DIR / "seen_jobs.json", seen)
    save_json(STATE_DIR / "ats_cache.json", ats_cache)

    top = scored[0] if scored else None
    subject = (f"Job brief {TODAY.strftime('%b %-d')}: {len(scored)} new roles"
               + (f", top {top['score']} at {top['company']}" if top else ""))
    if args.dry_run:
        print("Dry run: email not sent.")
    else:
        send_email(subject, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
