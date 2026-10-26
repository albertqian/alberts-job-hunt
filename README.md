# Job Search Agent

Every Tuesday and Friday at 7am PT, this agent emails you a job search brief:

- **New roles**: Senior/Principal/Director PMM openings at your watchlist companies and other B2B enterprise SaaS companies (Fortune 500 through post-Series B) in data/analytics/cloud, decision intelligence/AI and HR tech. Locations are Remote US, the Bay Area and the Central Coast. Roles with listed base pay under $135K are dropped.
- **Fit score (0–100)** for each role, with a breakdown (role/level 30, domain 25, evidence 25, logistics 10, trajectory 10), why you fit, gaps, a positioning angle, three specific resume edits and keywords to mirror.
- **Positioning guide**: what the market is asking for this week, a positioning statement, a rewritten resume headline and summary, and gaps to close.
- **Industry news**: 5 stories from the past week, labeled as news or vendor PR.
- **LinkedIn post draft** in your voice, built on one news story. It's thought leadership only: no job-search signals and no mention of your current employer. The email warns you if a banned term slips in.

Roles you've already seen don't come back as new. They move to a short "Still open" table.

## How it works

| Step | Source | Cost |
|---|---|---|
| Watchlist jobs | Free public job-board APIs (Greenhouse, Lever, Ashby) | Free |
| Other companies + watchlist companies without an API | Claude with web search | ~$0.01 per search, up to 12/run |
| Industry news | Claude with web search | up to 6 searches/run |
| Scoring, guide, post | Claude | standard token cost |

Every job and news link from web search is checked against the search results Claude actually saw. Links that can't be verified get dropped, so you don't get made-up postings.

## Files

```
.github/workflows/job-search-agent.yml   schedule + run
config/profile.yaml                      roles, locations, salary floor, industries, post rules
config/watchlist.yaml                    target companies (edit freely)
profile/resume.md                        your resume (the agent scores against this)
profile/style_guide.md                   your voice rules for LinkedIn posts
profile/writing_samples/*.md             writing samples (add your best LinkedIn posts here)
src/agent.py                             the agent
tests/test_agent.py                      offline tests
reports/                                 every brief, saved by date
state/                                   job history (so roles aren't repeated)
```

---

## Setup (about 15 minutes)

### Step 1: Create the repo
1. Go to **github.com**.
2. Click **+** (top right) → **New repository**.
3. Repository name: `job-search-agent`.
4. Select **Private**. Your resume lives in this repo, so it must be private.
5. Click **Create repository**.

### Step 2: Upload the files
1. Unzip `job-search-agent.zip` on your computer.
2. On the new repo page, click **uploading an existing file**.
3. Open the unzipped `job-search-agent` folder, select everything inside it, and drag it into the browser.
4. Click **Commit changes**.

### Step 3: Add the workflow file
Mac Finder hides folders that start with a dot, so `.github` usually doesn't get uploaded. Add it by hand:
1. In the repo, click **Add file** → **Create new file**.
2. In the name box, type: `.github/workflows/job-search-agent.yml`
3. Open `job-search-agent.yml` from the zip in a text editor, copy all of it, and paste it in.
4. Click **Commit changes** → **Commit changes**.

### Step 4: Add your secrets
1. In the repo, click **Settings** → **Secrets and variables** → **Actions**.
2. Click **New repository secret** for each of these:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Claude API key (console.anthropic.com → API Keys) |
| `GMAIL_ADDRESS` | The Gmail address that sends the email |
| `GMAIL_APP_PASSWORD` | A Gmail app password (myaccount.google.com → Security → 2-Step Verification → App passwords) |
| `EMAIL_TO` | Where to send the brief (optional; defaults to `GMAIL_ADDRESS`; comma-separate multiple) |

If your other agents already use these keys, you can reuse the same values.

### Step 5: Let the agent save its history
1. **Settings** → **Actions** → **General**.
2. Scroll to **Workflow permissions** → select **Read and write permissions** → **Save**.

### Step 6: Test it
1. Click the **Actions** tab → **Job Search Agent** (left side) → **Run workflow**.
2. Tick **Skip web search** and **Dry run** → **Run workflow**. This cheap test checks the job boards and the scoring.
3. When it turns green, open `reports/` in the repo to read the brief.
4. Run it again with both boxes **unticked**. You should get the full email within about 5 minutes.

After that it runs on its own every Tuesday and Friday.

---

## Customizing

- **Companies**: edit `config/watchlist.yaml`. If you know a company's job board (for example `boards.greenhouse.io/acme`), add `ats: greenhouse` and `slug: acme` so it skips guessing.
- **Roles, locations, salary floor**: edit `config/profile.yaml`.
- **Voice**: drop 2–3 of your best recent LinkedIn posts into `profile/writing_samples/` as `.md` files. The one sample included is an older Perficient blog post, so the voice will sharpen a lot once you add these.
- **Resume**: replace `profile/resume.md` whenever you update your resume.
- **Model**: by default it uses `claude-sonnet-5-5`. To change it, go to **Settings** → **Secrets and variables** → **Actions** → **Variables** tab → **New repository variable**, name it `CLAUDE_MODEL`, and enter the model ID.
- **Schedule**: edit the `cron` line in the workflow. GitHub uses UTC, so `0 14 * * 2,5` is 7am PDT and 6am PST.

## Run it locally (optional)
```bash
pip install -r requirements.txt pytest
python -m pytest tests                      # offline tests, no API key needed
export ANTHROPIC_API_KEY=...                # then:
python src/agent.py --dry-run               # writes reports/<date>.md, no email
```
