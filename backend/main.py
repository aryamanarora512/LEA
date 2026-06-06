"""
LEA M&A Sourcing Tool — FastAPI Backend
Run: uvicorn main:app --reload --port 8000
"""

from __future__ import annotations
import os, json, logging, asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import httpx

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from scraper import (research_firm, SCRAPER_REGISTRY, SCRAPER_LABELS, discover_firms,
                     scrape_firm_leadership, find_contact_email, generate_outreach_email)
from scorer import (
    compute_full_score,
    PracticeAreaData, BrandData, MarketData,
    FinancialData, GrowthData, MAReadinessData,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lea.api")

# ──────────────────────────────────────────────
# Database setup (SQLite)
# ──────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "lea.db"


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS firms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        city TEXT NOT NULL,
        state TEXT NOT NULL,
        website TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        founder_name TEXT DEFAULT '',
        firm_founded_year INTEGER,
        attorney_count INTEGER DEFAULT 0,
        office_count INTEGER DEFAULT 1,

        -- Scores
        composite_score REAL DEFAULT 0,
        practice_fit_score REAL DEFAULT 0,
        brand_quality_score REAL DEFAULT 0,
        market_position_score REAL DEFAULT 0,
        financial_health_score REAL DEFAULT 0,
        growth_momentum_score REAL DEFAULT 0,
        ma_readiness_score REAL DEFAULT 0,

        investment_tier TEXT DEFAULT 'Not Scored',
        signal TEXT DEFAULT 'pending',
        why_highlights TEXT DEFAULT '[]',  -- JSON array

        -- Raw scraped data (JSON blob)
        scraped_data TEXT DEFAULT '{}',

        -- CRM fields
        pipeline_stage TEXT DEFAULT 'Sourced',
        assigned_analyst TEXT DEFAULT '',
        outreach_sent INTEGER DEFAULT 0,
        outreach_replied INTEGER DEFAULT 0,
        notes TEXT DEFAULT '',

        score_last_updated TEXT,
        created_at TEXT DEFAULT (datetime('now')),

        -- Key scraped signals (stored flat for fast queries)
        google_stars REAL DEFAULT 0,
        google_review_count INTEGER DEFAULT 0,
        avvo_rating REAL DEFAULT 0,
        bar_complaint_count INTEGER DEFAULT 0,
        courtlistener_case_count INTEGER DEFAULT 0,
        bar_admission_year INTEGER,
        has_junior_partners INTEGER DEFAULT 0,
        website_last_updated_year INTEGER,
        contingency_pct REAL DEFAULT 0,
        practice_focus_pct REAL DEFAULT 0,
        crm TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS research_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        firm_id INTEGER REFERENCES firms(id),
        status TEXT DEFAULT 'pending',
        started_at TEXT,
        completed_at TEXT,
        error TEXT
    );

    CREATE TABLE IF NOT EXISTS scrape_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        firm_id INTEGER NOT NULL REFERENCES firms(id),
        source_key TEXT NOT NULL,
        status TEXT DEFAULT 'running',
        result_json TEXT,
        error TEXT,
        started_at TEXT DEFAULT (datetime('now')),
        completed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS user_watchlists (
        username     TEXT NOT NULL,
        firm_key     TEXT NOT NULL,
        firm_data    TEXT NOT NULL,
        saved_at     TEXT DEFAULT (datetime('now')),
        PRIMARY KEY  (username, firm_key)
    );

    CREATE TABLE IF NOT EXISTS user_crm (
        username     TEXT NOT NULL,
        firm_key     TEXT NOT NULL,
        crm_data     TEXT NOT NULL,
        updated_at   TEXT DEFAULT (datetime('now')),
        PRIMARY KEY  (username, firm_key)
    );

    CREATE TABLE IF NOT EXISTS sheet_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        username     TEXT,
        firm_name    TEXT,
        firm_key     TEXT,
        sheet_row    INTEGER,
        linked_at    TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS search_cache (
        cache_key    TEXT PRIMARY KEY,
        results_json TEXT NOT NULL,
        created_at   TEXT DEFAULT (datetime('now')),
        hit_count    INTEGER DEFAULT 0
    );
    """)
    conn.commit()
    conn.close()



@asynccontextmanager
async def lifespan(app):
    init_db()
    log.info("LEA Sourcing Tool ready.")
    yield

app = FastAPI(title="LEA M&A Sourcing Tool", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Frontend: check same-dir frontend/ first (Railway), then parent dir (local dev)
_fe_same = Path(__file__).parent / "frontend" / "index.html"
_fe_parent = Path(__file__).parent.parent / "frontend" / "index.html"
FRONTEND_PATH = _fe_same if _fe_same.exists() else _fe_parent


# ──────────────────────────────────────────────
# Pydantic models
# ──────────────────────────────────────────────

class FirmCreate(BaseModel):
    name: str
    city: str
    state: str
    website: str = ""
    founder_name: str = ""
    firm_founded_year: Optional[int] = None
    attorney_count: int = 0
    office_count: int = 1
    notes: str = ""


class FirmUpdate(BaseModel):
    pipeline_stage: Optional[str] = None
    assigned_analyst: Optional[str] = None
    outreach_sent: Optional[int] = None
    outreach_replied: Optional[int] = None
    notes: Optional[str] = None


# ──────────────────────────────────────────────
# API Routes
# ──────────────────────────────────────────────

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # Return a minimal 1x1 transparent ICO so the browser stops complaining
    return Response(content=b"", media_type="image/x-icon")


@app.get("/")
def serve_frontend():
    if FRONTEND_PATH.exists():
        return FileResponse(FRONTEND_PATH)
    return HTMLResponse(
        "<h1>LEA Sourcing Tool</h1>"
        "<p>Frontend not found. Make sure <code>frontend/index.html</code> exists.</p>"
        f"<p>Looking in: {FRONTEND_PATH}</p>"
    )


@app.get("/api/stats")
def get_stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM firms").fetchone()[0]
    strong_buy = conn.execute("SELECT COUNT(*) FROM firms WHERE signal='strong_buy'").fetchone()[0]
    avg_ma_score = conn.execute("SELECT AVG(ma_readiness_score) FROM firms").fetchone()[0] or 0
    outreach_sent = conn.execute("SELECT SUM(outreach_sent) FROM firms").fetchone()[0] or 0
    outreach_replied = conn.execute("SELECT SUM(outreach_replied) FROM firms").fetchone()[0] or 0
    this_week = conn.execute(
        "SELECT COUNT(*) FROM firms WHERE created_at >= datetime('now', '-7 days')"
    ).fetchone()[0]
    pi_firms = conn.execute(
        "SELECT COUNT(*) FROM firms WHERE practice_fit_score >= 75"
    ).fetchone()[0]
    conn.close()
    return {
        "acquisition_targets": total,
        "targets_this_week": this_week,
        "avg_ma_readiness_score": round(avg_ma_score, 0),
        "high_fit_pi_firms": pi_firms,
        "high_fit_new": max(0, pi_firms - 30),
        "outreach_sent": outreach_sent,
        "outreach_replied": outreach_replied,
    }


@app.get("/api/firms")
def list_firms(
    sort: str = "composite_score",
    order: str = "desc",
    stage: str = "",
    min_score: float = 0,
    state: str = "",
    limit: int = 50,
):
    valid_sorts = {
        "composite_score", "practice_fit_score", "brand_quality_score",
        "market_position_score", "financial_health_score", "growth_momentum_score",
        "ma_readiness_score", "name", "created_at",
    }
    sort_col = sort if sort in valid_sorts else "composite_score"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"

    where_clauses = ["1=1"]
    params = []
    if stage:
        where_clauses.append("pipeline_stage = ?")
        params.append(stage)
    if min_score > 0:
        where_clauses.append("composite_score >= ?")
        params.append(min_score)
    if state:
        where_clauses.append("state = ?")
        params.append(state.upper())

    where = " AND ".join(where_clauses)

    conn = get_db()
    rows = conn.execute(f"""
        SELECT id, name, city, state, website, attorney_count, office_count,
               composite_score, practice_fit_score, brand_quality_score,
               market_position_score, financial_health_score, growth_momentum_score,
               ma_readiness_score, investment_tier, signal,
               google_stars, google_review_count, avvo_rating, bar_admission_year,
               website_last_updated_year, contingency_pct, practice_focus_pct,
               crm, courtlistener_case_count, pipeline_stage,
               outreach_sent, outreach_replied, score_last_updated, created_at
        FROM firms
        WHERE {where}
        ORDER BY {sort_col} {order_dir}
        LIMIT ?
    """, params + [limit]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/firms/{firm_id}")
def get_firm(firm_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM firms WHERE id=?", (firm_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Firm not found")
    d = dict(row)
    # Parse JSON fields
    d["why_highlights"] = json.loads(d.get("why_highlights") or "[]")
    d["scraped_data"] = json.loads(d.get("scraped_data") or "{}")
    return d


@app.post("/api/firms", status_code=201)
def create_firm(firm: FirmCreate):
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO firms (name, city, state, website, founder_name,
                           firm_founded_year, attorney_count, office_count, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (firm.name, firm.city, firm.state, firm.website, firm.founder_name,
          firm.firm_founded_year, firm.attorney_count, firm.office_count, firm.notes))
    firm_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": firm_id, "message": f"Firm '{firm.name}' created. Trigger /api/research/{firm_id} to score."}


@app.patch("/api/firms/{firm_id}")
def update_firm(firm_id: int, update: FirmUpdate):
    conn = get_db()
    row = conn.execute("SELECT id FROM firms WHERE id=?", (firm_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Firm not found")
    updates = {k: v for k, v in update.dict().items() if v is not None}
    if updates:
        sets = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE firms SET {sets} WHERE id=?", list(updates.values()) + [firm_id])
        conn.commit()
    conn.close()
    return {"message": "Updated"}


@app.delete("/api/firms/{firm_id}")
def delete_firm(firm_id: int):
    conn = get_db()
    conn.execute("DELETE FROM firms WHERE id=?", (firm_id,))
    conn.commit()
    conn.close()
    return {"message": "Deleted"}


@app.post("/api/research/{firm_id}")
async def trigger_research(firm_id: int, background_tasks: BackgroundTasks):
    """Kick off async scraping + scoring for a firm."""
    conn = get_db()
    row = conn.execute("SELECT * FROM firms WHERE id=?", (firm_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Firm not found")

    firm = dict(row)
    background_tasks.add_task(_run_research, firm_id, firm)
    return {"message": f"Research started for {firm['name']}. Check /api/firms/{firm_id} for updates."}


async def _run_research(firm_id: int, firm: dict):
    log.info(f"Starting research for firm_id={firm_id}: {firm['name']}")

    # Update job status
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO research_jobs (firm_id, status, started_at)
        VALUES (?, 'running', datetime('now'))
    """, (firm_id,))
    conn.commit()
    conn.close()

    try:
        # Scrape all sources
        scraped = await research_firm(
            firm_name=firm["name"],
            city=firm["city"],
            state=firm["state"],
            website=firm.get("website", ""),
            founder_name=firm.get("founder_name", ""),
        )

        # Build scorer inputs from scraped data
        practice_areas = scraped.get("detected_practice_areas", [])
        practice_data = PracticeAreaData(areas=practice_areas if practice_areas else [])

        brand_data = BrandData(
            google_stars=scraped.get("google_stars", 0.0),
            google_review_count=scraped.get("google_review_count", 0),
            avvo_rating=scraped.get("avvo_rating", 0.0),
            nlp_sentiment_score=scraped.get("nlp_sentiment_score", 50.0),
            response_rate=scraped.get("response_rate", 0.0),
            google_ads_active=scraped.get("google_ads_active", False),
            tv_advertising=scraped.get("tv_advertising", False),
        )

        # Use existing market data from DB or defaults
        market_data = MarketData(
            city=firm["city"],
            state=firm["state"],
            google_rank_primary_kw=scraped.get("google_rank_primary_kw", 0),
        )

        financial_data = FinancialData(
            contingency_pct=scraped.get("contingency_pct", 0.0),
            legal_tech_crm=scraped.get("crm", ""),
        )

        growth_data = GrowthData(
            google_ads_active=scraped.get("google_ads_active", False),
            crm_recently_adopted=bool(scraped.get("crm", "")),
        )

        ma_data = MAReadinessData(
            bar_admission_year=scraped.get("bar_admission_year"),
            has_junior_partners=scraped.get("has_junior_partners", False),
            website_last_updated_year=scraped.get("website_last_updated_year"),
            attorney_count=scraped.get("attorney_count", firm.get("attorney_count", 0)),
            office_count=firm.get("office_count", 1),
            firm_founded_year=firm.get("firm_founded_year"),
        )

        # Compute scores
        result = compute_full_score(practice_data, brand_data, market_data, financial_data, growth_data, ma_data)
        sub = result["sub_scores"]

        # Update DB
        conn = get_db()
        conn.execute("""
            UPDATE firms SET
                composite_score=?, practice_fit_score=?, brand_quality_score=?,
                market_position_score=?, financial_health_score=?, growth_momentum_score=?,
                ma_readiness_score=?, investment_tier=?, signal=?, why_highlights=?,
                google_stars=?, google_review_count=?, avvo_rating=?,
                bar_admission_year=?, has_junior_partners=?,
                website_last_updated_year=?, contingency_pct=?,
                practice_focus_pct=?, crm=?, courtlistener_case_count=?,
                website=?, scraped_data=?, score_last_updated=datetime('now')
            WHERE id=?
        """, (
            result["composite"], sub["practice_fit"], sub["brand_quality"],
            sub["market_position"], sub["financial_health"], sub["growth_momentum"],
            sub["ma_readiness"], result["investment_tier"], result["signal"],
            json.dumps(result["why_highlights"]),
            scraped.get("google_stars", 0), scraped.get("google_review_count", 0),
            scraped.get("avvo_rating", 0),
            scraped.get("bar_admission_year"), int(scraped.get("has_junior_partners", False)),
            scraped.get("website_last_updated_year"), scraped.get("contingency_pct", 0),
            scraped.get("practice_focus_pct", 0), scraped.get("crm", ""),
            scraped.get("courtlistener_case_count", 0),
            scraped.get("website", firm.get("website", "")),
            json.dumps(scraped), firm_id,
        ))
        conn.execute("""
            UPDATE research_jobs SET status='completed', completed_at=datetime('now')
            WHERE id=(SELECT id FROM research_jobs WHERE firm_id=? ORDER BY id DESC LIMIT 1)
        """, (firm_id,))
        conn.commit()
        conn.close()
        log.info(f"Research complete for {firm['name']}: score={result['composite']}")

    except Exception as e:
        log.error(f"Research failed for firm_id={firm_id}: {e}", exc_info=True)
        conn = get_db()
        conn.execute("""
            UPDATE research_jobs SET status='failed', completed_at=datetime('now'), error=?
            WHERE id=(SELECT id FROM research_jobs WHERE firm_id=? ORDER BY id DESC LIMIT 1)
        """, (str(e), firm_id))
        conn.commit()
        conn.close()


@app.get("/api/research/{firm_id}/status")
def research_status(firm_id: int):
    conn = get_db()
    job = conn.execute(
        "SELECT * FROM research_jobs WHERE firm_id=? ORDER BY id DESC LIMIT 1", (firm_id,)
    ).fetchone()
    conn.close()
    if not job:
        return {"status": "no_job"}
    return dict(job)


@app.get("/api/score-distribution")
def score_distribution():
    """Return histogram data for the score distribution chart."""
    conn = get_db()
    rows = conn.execute("SELECT composite_score FROM firms").fetchall()
    conn.close()

    buckets = [0] * 10  # 0-9, 10-19, ..., 90-100
    for row in rows:
        score = int(row[0])
        bucket = min(score // 10, 9)
        buckets[bucket] += 1

    return [
        {"range": f"{i*10}–{i*10+9}", "count": buckets[i], "min": i*10}
        for i in range(10)
    ]




# ─────────────────────────────────────────────────────────────
# PER-SOURCE SCRAPING — new endpoints for Data Sources panel
# ─────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    firm_id: int
    source_key: str


@app.get("/api/scrapers")
def list_scrapers():
    """Return all available per-source scrapers with metadata."""
    return [
        {"key": k, "source": v[0], "variable": v[1], "frequency": v[2]}
        for k, v in SCRAPER_LABELS.items()
    ]


@app.post("/api/scrape")
async def trigger_single_scrape(req: ScrapeRequest, background_tasks: BackgroundTasks):
    """Trigger a single source scrape for a firm. Returns job_id for polling."""
    conn = get_db()
    row = conn.execute("SELECT name, city, state, website, founder_name FROM firms WHERE id=?",
                       (req.firm_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Firm not found")
    if req.source_key not in SCRAPER_REGISTRY:
        raise HTTPException(400, f"Unknown source key: {req.source_key}")

    firm = dict(row)
    # Create job record
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO scrape_jobs (firm_id, source_key, status) VALUES (?, ?, 'running')",
        (req.firm_id, req.source_key)
    )
    job_id = cur.lastrowid
    conn.commit()
    conn.close()

    background_tasks.add_task(
        _run_single_scrape, job_id, req.firm_id, req.source_key,
        firm["name"], firm.get("city", ""), firm.get("state", ""),
        firm.get("website", ""), firm.get("founder_name", "")
    )
    return {"job_id": job_id, "status": "running"}


@app.post("/api/scrape_all")
async def trigger_all_scrapes(req: ScrapeRequest, background_tasks: BackgroundTasks):
    """Trigger all scrapers for a firm at once."""
    conn = get_db()
    row = conn.execute("SELECT name, city, state, website, founder_name FROM firms WHERE id=?",
                       (req.firm_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Firm not found")

    firm = dict(row)
    job_ids = []
    conn = get_db()
    for key in SCRAPER_REGISTRY:
        cur = conn.execute(
            "INSERT INTO scrape_jobs (firm_id, source_key, status) VALUES (?, ?, 'running')",
            (req.firm_id, key)
        )
        job_ids.append({"key": key, "job_id": cur.lastrowid})
    conn.commit()
    conn.close()

    for item in job_ids:
        background_tasks.add_task(
            _run_single_scrape, item["job_id"], req.firm_id, item["key"],
            firm["name"], firm.get("city", ""), firm.get("state", ""),
            firm.get("website", ""), firm.get("founder_name", "")
        )

    return {"jobs": job_ids, "count": len(job_ids)}


async def _run_single_scrape(job_id: int, firm_id: int, source_key: str,
                              firm_name: str, city: str, state: str,
                              website: str, founder_name: str):
    """Background task: run one scraper, store result in scrape_jobs."""
    try:
        fn = SCRAPER_REGISTRY[source_key]
        result = await fn(firm_name, city, state, website, founder_name)
        conn = get_db()
        conn.execute(
            "UPDATE scrape_jobs SET status='done', result_json=?, completed_at=datetime('now') WHERE id=?",
            (json.dumps(result, default=str), job_id)
        )
        conn.commit()
        conn.close()
        log.info(f"Scrape job {job_id} ({source_key}) done for '{firm_name}'")
    except Exception as e:
        log.error(f"Scrape job {job_id} ({source_key}) failed: {e}", exc_info=True)
        conn = get_db()
        conn.execute(
            "UPDATE scrape_jobs SET status='error', error=?, completed_at=datetime('now') WHERE id=?",
            (str(e), job_id)
        )
        conn.commit()
        conn.close()


@app.get("/api/scrape_jobs/{job_id}")
def get_scrape_job(job_id: int):
    """Poll a scrape job's status and result."""
    conn = get_db()
    row = conn.execute("SELECT * FROM scrape_jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Job not found")
    d = dict(row)
    if d.get("result_json"):
        d["result"] = json.loads(d["result_json"])
    del d["result_json"]
    return d


@app.get("/api/firms/{firm_id}/scrape_results")
def get_firm_scrape_results(firm_id: int):
    """Return most recent scrape result per source for a firm."""
    conn = get_db()
    # Get latest completed result per source_key
    rows = conn.execute("""
        SELECT source_key, status, result_json, error, completed_at
        FROM scrape_jobs
        WHERE firm_id = ? AND status IN ('done', 'error')
        GROUP BY source_key
        HAVING MAX(id)
        ORDER BY completed_at DESC
    """, (firm_id,)).fetchall()
    conn.close()
    results = []
    for r in rows:
        d = dict(r)
        if d.get("result_json"):
            d["result"] = json.loads(d["result_json"])
        del d["result_json"]
        results.append(d)
    return results



# ─────────────────────────────────────────────────────────────
# FIRM DISCOVERY — /api/discover
# ─────────────────────────────────────────────────────────────

class DiscoverRequest(BaseModel):
    city: str
    state: str
    practice_area: str = "personal injury"
    max_results: int = 20


@app.post("/api/discover")
async def discover_endpoint(req: DiscoverRequest):
    """
    Discover new law firm acquisition candidates in a given market.
    Sources: Google Places (if key set), SerpAPI (if key set), Avvo, Martindale.
    Cross-checks results against existing firms in DB and flags duplicates.
    Results are cached for 7 days — repeat searches are instant.
    """
    import json as _json
    import hashlib

    # Build cache key from search params
    cache_key = hashlib.md5(
        f"{req.city.strip().lower()}|{req.state.strip().lower()}|{req.practice_area.strip().lower()}|{req.max_results}".encode()
    ).hexdigest()

    CACHE_TTL_HOURS = 7 * 24  # 7 days

    # Check cache first
    conn = get_db()
    cached = conn.execute(
        "SELECT results_json, created_at FROM search_cache WHERE cache_key=? AND created_at > datetime('now', ?)",
        (cache_key, f"-{CACHE_TTL_HOURS} hours")
    ).fetchone()

    if cached:
        # Cache hit — update hit count and return instantly
        conn.execute("UPDATE search_cache SET hit_count = hit_count + 1 WHERE cache_key=?", (cache_key,))
        conn.commit()
        result = _json.loads(cached["results_json"])
        result["cached"] = True
        # Still flag pipeline status (this is live data)
        existing_names = set(
            row[0].lower() for row in conn.execute("SELECT name FROM firms").fetchall()
        )
        conn.close()
        for firm in result["results"]:
            norm = firm["name"].lower()
            firm["already_in_pipeline"] = any(norm in ex or ex in norm for ex in existing_names)
        return result
    conn.close()

    # Cache miss — call the real API
    result = await discover_firms(
        city=req.city,
        state=req.state,
        practice_area=req.practice_area,
        max_results=req.max_results,
    )

    # Flag firms already in the pipeline
    conn = get_db()
    existing_names = set(
        row[0].lower() for row in conn.execute("SELECT name FROM firms").fetchall()
    )

    for firm in result["results"]:
        norm = firm["name"].lower()
        firm["already_in_pipeline"] = any(
            norm in ex or ex in norm for ex in existing_names
        )

    # Store in cache (only cache successful results with firms)
    if result.get("results"):
        result["cached"] = False
        conn.execute(
            "INSERT OR REPLACE INTO search_cache (cache_key, results_json, created_at, hit_count) VALUES (?,?,datetime('now'),0)",
            (cache_key, _json.dumps(result))
        )
    conn.commit()
    conn.close()

    return result




@app.get("/api/cache/stats")
async def cache_stats():
    """Return search cache statistics."""
    import json as _json
    conn = get_db()
    rows = conn.execute(
        "SELECT cache_key, created_at, hit_count FROM search_cache ORDER BY hit_count DESC"
    ).fetchall()
    total = len(rows)
    total_hits = sum(r["hit_count"] for r in rows)
    conn.close()
    return {"total_cached_searches": total, "total_cache_hits": total_hits,
            "ttl_days": 7, "note": "Cache saves ~3s per repeat search"}

@app.delete("/api/cache/clear")
async def clear_cache():
    """Clear all cached search results (admin use)."""
    conn = get_db()
    conn.execute("DELETE FROM search_cache")
    conn.commit()
    conn.close()
    return {"ok": True, "message": "Search cache cleared"}

# ─────────────────────────────────────────────────────────────
# OUTREACH ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.get("/api/firms/{firm_id}/contact")
async def get_firm_contact(firm_id: int):
    """
    Identify the managing partner / founder and find their email.
    Uses firm website scraping + Hunter.io (if key set) + pattern guessing.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT name, city, state, website, founder_name FROM firms WHERE id=?", (firm_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Firm not found")

    firm = dict(row)
    website = firm.get("website", "")

    # If we already have a founder name stored, use it; else scrape
    stored_founder = firm.get("founder_name", "").strip()
    leadership = {}
    if stored_founder:
        leadership = {"name": stored_founder, "title": "Founder / Managing Partner", "source": "database"}
    else:
        leadership = await scrape_firm_leadership(website, firm["name"])

    # Extract domain from website
    domain = ""
    if website:
        from urllib.parse import urlparse
        parsed = urlparse(website if website.startswith("http") else "https://" + website)
        domain = parsed.netloc.replace("www.", "")

    # Find email
    hunter_key = os.getenv("HUNTER_API_KEY", "")
    contact = {}
    if leadership.get("name") or domain:
        contact = await find_contact_email(
            name=leadership.get("name", ""),
            domain=domain,
            hunter_api_key=hunter_key,
        )
        # If we found an email on the website directly, prefer that
        if leadership.get("email"):
            contact["email"] = leadership["email"]
            contact["confidence"] = 95
            contact["method"] = "website (direct)"

    return {
        "firm_id":    firm_id,
        "firm_name":  firm["name"],
        "website":    website,
        "domain":     domain,
        "leader":     leadership,
        "contact":    contact,
        "hunter_active": bool(hunter_key),
    }


class DraftEmailRequest(BaseModel):
    firm_id: int
    leader_name: str = ""
    tone: str = "professional"   # professional | warm | brief


@app.post("/api/firms/{firm_id}/draft-email")
async def draft_outreach_email(firm_id: int, req: DraftEmailRequest):
    """Generate a personalised outreach email for the firm's managing partner."""
    conn = get_db()
    row = conn.execute("SELECT * FROM firms WHERE id=?", (firm_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Firm not found")
    firm = dict(row)
    leader_name = req.leader_name or firm.get("founder_name", "") or "there"
    draft = generate_outreach_email(firm, leader_name)
    return {"draft": draft, "leader_name": leader_name, "firm_name": firm["name"]}


# ─────────────────────────────────────────────────────────────
# DISCOVER ENRICH — deep profile for a discovered firm
# ─────────────────────────────────────────────────────────────

class EnrichRequest(BaseModel):
    name: str
    website: str = ""
    city: str = ""
    state: str = ""
    practice_area: str = "personal injury"

@app.post("/api/discover/enrich")
async def enrich_discovered_firm(req: EnrichRequest):
    """
    Deep-enrich a discovered firm with:
      - Managing partner / founder (website scrape)
      - LinkedIn employee headcount (Google-search signal)
      - Office locations (website scrape)
      - Federal case count / cases per week (CourtListener)
      - Revenue estimate (attorney count × benchmark)
    All sources run concurrently for speed.
    """
    import asyncio as _aio
    from scraper import (scrape_firm_leadership, find_contact_email,
                         scrape_linkedin_signals, scrape_courtlistener,
                         scrape_firm_website, _estimate_firm_revenue)

    async def safe(coro):
        try:
            return await coro
        except Exception as e:
            log.debug(f"Enrich sub-task failed: {e}")
            return {}

    # Normalise website URL
    real_website = req.website.strip() if req.website else ""
    if real_website and not real_website.startswith("http"):
        real_website = "https://" + real_website

    # Run everything in parallel
    leadership_task     = safe(scrape_firm_leadership(real_website, req.name))
    linkedin_task       = safe(scrape_linkedin_signals(req.name))
    courtlistener_task  = safe(scrape_courtlistener(req.name, req.state))
    website_task        = safe(scrape_firm_website(real_website))
    revenue_task        = safe(_estimate_firm_revenue(req.name, real_website, req.city or "", req.state or ""))

    leadership, linkedin, court, website, revenue = await _aio.gather(
        leadership_task, linkedin_task, courtlistener_task,
        website_task, revenue_task
    )

    # Cases per week estimate (CourtListener gives 90-day count)
    case_count_90d = court.get("case_count_90d") or court.get("docket_count") or 0
    cases_per_week = round(case_count_90d / 13, 1) if case_count_90d else None

    # Office locations from website
    locations = website.get("office_locations") or []
    if req.city and req.state and not locations:
        locations = [f"{req.city}, {req.state}"]

    # Email lookup
    domain = ""
    if req.website:
        from urllib.parse import urlparse
        domain = urlparse(req.website if req.website.startswith("http") else "https://"+req.website).netloc.replace("www.","")

    hunter_key = os.getenv("HUNTER_API_KEY", "")
    contact = {}
    leader_name = leadership.get("name", "")
    if leader_name or domain:
        contact = await safe(find_contact_email(leader_name, domain, hunter_key))
        if leadership.get("email"):
            contact["email"] = leadership["email"]
            contact["confidence"] = 95
            contact["method"] = "website (direct)"

    return {
        "name":          req.name,
        "website":       real_website,
        "city":          req.city,
        "state":         req.state,
        "practice_area": req.practice_area,
        # Leadership
        "leader": {
            "name":        leader_name,
            "title":       leadership.get("title", ""),
            "bio_snippet": leadership.get("bio_snippet", ""),
            "email":       contact.get("email"),
            "email_confidence": contact.get("confidence", 0),
            "email_method":     contact.get("method", ""),
            "email_guesses":    contact.get("guesses", []),
        },
        # Headcount
        "attorney_count_est":  revenue.get("attorney_count_est"),
        "attorney_count_web":  website.get("attorney_count"),
        "linkedin_headcount":       linkedin.get("linkedin_headcount"),
        "linkedin_headcount_range": linkedin.get("linkedin_headcount_range"),
        "linkedin_followers":       linkedin.get("linkedin_followers"),
        "linkedin_industry":        linkedin.get("linkedin_industry"),
        "linkedin_hiring":          linkedin.get("linkedin_hiring", False),
        "linkedin_profile_url":     linkedin.get("linkedin_profile_url"),
        # Financials
        "revenue_label":  revenue.get("revenue_label", "Private (undisclosed)"),
        "revenue_low":    revenue.get("revenue_low"),
        "revenue_high":   revenue.get("revenue_high"),
        "revenue_source": revenue.get("revenue_source", "estimate"),
        # Activity
        "case_count_90d":   case_count_90d,
        "cases_per_week":   cases_per_week,
        "courtlistener_url": court.get("courtlistener_url", ""),
        # Locations
        "office_locations": locations,
        # Website signals
        "practice_areas_detected": website.get("detected_practice_areas", []),
        "has_live_chat":   website.get("has_live_chat", False),
        "crm_detected":    website.get("crm", ""),
        "tech_signals":    website.get("tech_signals", []),
        "website_year":    website.get("last_updated_year"),
    }



# ── Court Analytics endpoint ──────────────────────────────────────────────────

class CourtAnalyticsRequest(BaseModel):
    city: str
    state: str
    practice_area: str = "personal injury"
    days_back: int = 180


@app.post("/api/court-analytics")
async def court_analytics_endpoint(req: CourtAnalyticsRequest):
    """
    Rank law firms in a market by real court filing activity.
    Queries CourtListener federal docket data:
      - Counts unique attorneys per firm (headcount proxy)
      - Counts active cases in the period
      - Calculates case filing velocity (cases/month)
      - Breaks down case types by nature-of-suit
    Returns firms ranked by active case count descending.
    """
    from scraper import analyze_court_data
    try:
        result = await analyze_court_data(
            city=req.city,
            state=req.state,
            practice_area=req.practice_area,
            days_back=req.days_back,
        )
        return result
    except Exception as e:
        log.error(f"Court analytics error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


# ── Monday.com GraphQL API Integration ───────────────────────────────────────

class MondayTokenRequest(BaseModel):
    token: str

class MondayItemsRequest(BaseModel):
    token: str
    board_id: str

MONDAY_API = "https://api.monday.com/v2"

@app.post("/api/monday/boards")
async def monday_get_boards(req: MondayTokenRequest):
    """Fetch all Monday.com boards for the given API token."""
    query = """
    {
      boards(limit: 50, order_by: used_at) {
        id
        name
        description
        items_count
        state
        board_kind
        columns { id title type }
      }
    }
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            MONDAY_API,
            json={"query": query},
            headers={"Authorization": req.token, "Content-Type": "application/json", "API-Version": "2024-01"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Monday.com API error: {resp.text[:300]}")
    data = resp.json()
    if "errors" in data:
        raise HTTPException(status_code=400, detail=data["errors"][0].get("message", "Monday.com error"))
    boards = data.get("data", {}).get("boards", [])
    return {"boards": boards}


@app.post("/api/monday/items")
async def monday_get_items(req: MondayItemsRequest):
    """Fetch all items from a specific Monday.com board."""
    query = f"""
    {{
      boards(ids: [{req.board_id}]) {{
        id
        name
        columns {{ id title type }}
        items_page(limit: 200) {{
          items {{
            id
            name
            state
            created_at
            updated_at
            column_values {{
              id
              text
              value
            }}
          }}
        }}
      }}
    }}
    """
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            MONDAY_API,
            json={"query": query},
            headers={"Authorization": req.token, "Content-Type": "application/json", "API-Version": "2024-01"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Monday.com API error: {resp.text[:300]}")
    data = resp.json()
    if "errors" in data:
        raise HTTPException(status_code=400, detail=data["errors"][0].get("message", "Monday.com error"))
    boards = data.get("data", {}).get("boards", [])
    if not boards:
        raise HTTPException(status_code=404, detail="Board not found")
    return {"board": boards[0]}


@app.post("/api/monday/update_item")
async def monday_update_item(request: Request):
    """Update a column value on a Monday.com item."""
    body = await request.json()
    token     = body.get("token")
    item_id   = body.get("item_id")
    board_id  = body.get("board_id")
    column_id = body.get("column_id")
    value     = body.get("value")   # JSON string per Monday.com column type

    mutation = f"""
    mutation {{
      change_simple_column_value(
        board_id: {board_id},
        item_id: {item_id},
        column_id: "{column_id}",
        value: {json.dumps(str(value))}
      ) {{
        id
      }}
    }}
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            MONDAY_API,
            json={"query": mutation},
            headers={"Authorization": token, "Content-Type": "application/json", "API-Version": "2024-01"},
        )
    data = resp.json()
    if "errors" in data:
        raise HTTPException(status_code=400, detail=data["errors"][0].get("message", "Monday.com mutation error"))
    return {"ok": True}


# ── AI Engine Endpoints ───────────────────────────────────────────────────────

class AIAnalyzeRequest(BaseModel):
    firm_name: str
    city: str
    state: str
    website: str = ""
    google_review_snippets: list = []

class AIOutreachRequest(BaseModel):
    firm_name: str
    city: str
    state: str
    website: str = ""
    contact_name: str = ""
    sender_name: str = "[Your Name]"
    insights: dict = {}

@app.post("/api/ai-analyze")
async def ai_analyze_endpoint(req: AIAnalyzeRequest):
    """
    Scrape the firm website (multi-page) and run LLM analysis via Groq (free).
    Returns structured insights: community focus, differentiators, notable results,
    acquisition signals, and personalised outreach hooks.
    Requires GROQ_API_KEY in .env (free at console.groq.com).
    """
    from ai_engine import analyze_firm, get_ai_backend
    if not get_ai_backend():
        raise HTTPException(
            status_code=400,
            detail="No AI key configured. Add GROQ_API_KEY to .env (free at console.groq.com)"
        )
    try:
        result = await analyze_firm(
            firm_name=req.firm_name,
            city=req.city,
            state=req.state,
            website_url=req.website,
            google_review_snippets=req.google_review_snippets or [],
        )
        return result
    except Exception as e:
        log.error(f"AI analyze error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ai-outreach")
async def ai_outreach_endpoint(req: AIOutreachRequest):
    """
    Generate a personalised outreach email using Groq LLaMA 3.3 70B (free).
    Runs full website analysis first if insights not provided.
    """
    from ai_engine import analyze_firm, generate_outreach_email, get_ai_backend
    if not get_ai_backend():
        raise HTTPException(
            status_code=400,
            detail="No AI key configured. Add GROQ_API_KEY to .env (free at console.groq.com)"
        )
    try:
        insights = req.insights
        if not insights:
            insights = await analyze_firm(
                firm_name=req.firm_name,
                city=req.city,
                state=req.state,
                website_url=req.website,
            )
        email_body = await generate_outreach_email(
            firm_name=req.firm_name,
            city=req.city,
            state=req.state,
            insights=insights,
            contact_name=req.contact_name,
            sender_name=req.sender_name,
        )
        return {"email": email_body, "insights": insights}
    except Exception as e:
        log.error(f"AI outreach error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ai-status")
def ai_status():
    """Check whether the AI engine (Groq / Gemini) is configured and ready."""
    from ai_engine import get_ai_backend
    backend = get_ai_backend()
    model_map = {"groq": "LLaMA 3.3 70B (Groq)", "gemini": "Gemini 1.5 Flash (Google)"}
    return {
        "ready":   bool(backend),
        "backend": backend or "none",
        "model":   model_map.get(backend, "not configured"),
        "message": f"AI engine ready — {model_map.get(backend, '')}" if backend
                   else "Add GROQ_API_KEY to .env (free at console.groq.com)",
    }


@app.get("/api/ratings-status")
def ratings_status():
    """Check which Google ratings source is active."""
    import os
    google_key     = os.getenv("GOOGLE_API_KEY", "")
    serp_key       = os.getenv("SERP_API_KEY", "")
    outscraper_key = os.getenv("OUTSCRAPER_API_KEY", "")
    yelp_key       = os.getenv("YELP_API_KEY", "")

    if google_key:
        return {"source": "Google Places API", "ready": True,
                "label": "Google (Places API)", "is_google": True}
    elif serp_key:
        return {"source": "SerpAPI", "ready": True,
                "label": "Google (via SerpAPI)", "is_google": True}
    elif outscraper_key:
        return {"source": "Outscraper", "ready": True,
                "label": "Google (via Outscraper)", "is_google": True}
    elif yelp_key:
        return {"source": "Yelp", "ready": True,
                "label": "Yelp", "is_google": False,
                "warning": "Using Yelp ratings — add SERP_API_KEY for real Google ratings"}
    else:
        return {"source": "none", "ready": False,
                "label": "None",
                "warning": "No ratings API configured."}


# ── Website Finder Endpoint ───────────────────────────────────────────────────

DIRECTORY_BLACKLIST = {
    "yelp.com","avvo.com","martindale.com","findlaw.com","lawyers.com",
    "justia.com","superlawyers.com","lawinfo.com","hg.org","nolo.com",
    "thumbtack.com","bark.com","expertise.com","legal.io","legalmatch.com",
    "yellowpages.com","whitepages.com","bbb.org","manta.com","chamberofcommerce.com",
    "google.com","facebook.com","instagram.com","twitter.com","x.com",
    "linkedin.com","indeed.com","glassdoor.com","bing.com","yahoo.com",
    "mapquest.com","foursquare.com","angieslist.com","homeadvisor.com",
}

def _is_directory(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        domain = urlparse(url).netloc.lower().lstrip("www.")
        return any(domain == bl or domain.endswith("." + bl) for bl in DIRECTORY_BLACKLIST)
    except Exception:
        return True

class FindWebsiteRequest(BaseModel):
    firm_name: str
    city: str
    state: str

@app.post("/api/find-website")
async def find_firm_website(req: FindWebsiteRequest):
    """
    Searches DuckDuckGo for a law firm's official website.
    Filters out directories (Yelp, Avvo, FindLaw, etc.) and returns
    the most likely official .com domain.
    """
    from urllib.parse import quote_plus, urlparse
    import httpx as _httpx
    from bs4 import BeautifulSoup as _BS

    query = f'"{req.firm_name}" law firm {req.city} {req.state}'
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://duckduckgo.com/",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    candidates = []

    # Stage 1: duckduckgo-search library (fast)
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=10):
                url = r.get("href","")
                if url and not _is_directory(url):
                    candidates.append(url)
    except Exception:
        pass

    # Stage 2: HTML fallback if library failed
    if not candidates:
        try:
            post_data = f"q={quote_plus(query)}&kl=us-en"
            async with _httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
                resp = await client.post("https://html.duckduckgo.com/html/", content=post_data)
            soup = _BS(resp.text, "html.parser")
            for div in soup.select(".result"):
                a = div.select_one(".result__a")
                if not a:
                    continue
                href = a.get("href","")
                # DuckDuckGo wraps URLs — extract from uddg param
                if "uddg=" in href:
                    from urllib.parse import unquote, parse_qs
                    qs = parse_qs(href.split("?",1)[-1])
                    href = unquote(qs.get("uddg",[""])[0])
                if href and href.startswith("http") and not _is_directory(href):
                    candidates.append(href)
        except Exception as e:
            log.warning(f"Website finder HTML fallback failed: {e}")

    # Stage 3: Google fallback via Decodo (uses your existing key)
    if not candidates:
        try:
            token = "VTAwMDA0MTc5OTc6UFdfMTFlM2U1ZjJiMDg1MTkwNTMzNjEzYjQzYWFjZDY2Zjlh"
            search_url = f"https://www.google.com/search?q={quote_plus(query + ' official website')}&num=5"
            async with _httpx.AsyncClient(timeout=25) as client:
                resp = await client.post(
                    "https://scraper-api.decodo.com/v2/scrape",
                    json={"url": search_url, "headless": "html", "proxy_pool": "residential"},
                    headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
                )
            if resp.status_code == 200:
                soup = _BS(resp.json().get("body",""), "html.parser")
                for a in soup.select("a[href]"):
                    href = a["href"]
                    if href.startswith("/url?q="):
                        href = href[7:].split("&")[0]
                    if href.startswith("http") and not _is_directory(href):
                        candidates.append(href)
        except Exception as e:
            log.warning(f"Website finder Decodo fallback failed: {e}")

    if not candidates:
        return {"website": None, "found": False}

    # Normalise — return just the root domain
    best = candidates[0]
    try:
        p = urlparse(best)
        website = f"{p.scheme}://{p.netloc}"
    except Exception:
        website = best

    log.info(f"Website found for {req.firm_name}: {website}")
    return {"website": website, "found": True, "all_candidates": candidates[:5]}


# ── Investment Score Endpoint ─────────────────────────────────────────────────

class ScoreRequest(BaseModel):
    firm:     dict
    insights: dict | None = None

@app.post("/api/score-firm")
async def score_firm_endpoint(req: ScoreRequest):
    """
    Compute LEA investment score (0-100) for a firm.
    Pass firm data dict + optional insights from /api/ai-analyze.
    """
    from ai_engine import score_firm
    try:
        result = await score_firm(req.firm, req.insights)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── LEA Firms Summary (for Heat Map) ─────────────────────────────────────────

@app.get("/api/lea-firms-summary")
async def lea_firms_summary():
    """Return all LEA research firms for the geographic heat map."""
    try:
        from scraper import _load_lea_firms
        firms = _load_lea_firms()
        # Return lightweight fields only
        result = []
        for f in firms:
            result.append({
                "name":               f.get("name",""),
                "city":               f.get("city",""),
                "state":              f.get("state",""),
                "market":             f.get("market",""),
                "lea_rank":           f.get("rank"),
                "lea_notes":          f.get("notes",""),
                "google_stars":       f.get("google_stars"),
                "google_review_count":f.get("google_review_count"),
                "source":             "LEA Research",
            })
        return {"firms": result, "total": len(result)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Weekly Digest Endpoint ────────────────────────────────────────────────────

class DigestRequest(BaseModel):
    markets: list[dict]   # [{city, state, practice_area}]
    max_per_market: int = 5

@app.post("/api/digest")
async def digest_endpoint(req: DigestRequest):
    """
    Run discovery for each market and return top firms sorted by rule-based score.
    Used by the weekly automated digest.
    """
    from scraper import discover_firms
    from ai_engine import _compute_rule_score
    import asyncio

    results = []
    for market in req.markets[:10]:   # cap at 10 markets per run
        try:
            disc = await discover_firms(
                city=market.get("city",""),
                state=market.get("state",""),
                practice_area=market.get("practice_area","personal injury"),
                max_results=20,
            )
            firms = disc.get("results", [])
            # Quick rule-based score (no LLM — keeps digest fast)
            for f in firms:
                rs = _compute_rule_score(f, None)
                f["rule_score"] = rs["_rule_total"]
            firms.sort(key=lambda x: -x.get("rule_score", 0))
            results.append({
                "market": f"{market.get('city')}, {market.get('state')}",
                "top_firms": firms[:req.max_per_market],
                "total_found": len(firms),
            })
        except Exception as e:
            results.append({"market": f"{market.get('city')}, {market.get('state')}", "error": str(e)})

    return {"digest": results, "markets_scanned": len(results)}


# ── User List ─────────────────────────────────────────────────────────────────

LEA_USERS = [
    "Admin", "LEA Investor",
]

@app.get("/api/users")
async def get_users():
    return {"users": LEA_USERS}


# ── User Watchlist Persistence ────────────────────────────────────────────────

class WatchlistSaveRequest(BaseModel):
    username: str
    watchlist: list  # list of firm dicts
    crm_data: dict = {}

@app.post("/api/user/save")
async def save_user_data(req: WatchlistSaveRequest):
    """Save a user's entire watchlist + CRM state to the DB."""
    conn = get_db()
    try:
        # Upsert each firm in watchlist
        for firm in req.watchlist:
            key = f"{firm.get('name','')}|{firm.get('city','')}"
            conn.execute(
                "INSERT OR REPLACE INTO user_watchlists (username, firm_key, firm_data, saved_at) VALUES (?,?,?,datetime('now'))",
                (req.username, key, json.dumps(firm))
            )
        # Upsert CRM entries
        for firm_key, crm in req.crm_data.items():
            conn.execute(
                "INSERT OR REPLACE INTO user_crm (username, firm_key, crm_data, updated_at) VALUES (?,?,?,datetime('now'))",
                (req.username, firm_key, json.dumps(crm))
            )
        conn.commit()
        return {"ok": True, "saved": len(req.watchlist)}
    finally:
        conn.close()

@app.get("/api/user/{username}/load")
async def load_user_data(username: str):
    """Load a user's watchlist + CRM data from the DB."""
    conn = get_db()
    try:
        wl_rows = conn.execute(
            "SELECT firm_data FROM user_watchlists WHERE username=? ORDER BY saved_at DESC",
            (username,)
        ).fetchall()
        crm_rows = conn.execute(
            "SELECT firm_key, crm_data FROM user_crm WHERE username=?",
            (username,)
        ).fetchall()
        watchlist = [json.loads(r["firm_data"]) for r in wl_rows]
        crm_data  = {r["firm_key"]: json.loads(r["crm_data"]) for r in crm_rows}
        return {"username": username, "watchlist": watchlist, "crm_data": crm_data}
    finally:
        conn.close()

@app.delete("/api/user/{username}/watchlist/{firm_key:path}")
async def remove_from_watchlist(username: str, firm_key: str):
    """Remove one firm from a user's watchlist."""
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM user_watchlists WHERE username=? AND firm_key=?",
            (username, firm_key)
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ── Google Sheets Integration ─────────────────────────────────────────────────

SPREADSHEET_ID  = "1Xn9T5hCEgbnMmE1cRBpEaDQvhxvcl4yGUI4MQlWKdXQ"
SHEET_TAB_NAME  = os.getenv("GOOGLE_SHEET_TAB", "Sheet1")  # set in .env if tab has a custom name
CREDS_PATH      = Path(__file__).parent / "google_credentials.json"

class LinkToSheetRequest(BaseModel):
    firm:     dict
    username: str = "Unknown"
    channel:  str = "Cold Call"

@app.post("/api/link-to-sheet")
async def link_to_sheet(req: LinkToSheetRequest):
    """Append a firm row to the LEA sourcing Google Sheet."""
    # Support credentials from env var (for Railway/cloud) or local file
    creds_json_str = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
    if not creds_json_str and not CREDS_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="Google credentials not configured. Add google_credentials.json or set GOOGLE_CREDENTIALS_JSON env var. See SETUP_GOOGLE_SHEETS.md for instructions."
        )
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        import json as _json

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        if creds_json_str:
            # Cloud deployment: credentials from environment variable
            creds_info = _json.loads(creds_json_str)
            creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        else:
            # Local development: credentials from file
            creds = Credentials.from_service_account_file(str(CREDS_PATH), scopes=scopes)
        client = gspread.authorize(creds)
        sheet  = client.open_by_key(SPREADSHEET_ID)

        # Try tab by name, fall back to first sheet
        try:
            ws = sheet.worksheet(SHEET_TAB_NAME)
        except Exception:
            ws = sheet.get_worksheet(0)

        firm = req.firm
        today = datetime.now().strftime("%Y-%m-%d")

        # Find next available ID
        all_vals = ws.col_values(2)  # column B = Firm Name
        # Count only rows with actual firm names (skip header rows 1 & 2)
        filled = [v for v in all_vals[2:] if v.strip()]
        next_id  = max(1, len(filled) + 1)
        # Target the specific row (row 1=title, row 2=header, row 3+ = data)
        target_row = len(filled) + 3  # +1 for title, +1 for header, +1 for next

        # Revenue label → numeric
        rev_label = firm.get("revenue_label", "") or ""
        import re
        rev_match = re.search(r"[\d.]+", rev_label.replace(",",""))
        rev_num = rev_match.group() if rev_match else ""

        # Attorney count
        atty = (firm.get("attorney_count_website") or
                firm.get("attorney_count_web") or
                firm.get("attorney_count_est") or
                firm.get("attorney_count") or "")

        # Row: A=ID, B=Firm Name, C=City, D=State, E=Practice Type,
        #      F=Owner, G=Channel, H=Date Contacted, I=Phone, J=Email,
        #      K=Person Spoken With (blank), L=# Times Contacted (blank),
        #      M=Response (blank), N=Stage/Result (blank),
        #      O=Est Revenue ($M), P=Est Profit (blank),
        #      Q=# of Attorneys, R=Next Step (blank),
        #      S=Follow-Up Date (blank), T=Notes (blank)
        row = [
            next_id,                                                      # A: ID
            firm.get("name", ""),                                         # B: Firm Name
            firm.get("city", ""),                                         # C: City
            firm.get("state", ""),                                        # D: State
            firm.get("practice_areas", ["Personal Injury (General)"])[0] # E: Practice Type
                if isinstance(firm.get("practice_areas"), list)
                else firm.get("practice_area", "Personal Injury (General)"),
            firm.get("founder_name", "") or firm.get("leader_name", ""), # F: Owner
            req.channel,                                                  # G: Channel
            today,                                                        # H: Date Contacted
            firm.get("phone", ""),                                        # I: Phone
            firm.get("email", "") or firm.get("leader_email", ""),       # J: Email
            "",                                                           # K: Person Spoken With
            "",                                                           # L: # Times Contacted
            "",                                                           # M: Response
            "",                                                           # N: Stage/Result
            rev_num,                                                      # O: Est Revenue ($M)
            "",                                                           # P: Est Profit ($M)
            atty,                                                         # Q: # of Attorneys
            "",                                                           # R: Next Step
            "",                                                           # S: Follow-Up Date
            f"Added via LEA app by {req.username}",                      # T: Notes
        ]

        ws.update(f'A{target_row}', [row], value_input_option='USER_ENTERED')

        # Log to local DB
        conn = get_db()
        conn.execute(
            "INSERT INTO sheet_log (username, firm_name, firm_key, sheet_row) VALUES (?,?,?,?)",
            (req.username, firm.get("name",""), f"{firm.get('name','')}|{firm.get('city','')}", next_id)
        )
        conn.commit()
        conn.close()

        return {"ok": True, "row": next_id, "firm": firm.get("name","")}

    except ImportError:
        raise HTTPException(status_code=503, detail="gspread not installed. Run: pip install gspread google-auth")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Google Sheets error: {str(e)}")


@app.get("/api/sheet-log/{firm_key:path}")
async def get_sheet_log(firm_key: str):
    """Check if a firm has already been linked to the sheet."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT username, linked_at, sheet_row FROM sheet_log WHERE firm_key=? ORDER BY linked_at DESC",
            (firm_key,)
        ).fetchall()
        return {"linked": len(rows) > 0, "entries": [dict(r) for r in rows]}
    finally:
        conn.close()
