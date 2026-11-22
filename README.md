# LEA M&A Sourcing Tool
 
Built an investment intelligence platform during my internship at **Legal Enterprises of America** (Private Equity firm in NY) to identify, score, and track acquisition targets in the personal injury law firm market.
 
**Demo:** [lea-production.up.railway.app](https://lea-production.up.railway.app)
 
---
 
## Goal
 
LEA's investment team needed a faster way to source law firms for acquisition. The manual process involved Googling firms city by city, checking Google reviews, researching websites, and drafting outreach emails. It was slow and inconsistent. The goal was to build a single tool that could:
 
1. Discover law firms in any US city in seconds
2. Surface Google ratings and review counts automatically
3. Analyse firm websites with AI to find personalisation hooks
4. Score firms on acquisition readiness (0–100)
5. Push firms directly into a CRM (Google Sheets + Monday.com)
6. Draft personalised outreach emails with one click
---
 
## Architecture
 
```
sourcing-tool/
├── backend/
│   ├── main.py          # FastAPI async API server
│   ├── scraper.py       # Firm discovery + data enrichment
│   ├── scorer.py        # Investment scoring engine (0–100)
│   ├── ai_engine.py     # Groq LLM website analysis
│   └── frontend/
│       └── index.html   # Single-file React 18 app (Babel CDN, no build step)
└── frontend/
    └── index.html       # Mirror of backend/frontend/index.html (served by Railway)
```
 
**Stack:**
| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, asyncio |
| Database | SQLite (WAL mode) with 7-day search cache |
| Frontend | React 18 in-browser Babel (single HTML file, zero build tooling) |
| Firm discovery | SerpAPI (Google Maps engine) |
| AI analysis | Groq API (free tier, Llama 3) |
| CRM | Google Sheets API + Monday.com GraphQL API |
| Hosting | Railway (~$5/month) |
 
---
 
## Development Journey
 
### Iteration 1 — Basic Discovery
The first version searched for firms using Yelp ratings and displayed a simple list. It could find firms by city but lacked Google data, scoring, or any AI features. Rating data was often missing or inaccurate.
 
The main issue was that Yelp data is sparse for law firms. Most firms only appear on Google, so ratings were often missing entirely.
 
### Iteration 2 — Google Integration via SerpAPI
Replaced Yelp with SerpAPI's Google Maps engine, which immediately provided real Google star ratings and review counts for the majority of firms. Added a 7-day SQLite cache to avoid re-hitting the API for repeated searches.
 
A tricky bug came up where the deduplication logic was keeping the wrong version of a firm, specifically the unrated LEA-research version instead of the rated SerpAPI version. Fixed it by ensuring SerpAPI results are added to the results list before LEA research firms, so deduplication always keeps the rated entry.
 
### Iteration 3 — AI Website Analysis
Added a Groq-powered website scraper (`ai_engine.py`) that:
- Auto-discovers the firm's website URL
- Scrapes multiple pages (homepage, about, attorney roster, practice areas)
- Extracts: unique differentiators, notable case results, community focus, acquisition signals, attorney names and count
- Returns structured JSON used to populate the AI Overview card
One significant constraint I ran into was that Babel CDN (used to avoid a build step) cannot parse multi-line backtick template literals or immediately-invoked function expressions (IIFEs) inside JSX. All JSX had to use string concatenation and named helper components. This caused several blank-page incidents traced back to this parser limitation, and was an important architectural lesson for single-file React apps.
 
### Iteration 4 — Investment Scoring
Built a 0–100 scoring engine (`scorer.py`) with five sub-dimensions:
- **Client Volume** — Google review count as a proxy for case load
- **Reputation** — Google star rating weighted by review count
- **Scale** — Attorney count from website scrape and CourtListener dockets
- **Market Activity** — Federal case filing frequency
- **Acquisition Readiness** — Signals like single-founder structure, succession gaps, partner age indicators
Each firm gets a score, letter grade (A–F), and recommended action (Invest Now / Strong Consider / Worth Outreach / Monitor / Pass).
 
### Iteration 5 — CRM Integration
Two-way CRM sync:
- **Google Sheets** — "Link to Sheet" appends firm data to a shared tracking spreadsheet via the Google Sheets API
- **Monday.com** — "Add to Monday" creates a new item in the Active Contacts board via Monday's GraphQL API, pre-filling Owner, Date Contacted, and a Comments block with rating, score, and AI insights
### Iteration 6 — UI Polish & Production Hardening
- Merged Investment Score + AI Insights into a single **AI Overview** card (white background, score circle, 4 bullet points — no thesis text, no sub-score bars, no strengths/risks grid)
- Added a verified facts strip (Google rating badge, attorney count, federal cases) — only shows data confirmed from real sources, eliminating hallucination risk
- **Draft Email** button opens Mac Mail with the LEA pitch template pre-filled; the personalisation block is auto-populated with AI findings formatted as a numbered list in square brackets, flagging it for human review before sending
- Compressed user list to Admin and LEA Investor only
- Embedded Google Sheet in the Manage tab for quick CRM access without leaving the tool
---
 
## Key Features
 
### Firm Discovery
Search any US city and practice area combination. Returns up to 20 firms with Google star ratings, review counts, and geographic data. Results persist in localStorage so they survive tab closes and browser refreshes.
 
### AI Overview Card
One click scrapes the firm's website (multi-page) and returns:
- **Verified facts strip:** Google rating, attorney count from website, federal case count — zero hallucination risk
- **Up to 4 AI bullets:** unique differentiators → notable results → community focus → acquisition signals (generic outreach hooks excluded)
- **Investment score (0–100)** with letter grade and recommended action
### Draft Email
Clicking **✉ Draft Email** opens Mac Mail with the full LEA pitch template. The personalisation block is replaced with a numbered list of real facts found about the firm, inside square brackets to signal the sender to review before hitting send.
 
### Monday.com CRM
The **Add to Monday** button pushes the firm into the LEA "Firms" board (Active Contacts group) with firm name, the logged-in user as Owner, today's date, and a Comments block containing location, Google rating, LEA score, and AI insight bullets.
 
---
 
## Environment Variables
 
| Variable | Purpose |
|---|---|
| `SERP_API_KEY` | Google Maps firm discovery (SerpAPI) |
| `GROQ_API_KEY` | AI website analysis (free at console.groq.com) |
| `MONDAY_API_TOKEN` | Monday.com CRM integration |
| `GOOGLE_CREDENTIALS_JSON` | Google Sheets service account (paste full JSON) |
| `GOOGLE_SHEET_ID` | Target spreadsheet ID |
| `HUNTER_API_KEY` | Contact email finder |
| `NEWS_API_KEY` | Press mention tracking |
 
---
 
## Deployment
 
Hosted on Railway with the backend as the root service. See `DEPLOY.md` for full setup instructions including Google Sheets credentials, Railway volumes for SQLite persistence, and optional custom domain setup.
 
```bash
# Local development
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
# Open http://localhost:8000
```
 
---
 
## Results
 
- **Discovery speed:** 20 firms with Google ratings in ~8 seconds (down from 33s after async optimisation and caching)
- **AI analysis:** Multi-page website scrape + LLM analysis in ~15 seconds per firm
- **Email drafting:** From 10+ minutes of manual research to one click
- **CRM sync:** Firms pushed to Google Sheets and Monday.com simultaneously with score and AI context attached
---
 
*Built by Aryaman Arora — LEA Investment Intelligence Internship*
 
