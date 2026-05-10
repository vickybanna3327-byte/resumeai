# ResumeAI

An autonomous job application agent that searches Indeed Canada and LinkedIn, tailors ATS-optimised resumes using Claude AI, and applies to jobs automatically — all from a single Streamlit interface.

## What it does

- **Profile Manager** — store your personal info, work experience, education, skills, projects, and certifications in one place
- **Resume Tailor** — sends your profile + a job description to Claude (claude-sonnet-4-6) and receives a fully ATS-safe resume with a match score (0–100), exported as `.docx` and `.pdf`
- **Job Searcher** — scrapes Indeed Canada and LinkedIn using Playwright with human-like behaviour (random delays, stealth JS, session caching) to avoid bot detection
- **Job Applicator** — fills Indeed Easy Apply and LinkedIn Easy Apply forms automatically using Playwright; supports `dry_run` mode that stops before Submit
- **Auto-Apply Pipeline** — after a search, fetches full job descriptions, scores each one with Claude, and applies to any job scoring ≥ your threshold (default 70); live progress displayed in the UI
- **Dashboard** — application history, outcome tracking, and charts

## Tech Stack

| Layer | Technology |
|---|---|
| UI | Streamlit |
| AI / LLM | Anthropic Claude (claude-sonnet-4-6) |
| Browser automation | Playwright (async) |
| Resume generation | python-docx, reportlab |
| Database | SQLite (built-in) |
| Data manipulation | Pandas |
| Language | Python 3.10+ |

## Project Structure

```
resumeai/
├── app.py                  # Streamlit app — 4-tab UI
├── requirements.txt
├── modules/
│   ├── profile_manager.py  # CRUD over data/profile.json
│   ├── resume_tailor.py    # Claude AI resume tailoring + scoring
│   ├── job_searcher.py     # Indeed + LinkedIn Playwright scraper
│   ├── job_applicator.py   # Form automation + Easy Apply
│   └── database.py         # SQLite init + migrations
├── templates/
│   ├── resume_prompt.txt
│   └── cover_letter_prompt.txt
├── data/                   # Created at runtime — gitignored
│   ├── profile.json        # Your profile (gitignored)
│   ├── settings.json       # App settings (gitignored)
│   ├── sessions/           # Browser auth state (gitignored)
│   ├── resumes/            # Generated resumes (gitignored)
│   └── resumeai.db         # SQLite database (gitignored)
└── tests/
```

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/vickybanna3327-byte/resumeai.git
cd resumeai
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Create your profile

Run the app and fill in the **Profile** tab, or create `data/profile.json` manually following the structure in `modules/profile_manager.py`.

### 4. Add your Anthropic API key

Open the **Settings** tab in the app and paste your key — it is stored locally in `data/settings.json` (gitignored).

Alternatively, create a `.env` file:

```
ANTHROPIC_API_KEY=sk-ant-...
```

### 5. Run

```bash
streamlit run app.py
```

Open `http://localhost:8501` in your browser.

## Usage

1. **Profile tab** — fill in all sections; the sidebar shows a completeness percentage
2. **Search Jobs tab** — enter a job title and location, choose sources, click Search
3. **Auto-Apply** — after searching, enable Auto-Apply, set your minimum match score, and click Start — the pipeline runs fully automatically
4. **Dashboard tab** — review all applications, outcomes, and match scores

## Configuration

All settings are managed from the **Settings** tab inside the app:

| Setting | Default | Description |
|---|---|---|
| Dry Run | On | Fill forms but stop before Submit |
| Min Match Score | 70 | Auto-apply only to jobs above this score |
| Max Apps / Day | 10 | Daily application cap |
| Search Pages | 3 | Pages per source per search |
| Expected Salary | — | Auto-filled in salary fields |
| Notice Period | — | Auto-filled in availability fields |

## Important Notes

- Always test with **Dry Run enabled** before turning on live applications
- `data/profile.json` and `data/settings.json` are gitignored — they never leave your machine
- Browser sessions are cached in `data/sessions/` so you only need to log in once per platform
- The app respects the daily cap to avoid triggering spam detection on job boards

## License

MIT
