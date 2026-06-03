"""
LEA Scraper Module
Pulls data from Google Places, CourtListener, AVVO, firm websites, and state bars.
All scrapers return dicts that map directly to scorer.py data classes.
"""

from __future__ import annotations
import os, re, time, logging, json
from typing import Optional
from datetime import datetime
from urllib.parse import urljoin, urlparse, quote_plus

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("lea.scraper")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 15


# ─────────────────────────────────────────────────────────────
# 1. GOOGLE PLACES API  (requires GOOGLE_API_KEY in .env)
# ─────────────────────────────────────────────────────────────

async def scrape_google_places(firm_name: str, city: str, state: str) -> dict:
    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        log.warning("GOOGLE_API_KEY not set — skipping Google Places")
        return {"google_stars": None, "google_reviews": None, "rating_source": None, "error": "No GOOGLE_API_KEY configured"}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        query = f"{firm_name} law firm {city} {state}"

        # Text search
        search_url = (
            "https://maps.googleapis.com/maps/api/place/textsearch/json"
            f"?query={quote_plus(query)}&type=lawyer&key={api_key}"
        )
        resp = await client.get(search_url)
        data = resp.json()
        results = data.get("results", [])
        if not results:
            log.warning(f"No Google Places results for {firm_name}")
            return {}

        place = results[0]
        place_id = place.get("place_id")

        # Detailed place info
        detail_url = (
            "https://maps.googleapis.com/maps/api/place/details/json"
            f"?place_id={place_id}"
            f"&fields=name,rating,user_ratings_total,website,formatted_phone_number,reviews"
            f"&key={api_key}"
        )
        det_resp = await client.get(detail_url)
        det = det_resp.json().get("result", {})

        reviews = det.get("reviews", [])
        sentiment = _simple_sentiment(reviews)

        return {
            "google_stars": det.get("rating", 0.0),
            "google_review_count": det.get("user_ratings_total", 0),
            "website": det.get("website", ""),
            "phone": det.get("formatted_phone_number", ""),
            "nlp_sentiment_score": sentiment,
            "response_rate": _estimate_response_rate(reviews),
        }


def _simple_sentiment(reviews: list) -> float:
    """Very basic VADER-lite sentiment on review texts."""
    if not reviews:
        return 50.0
    positive_words = {"excellent","great","amazing","wonderful","professional","helpful",
                      "best","fantastic","highly recommend","efficient","responsive",
                      "outstanding","exceptional","knowledgeable","compassionate","won",
                      "settlement","successful","resolved","grateful","thankful"}
    negative_words = {"bad","terrible","awful","horrible","unprofessional","slow",
                      "rude","ignored","incompetent","waste","disappointed","never",
                      "useless","scam","fraud","unhelpful","unresponsive"}
    pos = neg = 0
    for r in reviews:
        text = r.get("text", "").lower()
        pos += sum(1 for w in positive_words if w in text)
        neg += sum(1 for w in negative_words if w in text)
    total = pos + neg
    if total == 0:
        return 60.0
    return round((pos / total) * 100, 1)


def _estimate_response_rate(reviews: list) -> float:
    if not reviews:
        return 0.0
    replied = sum(1 for r in reviews if r.get("owner_response"))
    return round((replied / len(reviews)) * 100, 1)


def _mock_google_data(firm_name: str) -> dict:
    """Deterministic mock so UI always has data when no API key."""
    import hashlib
    h = int(hashlib.md5(firm_name.encode()).hexdigest(), 16)
    stars = round(3.8 + (h % 12) / 10, 1)
    count = 20 + (h % 300)
    return {
        "google_stars": min(stars, 5.0),
        "google_review_count": count,
        "nlp_sentiment_score": 55.0 + (h % 35),
        "response_rate": 20.0 + (h % 60),
        "website": "",
    }


# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# 1c. OUTSCRAPER — Real Google Maps ratings, no Google key needed
# Sign up free at https://outscraper.com (no card required, $3 credit)
# ─────────────────────────────────────────────────────────────
OUTSCRAPER_ENDPOINT = "https://api.app.outscraper.com/maps/search-v3"

# 1b. DECODO WEB SCRAPING API — Real Google reviews, no Google key needed
# ─────────────────────────────────────────────────────────────

DECODO_TOKEN = "VTAwMDA0MTc5OTc6UFdfMTFlM2U1ZjJiMDg1MTkwNTMzNjEzYjQzYWFjZDY2Zjlh"
DECODO_ENDPOINT = "https://scraper-api.decodo.com/v2/scrape"

def _parse_google_rating_from_serp(html: str) -> tuple:
    """Parse (stars, review_count) from Google SERP or Maps HTML."""
    if not html:
        return 0.0, 0

    # Method 1: JSON-LD structured data (most reliable)
    try:
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    rating_data = item.get("aggregateRating") if isinstance(item, dict) else None
                    if rating_data:
                        stars = float(rating_data.get("ratingValue", 0))
                        count = int(str(rating_data.get("reviewCount", 0)).replace(",", ""))
                        if 1.0 <= stars <= 5.0 and count > 0:
                            return round(stars, 1), count
            except Exception:
                continue
    except Exception:
        pass

    # Method 2: Regex — Google knowledge panel patterns
    # Stars: "4.5" near ratings context
    stars = 0.0
    count = 0

    for pattern in [r'"ratingValue"\s*:\s*"?(\d+\.?\d*)"?',
                    r'(\d+\.\d+)\s*stars?',
                    r'(\d+\.\d+)\s*/\s*5']:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            try:
                v = float(m.group(1))
                if 1.0 <= v <= 5.0:
                    stars = round(v, 1)
                    break
            except Exception:
                pass

    for pattern in [r'"reviewCount"\s*:\s*"?(\d[\d,]*)"?',
                    r'(\d[\d,]*)\s+(?:Google\s+)?reviews?',
                    r'Based on (\d[\d,]*) reviews?']:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            try:
                count = int(m.group(1).replace(",", ""))
                if count > 0:
                    break
            except Exception:
                pass

    return stars, count


async def scrape_google_decodo(firm_name: str, city: str, state: str) -> dict:
    """
    Use Decodo Web Scraping API to get real Google ratings.
    Fetches Google SERP for the firm — knowledge panel has rating + review count.
    Costs ~$1.50/1K requests (residential proxy). Falls back gracefully.
    """
    token = os.getenv("DECODO_API_KEY", DECODO_TOKEN)
    query = f'"{firm_name}" law firm {city} {state}'
    search_url = f"https://www.google.com/search?q={quote_plus(query)}&hl=en&gl=us&num=5"

    payload = {
        "url": search_url,
        "headless": "html",
        "proxy_pool": "residential",
    }
    headers = {
        "Accept": "application/json",
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(DECODO_ENDPOINT, json=payload, headers=headers)

        if resp.status_code != 200:
            log.warning(f"Decodo error {resp.status_code} for {firm_name}: {resp.text[:200]}")
            return {"google_stars": None, "google_review_count": None,
                    "rating_source": None, "error": f"Decodo HTTP {resp.status_code}"}

        data = resp.json()
        # Decodo returns body in 'body' key
        html = data.get("body") or data.get("content") or ""

        stars, count = _parse_google_rating_from_serp(html)

        if stars > 0:
            log.info(f"Decodo Google: {firm_name} → {stars}★ ({count} reviews)")
            return {
                "google_stars":        stars,
                "google_review_count": count,
                "rating_source":       "Google",
            }
        else:
            log.warning(f"Decodo: No rating found in SERP for {firm_name}")
            return {"google_stars": None, "google_review_count": None, "rating_source": None}

    except Exception as e:
        log.warning(f"Decodo scrape failed for {firm_name}: {e}")
        return {"google_stars": None, "google_review_count": None,
                "rating_source": None, "error": str(e)}


# ─────────────────────────────────────────────────────────────
# 2. COURTLISTENER API  (free, no key needed)
# ─────────────────────────────────────────────────────────────

async def scrape_courtlistener(firm_name: str, state: str, **kwargs) -> dict:
    """
    Search CourtListener v3 dockets API for a specific firm.
    Uses auth token from COURTLISTENER_TOKEN env var (5000 req/day free).
    Searches by firm name across all dockets, filtered to the relevant state court.
    """
    from datetime import timedelta

    cl_token = os.getenv("COURTLISTENER_TOKEN", "")
    req_headers = {**HEADERS, "Accept": "application/json"}
    if cl_token:
        req_headers["Authorization"] = f"Token {cl_token}"

    # State → primary federal court districts
    state_courts = {
        "FL": ["flsd", "flmd"], "TX": ["txsd", "txnd"],
        "CA": ["cacd", "cand"], "NY": ["nysd", "nyed"],
        "GA": ["gand"],          "IL": ["ilnd"],
        "NC": ["ncwd", "nced"], "TN": ["tnmd", "tnwd"],
        "AZ": ["azd"],           "PA": ["paed", "pawd"],
        "OH": ["ohnd", "ohsd"], "MI": ["mied"],
        "VA": ["vaed"],          "CO": ["cod"],
        "WA": ["wawd"],          "NV": ["nvd"],
        "MA": ["mad"],           "MN": ["mnd"],
        "MO": ["moed"],          "OR": ["ord"],
        "MD": ["mdd"],           "LA": ["laed"],
    }
    courts = state_courts.get(state.upper(), [])

    # Date window: last 2 years
    date_from = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")

    total_count = 0
    sample_dockets = []
    case_count_90d = 0
    date_from_90d  = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

    try:
        async with httpx.AsyncClient(timeout=20, headers=req_headers, follow_redirects=True) as client:
            for court in (courts[:1] or [""]):
                # ── All-time count for this firm ────────────────────────
                params: dict = {
                    "q":          f'"{firm_name}"',
                    "order_by":   "-date_filed",
                    "page_size":  "10",
                    "format":     "json",
                    "date_filed__gte": date_from,
                }
                if court:
                    params["court"] = court

                r = await client.get(
                    "https://www.courtlistener.com/api/rest/v3/dockets/",
                    params=params,
                )
                if r.status_code == 200:
                    data = r.json()
                    total_count   = data.get("count", 0)
                    sample_dockets = [d.get("case_name", "") for d in data.get("results", [])[:5]]

                # ── 90-day count ────────────────────────────────────────
                params90 = {**params, "date_filed__gte": date_from_90d}
                r90 = await client.get(
                    "https://www.courtlistener.com/api/rest/v3/dockets/",
                    params=params90,
                )
                if r90.status_code == 200:
                    case_count_90d = r90.json().get("count", 0)

                # If strict quoted search returns 0, try unquoted fallback
                if total_count == 0:
                    params_loose = {**params, "q": firm_name}
                    r2 = await client.get(
                        "https://www.courtlistener.com/api/rest/v3/dockets/",
                        params=params_loose,
                    )
                    if r2.status_code == 200:
                        d2 = r2.json()
                        total_count    = d2.get("count", 0)
                        sample_dockets = [d.get("case_name", "") for d in d2.get("results", [])[:5]]

    except Exception as exc:
        log.warning(f"CourtListener scrape failed for {firm_name}: {exc}")

    return {
        "courtlistener_case_count": total_count,
        "total_cases_found":        total_count,
        "case_count_90d":           case_count_90d,
        "sample_dockets":           sample_dockets,
        "has_federal_cases":        total_count > 0,
    }


# ─────────────────────────────────────────────────────────────
# 3. AVVO SCRAPER
# ─────────────────────────────────────────────────────────────

async def scrape_avvo(firm_name: str, city: str, state: str) -> dict:
    """
    Scrapes AVVO for attorney ratings under a firm.
    Returns average AVVO rating and count of rated attorneys.
    """
    city_slug = city.lower().replace(" ", "-")
    state_slug = state.lower()

    # Try the firm search page
    search_url = (
        f"https://www.avvo.com/search/lawyer_search.json"
        f"?q={quote_plus(firm_name)}&loc={quote_plus(city+', '+state)}"
    )

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
            resp = await client.get(search_url)
            if resp.status_code == 200:
                data = resp.json()
                lawyers = data.get("lawyers", [])
                if lawyers:
                    ratings = [float(l.get("rating", 0)) for l in lawyers if l.get("rating")]
                    avg_rating = sum(ratings) / len(ratings) if ratings else 0.0
                    return {
                        "avvo_rating": round(avg_rating, 1),
                        "avvo_attorney_count": len(lawyers),
                    }
    except Exception as e:
        log.debug(f"AVVO JSON search failed for {firm_name}: {e}")

    # Fallback: scrape HTML
    try:
        url = (
            f"https://www.avvo.com/personal-injury-lawyer/{state_slug}/"
            f"{city_slug.replace('-', '_')}.html"
        )
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
            resp = await client.get(url)
            soup = BeautifulSoup(resp.text, "html.parser")
            rating_tags = soup.select("[class*='rating'], [data-rating], [itemprop='ratingValue']")
            ratings = []
            for tag in rating_tags[:10]:
                txt = tag.get_text(strip=True)
                m = re.search(r"(\d+\.\d+|\d+)", txt)
                if m:
                    val = float(m.group(1))
                    if 1.0 <= val <= 10.0:
                        ratings.append(val)
            if ratings:
                return {"avvo_rating": round(sum(ratings)/len(ratings), 1)}
    except Exception as e:
        log.debug(f"AVVO HTML scrape failed: {e}")

    return {"avvo_rating": 0.0}


# ─────────────────────────────────────────────────────────────
# 4. FIRM WEBSITE SCRAPER
# ─────────────────────────────────────────────────────────────

PRACTICE_AREA_KEYWORDS = {
    "personal injury": ["personal injury", "accident", "injury"],
    "auto accidents": ["auto accident", "car accident", "vehicle accident", "motor vehicle"],
    "premises liability": ["premises liability", "slip and fall", "property accident"],
    "workers compensation": ["workers comp", "workers compensation", "work injury"],
    "immigration": ["immigration", "visa", "deportation", "citizenship", "daca"],
    "consumer bankruptcy": ["bankruptcy", "chapter 7", "chapter 13", "debt relief"],
    "mass tort": ["mass tort", "class action", "multidistrict", "mdl"],
    "criminal": ["criminal defense", "criminal law", "dui", "dwi", "drug charges"],
    "family law": ["family law", "divorce", "child custody", "alimony", "adoption"],
    "medical malpractice": ["medical malpractice", "medical negligence", "doctor error"],
    "product liability": ["product liability", "defective product"],
    "sexual abuse": ["sexual abuse", "sexual assault", "sexual harassment"],
}


async def scrape_firm_website(url: str) -> dict:
    if not url:
        return {}
    if not url.startswith("http"):
        url = "https://" + url

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
            resp = await client.get(url)
            soup = BeautifulSoup(resp.text, "html.parser")
            text = soup.get_text(" ", strip=True).lower()
            full_html = resp.text.lower()

            # Practice areas
            detected_areas = []
            for area, keywords in PRACTICE_AREA_KEYWORDS.items():
                if any(kw in text for kw in keywords):
                    detected_areas.append(area)

            # Attorney count
            attorney_count = _extract_attorney_count(soup, text)

            # Technology signals
            has_live_chat = any(w in full_html for w in ["livechat", "intercom", "drift", "tawk", "chat widget", "zendesk"])
            has_digital_intake = any(w in full_html for w in ["intake form", "online form", "book online", "schedule online", "free consultation form"])

            # CRM / tech stack detection
            crm = ""
            crm_signals = {
                "Clio": ["clio.com", "clio grow", "goclio"],
                "MyCase": ["mycase.com", "mycase"],
                "Filevine": ["filevine.com", "filevine"],
                "Litify": ["litify.com", "litify"],
                "HubSpot": ["hubspot.com", "hubspot"],
                "Salesforce": ["salesforce.com", "salesforce"],
            }
            for crm_name, signals in crm_signals.items():
                if any(s in full_html for s in signals):
                    crm = crm_name
                    break

            # Junior partners / succession
            team_indicators = ["our team", "attorneys", "meet the team", "our lawyers", "partners"]
            has_team_page = any(t in text for t in team_indicators)
            junior_indicators = ["associate", "junior partner", "of counsel", "staff attorney"]
            has_junior = any(j in text for j in junior_indicators)

            # TV / advertising signals
            tv_signals = ["as seen on tv", "billboard", "1-800", "1 800", "toll-free"]
            tv_advertising = any(s in text for s in tv_signals)

            # Google Ads signal from meta/scripts
            google_ads_active = "googleadservices" in full_html or "googletag" in full_html or "adwords" in full_html

            # Website freshness (find copyright year or last-updated)
            last_updated_year = _extract_website_year(soup, text)

            # Contingency language
            contingency_pct = 0.0
            contingency_matches = re.findall(r"no fee unless you win|contingency|no recovery no fee", text)
            if contingency_matches:
                contingency_pct = 85.0  # strong contingency signal

            # SEO: count words in practice area pages
            pi_word_count = sum(text.count(w) for w in ["injury", "accident", "settlement", "trial", "verdict", "compensation"])
            total_words = len(text.split())
            practice_focus_pct = round((pi_word_count / max(total_words, 1)) * 100 * 10, 1)
            practice_focus_pct = min(practice_focus_pct, 99.0)

            return {
                "detected_practice_areas": detected_areas,
                "attorney_count": attorney_count,
                "has_live_chat": has_live_chat,
                "has_digital_intake": has_digital_intake,
                "crm": crm,
                "has_junior_partners": has_junior,
                "tv_advertising": tv_advertising,
                "google_ads_active": google_ads_active,
                "website_last_updated_year": last_updated_year,
                "contingency_pct": contingency_pct,
                "practice_focus_pct": practice_focus_pct,
            }
    except Exception as e:
        log.warning(f"Website scrape failed for {url}: {e}")
        return {}


def _extract_attorney_count(soup: BeautifulSoup, text: str) -> int:
    # Look for "X attorneys" or "team of X"
    patterns = [
        r"(\d+)\s+attorneys",
        r"team of (\d+)",
        r"(\d+)\s+lawyers",
        r"our (\d+) attorneys",
        r"(\d+)\+\s+attorneys",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return int(m.group(1))
    # Count attorney profile cards
    profile_tags = soup.select("[class*='attorney'], [class*='lawyer'], [class*='team-member'], [class*='staff']")
    if len(profile_tags) >= 2:
        return min(len(profile_tags), 200)
    return 0


def _extract_website_year(soup: BeautifulSoup, text: str) -> Optional[int]:
    # Copyright year
    copyright_match = re.search(r"©\s*(\d{4})|copyright\s*(\d{4})", text)
    if copyright_match:
        yr = int(copyright_match.group(1) or copyright_match.group(2))
        if 2015 <= yr <= 2030:
            return yr
    # Meta last-modified
    meta = soup.find("meta", {"http-equiv": "last-modified"})
    if meta and meta.get("content"):
        m = re.search(r"(\d{4})", meta["content"])
        if m:
            return int(m.group(1))
    return None


# ─────────────────────────────────────────────────────────────
# 5. STATE BAR SCRAPER  (bar admission year for owner age)
# ─────────────────────────────────────────────────────────────

STATE_BAR_URLS = {
    "FL": "https://www.floridabar.org/directories/find-mbr/?lName={last_name}&fName={first_name}",
    "TX": "https://www.texasbar.com/AM/Template.cfm?Section=Find_A_Lawyer&Template=/customsource/memberdirectory/searchprocess.cfm",
    "CA": "https://apps.calbar.ca.gov/attorney/LicenseeSearch/QuickSearch?licenseType=A&searchType=L&searchValue={last_name}",
    "GA": "https://www.gabar.org/MemberSearchForm.cfm",
    "NY": "https://iapps.courts.state.ny.us/attorney/AttorneySearch",
}


async def scrape_state_bar(attorney_name: str, state: str) -> dict:
    """
    Attempts to find bar admission year for founder/owner age estimation.
    Falls back to name-based estimation if scraping fails.
    """
    parts = attorney_name.strip().split()
    first = parts[0] if parts else ""
    last = parts[-1] if len(parts) > 1 else parts[0] if parts else ""

    # Try Florida Bar (most open API)
    if state.upper() == "FL":
        try:
            url = f"https://www.floridabar.org/directories/find-mbr/?lName={quote_plus(last)}&fName={quote_plus(first)}"
            async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
                resp = await client.get(url)
                soup = BeautifulSoup(resp.text, "html.parser")
                # Look for admit date
                admit_cells = soup.find_all(string=re.compile(r"Admit(ted)?\s*Date", re.I))
                for cell in admit_cells:
                    parent = cell.parent.find_next_sibling()
                    if parent:
                        m = re.search(r"(\d{4})", parent.get_text())
                        if m:
                            yr = int(m.group(1))
                            if 1960 <= yr <= 2020:
                                return {"bar_admission_year": yr, "bar_state": state}
        except Exception as e:
            log.debug(f"FL Bar scrape failed: {e}")

    # Generic: try to find year from any state bar search
    # (most state bars allow public attorney lookup)
    return {"bar_admission_year": None}


# ─────────────────────────────────────────────────────────────
# 6. SEO SIGNAL (SerpAPI / fallback DuckDuckGo)
# ─────────────────────────────────────────────────────────────

async def scrape_seo_signals(firm_name: str, city: str, state: str, website: str = "") -> dict:
    """Check Google/SERP ranking for '[city] personal injury lawyer'."""
    serp_api_key = os.getenv("SERP_API_KEY", "")

    if serp_api_key:
        query = f"{city} {state} personal injury lawyer"
        url = f"https://serpapi.com/search.json?engine=google&q={quote_plus(query)}&api_key={serp_api_key}&num=20"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(url)
                data = resp.json()
                organic = data.get("organic_results", [])
                domain = urlparse(website).netloc.replace("www.", "") if website else ""
                for i, result in enumerate(organic):
                    result_domain = urlparse(result.get("link", "")).netloc.replace("www.", "")
                    if domain and domain in result_domain:
                        return {"google_rank_primary_kw": i + 1}
        except Exception as e:
            log.debug(f"SerpAPI error: {e}")

    return {"google_rank_primary_kw": 0}


# ─────────────────────────────────────────────────────────────
# 7. LINKEDIN  (Google SERP snippet extraction — no cookies needed)
# ─────────────────────────────────────────────────────────────

async def scrape_linkedin_signals(firm_name: str) -> dict:
    """
    Extract LinkedIn company data from Google search snippets.

    Primary:  SerpAPI engine=google (site:linkedin.com/company query) — returns
              clean JSON with snippet text that includes employee range & followers.
    Fallback: DuckDuckGo library search of the same query.

    Google indexes LinkedIn company pages and exposes in snippets:
      "51-200 employees · Headquarters: New York · Founded: 2013"
    And in the page title/description:
      "2.6K+ followers"

    Returns:
        linkedin_headcount       – midpoint integer  (e.g. 125 for "51-200")
        linkedin_headcount_range – display string    (e.g. "51-200 employees")
        linkedin_followers       – int or None       (e.g. 2600 for "2.6K+")
        linkedin_followers_label – display string    (e.g. "2.6K+")
        linkedin_industry        – str or None
        linkedin_hiring          – bool
        linkedin_profile_url     – str or None
    """
    RANGE_MAP = {
        "1-10": 5, "2-10": 5,
        "11-50": 30, "11-49": 30,
        "51-200": 125, "50-200": 125,
        "201-500": 350,
        "501-1000": 750,
        "1001-5000": 3000,
        "5001-10000": 7500,
        "10001+": 10001,
    }

    def _parse_followers_str(s: str):
        """Convert '2.6K+', '1.2M+', '850' etc. → (int, label_str)"""
        s = s.strip().rstrip("+").strip()
        try:
            if s.upper().endswith("M"):
                return int(float(s[:-1]) * 1_000_000), s + "+"
            if s.upper().endswith("K"):
                return int(float(s[:-1]) * 1_000), s + "+"
            return int(s.replace(",", "")), s
        except Exception:
            return None, s

    def _parse_snippet(text: str) -> dict:
        result = {}

        # Employee range — e.g. "51-200 employees"
        range_m = re.search(r"(\d[\d,]*[-–]\d[\d,]*|10[,.]?001\+?)\s*employees", text, re.I)
        if range_m:
            raw = range_m.group(1).replace("–", "-").replace(",", "").replace(".", "")
            result["linkedin_headcount_range"] = f"{raw} employees"
            result["linkedin_headcount"] = RANGE_MAP.get(raw)
            if result["linkedin_headcount"] is None:
                parts = raw.split("-")
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    result["linkedin_headcount"] = (int(parts[0]) + int(parts[1])) // 2

        # Exact count fallback — e.g. "412 employees"
        if "linkedin_headcount" not in result:
            exact_m = re.search(r"(\d{1,5})\s*(?:employees|attorneys|professionals|lawyers)", text, re.I)
            if exact_m:
                result["linkedin_headcount"] = int(exact_m.group(1))

        # Followers — handles "2.6K+ followers", "1,240 followers", "850 followers"
        fol_m = re.search(r"([\d.,]+[KkMm]?\+?)\s*followers", text, re.I)
        if fol_m:
            val, label = _parse_followers_str(fol_m.group(1))
            if val is not None:
                result["linkedin_followers"] = val
                result["linkedin_followers_label"] = label

        # Hiring signal
        result["linkedin_hiring"] = bool(re.search(
            r"hiring|open positions|we're growing|join our team|now recruiting",
            text, re.I
        ))
        return result

    # ── PRIMARY: SerpAPI organic search ──────────────────────────────
    async def _try_serpapi(name: str) -> tuple[str, str | None]:
        api_key = os.getenv("SERP_API_KEY", "")
        if not api_key:
            return "", None
        query = f'site:linkedin.com/company "{name}" employees followers'
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get("https://serpapi.com/search.json", params={
                    "engine":  "google",
                    "q":       query,
                    "api_key": api_key,
                    "num":     5,
                    "gl":      "us",
                    "hl":      "en",
                })
            if resp.status_code != 200:
                return "", None
            data = resp.json()
            profile_url = None
            snippets = []
            # knowledge graph sometimes has follower count directly
            kg = data.get("knowledge_graph", {})
            if kg:
                snippets.append(kg.get("description", ""))
            # organic results contain employee range in snippet
            for r in data.get("organic_results", [])[:5]:
                link = r.get("link", "")
                if "linkedin.com/company" in link and not profile_url:
                    profile_url = link
                # Combine title + snippet — title often has "X followers"
                snippets.append(r.get("title", "") + " " + r.get("snippet", ""))
            return " · ".join(snippets), profile_url
        except Exception as e:
            log.debug(f"SerpAPI LinkedIn search failed: {e}")
            return "", None

    # ── FALLBACK: DuckDuckGo library ─────────────────────────────────
    async def _try_ddg(name: str) -> tuple[str, str | None]:
        snippets, profile_url = "", None
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = ddgs.text(
                    f'site:linkedin.com/company "{name}" employees followers',
                    max_results=5
                )
            combined = []
            for r in (results or []):
                combined.append(r.get("title", "") + " " + r.get("body", ""))
                href = r.get("href", "")
                if not profile_url and "linkedin.com/company" in href:
                    profile_url = href
            snippets = " · ".join(combined)
        except Exception as e:
            log.debug(f"DDG LinkedIn search failed: {e}")
        return snippets, profile_url

    # ── Run SerpAPI first; DDG as parallel safety net ─────────────────
    import asyncio as _ali
    (serp_text, serp_url), (ddg_text, ddg_url) = await _ali.gather(
        _try_serpapi(firm_name),
        _try_ddg(firm_name),
    )

    # Prefer SerpAPI result; augment with DDG if SerpAPI found nothing
    combined_text = serp_text if serp_text else ddg_text
    if serp_text and ddg_text:
        combined_text = serp_text + " · " + ddg_text
    profile_url = serp_url or ddg_url

    parsed = _parse_snippet(combined_text)

    return {
        "linkedin_headcount":        parsed.get("linkedin_headcount",        0),
        "linkedin_headcount_range":  parsed.get("linkedin_headcount_range",  None),
        "linkedin_followers":        parsed.get("linkedin_followers",         None),
        "linkedin_followers_label":  parsed.get("linkedin_followers_label",   None),
        "linkedin_industry":         parsed.get("linkedin_industry",          None),
        "linkedin_hiring":           parsed.get("linkedin_hiring",            False),
        "linkedin_profile_url":      profile_url,
        "linkedin_snippet":          combined_text[:500],
    }

# ─────────────────────────────────────────────────────────────
# 8. ORCHESTRATOR — run all scrapers for one firm
# ─────────────────────────────────────────────────────────────

async def research_firm(
    firm_name: str,
    city: str,
    state: str,
    website: str = "",
    founder_name: str = "",
) -> dict:
    """
    Master research function. Runs all scrapers and returns
    a unified dict ready for scorer.py.
    """
    import asyncio
    log.info(f"Researching: {firm_name} | {city}, {state}")

    # Run scrapers concurrently
    # Use Decodo (real Google scraping) when no Google Places API key
    _google_key      = os.getenv("GOOGLE_API_KEY", "")
    _serp_key        = os.getenv("SERP_API_KEY", "")
    _outscraper_key  = os.getenv("OUTSCRAPER_API_KEY", "")
    if _google_key:
        _google_scraper = scrape_google_places(firm_name, city, state)
    elif _serp_key:
        _google_scraper = scrape_google_serpapi(firm_name, city, state)
    elif _outscraper_key:
        _google_scraper = scrape_google_outscraper(firm_name, city, state)
    else:
        _google_scraper = scrape_google_decodo(firm_name, city, state)
    tasks = {
        "google": _google_scraper,
        "court": scrape_courtlistener(firm_name, state),
        "avvo": scrape_avvo(firm_name, city, state),
        "seo": scrape_seo_signals(firm_name, city, state, website),
        "linkedin": scrape_linkedin_signals(firm_name),
    }

    # Website scrape (needs URL first — may come from Google)
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    scraped = {}
    for key, res in zip(tasks.keys(), results):
        if isinstance(res, Exception):
            log.warning(f"Scraper '{key}' failed: {res}")
            scraped[key] = {}
        else:
            scraped[key] = res

    # Get website URL from Google if not provided
    if not website:
        website = scraped.get("google", {}).get("website", "")

    if website:
        try:
            site_data = await scrape_firm_website(website)
        except Exception as e:
            log.warning(f"Website scrape failed: {e}")
            site_data = {}
        scraped["website"] = site_data
    else:
        scraped["website"] = {}

    # Bar scrape for founder
    if founder_name:
        try:
            bar_data = await scrape_state_bar(founder_name, state)
        except Exception as e:
            bar_data = {}
        scraped["bar"] = bar_data
    else:
        scraped["bar"] = {}

    # ── Merge into unified dict ──
    g = scraped.get("google", {})
    c = scraped.get("court", {})
    a = scraped.get("avvo", {})
    s = scraped.get("seo", {})
    li = scraped.get("linkedin", {})
    w = scraped.get("website", {})
    b = scraped.get("bar", {})

    attorney_count = w.get("attorney_count") or li.get("linkedin_headcount") or 0

    return {
        # Google brand signals
        "google_stars": g.get("google_stars", 0.0),
        "google_review_count": g.get("google_review_count", 0),
        "nlp_sentiment_score": g.get("nlp_sentiment_score", 50.0),
        "response_rate": g.get("response_rate", 0.0),
        "website": website or g.get("website", ""),
        "phone": g.get("phone", ""),

        # AVVO
        "avvo_rating": a.get("avvo_rating", 0.0),

        # Court / case volume
        "courtlistener_case_count": c.get("courtlistener_case_count", 0),
        "has_federal_cases": c.get("has_federal_cases", False),
        "sample_dockets": c.get("sample_dockets", []),

        # SEO / market
        "google_rank_primary_kw": s.get("google_rank_primary_kw", 0),

        # Website-derived
        "detected_practice_areas": w.get("detected_practice_areas", []),
        "attorney_count": attorney_count,
        "has_live_chat": w.get("has_live_chat", False),
        "has_digital_intake": w.get("has_digital_intake", False),
        "crm": w.get("crm", ""),
        "has_junior_partners": w.get("has_junior_partners", False),
        "tv_advertising": w.get("tv_advertising", False),
        "google_ads_active": w.get("google_ads_active", False),
        "website_last_updated_year": w.get("website_last_updated_year"),
        "contingency_pct": w.get("contingency_pct", 0.0),
        "practice_focus_pct": w.get("practice_focus_pct", 0.0),

        # LinkedIn
        "linkedin_hiring":           li.get("linkedin_hiring",           False),
        "linkedin_headcount":        li.get("linkedin_headcount",        None),
        "linkedin_headcount_range":  li.get("linkedin_headcount_range",  None),
        "linkedin_followers":        li.get("linkedin_followers",        None),
        "linkedin_followers_label":  li.get("linkedin_followers_label",  None),
        "linkedin_industry":         li.get("linkedin_industry",         None),
        "linkedin_profile_url":      li.get("linkedin_profile_url",     None),

        # Bar admission
        "bar_admission_year": b.get("bar_admission_year"),
    }


# ─────────────────────────────────────────────────────────────
# NEW SCRAPERS — added for per-source scraping panel
# ─────────────────────────────────────────────────────────────

from datetime import timedelta


async def scrape_sec_edgar_source(firm_name: str, **kwargs) -> dict:
    """
    Search SEC EDGAR full-text for M&A filings that mention the firm as legal counsel.
    Free, no auth required. Rate limit: 10 req/sec.
    Target filing types: S-4 (mergers), DEFM14A (proxy), 8-K (deal announcements).
    """
    since = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    url = "https://efts.sec.gov/LATEST/search-index"
    edgar_headers = {"User-Agent": "LEA Investment research@lea.com", "Accept-Encoding": "gzip"}
    total = 0
    sample_deals = []

    async with httpx.AsyncClient(timeout=TIMEOUT, headers=edgar_headers) as client:
        for ftype in ["S-4", "DEFM14A", "8-K", "SC 13D"]:
            params = {
                "q": f'"{firm_name}"',
                "dateRange": "custom",
                "startdt": since,
                "enddt": datetime.now().strftime("%Y-%m-%d"),
                "forms": ftype,
            }
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    hits = data.get("hits", {})
                    count_obj = hits.get("total", {})
                    count = count_obj.get("value", 0) if isinstance(count_obj, dict) else int(count_obj or 0)
                    total += count
                    for h in hits.get("hits", [])[:2]:
                        src = h.get("_source", {})
                        sample_deals.append({
                            "form_type": ftype,
                            "company": src.get("entity_name", "N/A"),
                            "filed": src.get("file_date", "N/A"),
                        })
            except Exception as e:
                log.debug(f"SEC EDGAR {ftype} error for {firm_name}: {e}")

    return {
        "source": "SEC EDGAR",
        "variable": "M&A transactions advised",
        "total_filings": total,
        "period_days": 90,
        "sample_deals": sample_deals[:8],
    }


async def scrape_news_source(firm_name: str, query_type: str = "press",
                              days_back: int = 30, **kwargs) -> dict:
    """
    NewsAPI-based press coverage scraper.
    Requires NEWS_API_KEY env var (free at https://newsapi.org/).
    query_type: 'press' | 'wins' | 'negative' | 'laterals'
    """
    api_key = os.getenv("NEWS_API_KEY", "")
    if not api_key:
        return {
            "source": "NewsAPI",
            "error": "NEWS_API_KEY not set. Get a free key at https://newsapi.org/ and add to .env",
            "articles": [],
            "total_articles": 0,
        }

    since = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%dT00:00:00")
    query_map = {
        "press":    f'"{firm_name}" law firm',
        "wins":     f'"{firm_name}" (verdict OR settlement OR awarded OR "won" OR ruling)',
        "negative": f'"{firm_name}" (misconduct OR layoff OR scandal OR malpractice OR sanction OR "ethics violation")',
        "laterals": f'"{firm_name}" (lateral OR "joins" OR "departs" OR "leaves" OR partner)',
    }
    label_map = {
        "press":    "Total press mention count",
        "wins":     "Notable case wins / verdicts",
        "negative": "Negative press / controversy",
        "laterals": "Lateral hires & departures",
    }

    query = query_map.get(query_type, query_map["press"])
    label = label_map.get(query_type, "Press coverage")

    # Build both a strict and a loose query for fallback
    # Strict: exact firm name in quotes (best precision)
    # Loose:  firm name words + context (better recall for small local firms)
    loose_query_map = {
        "press":    f'{firm_name} attorney law',
        "wins":     f'{firm_name} verdict settlement awarded ruling',
        "negative": f'{firm_name} misconduct scandal malpractice sanction',
        "laterals": f'{firm_name} attorney lateral partner joins departs',
    }
    loose_query = loose_query_map.get(query_type, f'{firm_name} law')

    def _build_params(q: str) -> dict:
        return {
            "q": q, "from": since, "language": "en",
            "sortBy": "publishedAt", "pageSize": 10, "apiKey": api_key,
        }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            # Try strict query first
            resp = await client.get("https://newsapi.org/v2/everything", params=_build_params(query))
            data = resp.json()

            # NewsAPI returns status/code on error (e.g. developer plan restrictions)
            if data.get("status") == "error":
                err_code = data.get("code", "")
                err_msg  = data.get("message", "NewsAPI error")
                # "developerInactive" or "rateLimited" — try /v2/top-headlines as fallback
                if err_code in ("developerInactive", "rateLimited", "parameterInvalid"):
                    return {"source": "NewsAPI", "error": err_msg, "articles": [], "total_articles": 0}

            articles = data.get("articles", [])
            total    = data.get("totalResults", 0)

            # If strict query returns nothing, try the loose query
            if total == 0:
                resp2 = await client.get("https://newsapi.org/v2/everything", params=_build_params(loose_query))
                data2 = resp2.json()
                if data2.get("status") != "error":
                    articles = data2.get("articles", [])
                    total    = data2.get("totalResults", 0)

            # Filter out articles that don't actually mention the firm name
            firm_words = firm_name.lower().split()
            relevant = []
            for a in articles:
                text = ((a.get("title") or "") + " " + (a.get("description") or "")).lower()
                if any(w in text for w in firm_words):
                    relevant.append(a)
            # Use filtered count if we got results, else fall back to raw total
            if relevant:
                articles = relevant
                total    = max(total, len(relevant))

            risk_level = None
            if query_type == "negative":
                risk_level = "HIGH" if total > 5 else ("MEDIUM" if total > 1 else "LOW")

            return {
                "source": "NewsAPI",
                "variable": label,
                "total_articles": total,
                "period_days": days_back,
                "risk_level": risk_level,
                "articles": [
                    {
                        "title":       a.get("title", ""),
                        "source":      a.get("source", {}).get("name", ""),
                        "published":   a.get("publishedAt", ""),
                        "url":         a.get("url", ""),
                        "description": (a.get("description") or "")[:150],
                    }
                    for a in articles[:6]
                ],
            }
    except Exception as e:
        return {"source": "NewsAPI", "error": str(e), "articles": [], "total_articles": 0}


async def scrape_martindale_source(firm_name: str, **kwargs) -> dict:
    """
    Martindale-Hubbell scraper for AV Preeminent® status and attorney rating.
    Respectful scraping: 2s delay.
    """
    import asyncio as _asyncio
    await _asyncio.sleep(2)
    search_url = "https://www.martindale.com/find-attorneys/"
    params = {"q": firm_name, "type": "firm"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
            resp = await client.get(search_url, params=params)
            text = resp.text
            av_preeminent = bool(re.search(r"AV\s*Preeminent", text, re.I))
            rating = None
            rm = re.search(r'"ratingValue"\s*:\s*"?(\d+\.?\d*)"?', text)
            if rm:
                rating = float(rm.group(1))
            attorney_count = None
            cm = re.search(r'(\d+)\s+(?:attorney|lawyer)', text, re.I)
            if cm:
                attorney_count = int(cm.group(1))
            soup = BeautifulSoup(text, "html.parser")
            first_link = soup.select_one("a[href*='/law-firm/']")
            profile_url = ("https://www.martindale.com" + first_link["href"]) if first_link else None
            return {
                "source": "Martindale-Hubbell",
                "variable": "AV Preeminent® rating & firm profile",
                "av_preeminent": av_preeminent,
                "rating": rating,
                "attorney_count": attorney_count,
                "profile_url": profile_url,
                "search_url": str(resp.url),
            }
    except Exception as e:
        return {
            "source": "Martindale-Hubbell",
            "error": str(e),
            "av_preeminent": False,
            "rating": None,
            "attorney_count": None,
        }



async def scrape_avvo_via_courtlistener(firm_name: str, state: str) -> dict:
    """
    Replacement for direct Avvo scraping (which is JS-rendered/blocked).
    Uses CourtListener attorney search to find attorneys associated with the firm,
    then fetches their Avvo profiles via public search JSON (still works for individual lookups).
    Falls back to CourtListener attorney activity if Avvo is unavailable.
    """
    # First: find attorneys at this firm via CourtListener
    try:
        async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
            resp = await client.get(
                "https://www.courtlistener.com/api/rest/v4/attorneys/",
                params={"name": firm_name, "page_size": 10},
            )
            attorneys = resp.json().get("results", []) if resp.status_code == 200 else []

        if attorneys:
            return {
                "source": "CourtListener (Attorney Search)",
                "variable": "Attorney profiles from federal court records",
                "attorney_count": len(attorneys),
                "attorney_names": [a.get("name", "") for a in attorneys[:5]],
                "avvo_rating": None,
                "note": "Attorney data sourced from federal court filings via CourtListener",
            }
    except Exception as e:
        log.debug(f"CourtListener attorney search failed for {firm_name}: {e}")

    # Fallback: try Avvo JSON API (still works occasionally)
    try:
        async with httpx.AsyncClient(timeout=10, headers=HEADERS, follow_redirects=True) as client:
            resp = await client.get(
                "https://www.avvo.com/search/lawyer_search.json",
                params={"q": firm_name, "loc": state}
            )
            if resp.status_code == 200:
                data = resp.json()
                lawyers = data.get("lawyers", [])
                ratings = [float(l["rating"]) for l in lawyers if l.get("rating")]
                return {
                    "source": "Avvo",
                    "avvo_rating": round(sum(ratings)/len(ratings), 1) if ratings else None,
                    "avvo_attorney_count": len(lawyers),
                }
    except Exception:
        pass

    return {
        "source": "Avvo/CourtListener",
        "avvo_rating": None,
        "attorney_count": None,
        "note": "Rating data unavailable — Avvo requires JavaScript rendering",
    }

# ─────────────────────────────────────────────────────────────
# SCRAPER REGISTRY — maps source_key → coroutine factory
# Used by the per-source scrape API endpoint.
# ─────────────────────────────────────────────────────────────

SCRAPER_REGISTRY = {
    # Existing scrapers (wrapped for uniform signature)
    "courtlistener": lambda n, city, state, website, founder: scrape_courtlistener(n, state),
    "google_places":  lambda n, city, state, website, founder: scrape_google_places(n, city, state),
    "avvo":           lambda n, city, state, website, founder: scrape_avvo_via_courtlistener(n, state),
    "firm_website":   lambda n, city, state, website, founder: scrape_firm_website(website or ""),
    "state_bar":      lambda n, city, state, website, founder: scrape_state_bar(founder or n, state),
    "linkedin":       lambda n, city, state, website, founder: scrape_linkedin_signals(n),
    # New scrapers
    "sec_edgar":      lambda n, city, state, website, founder: scrape_sec_edgar_source(n),
    "news_press":     lambda n, city, state, website, founder: scrape_news_source(n, "press", 7),
    "news_wins":      lambda n, city, state, website, founder: scrape_news_source(n, "wins", 30),
    "news_negative":  lambda n, city, state, website, founder: scrape_news_source(n, "negative", 30),
    "news_laterals":  lambda n, city, state, website, founder: scrape_news_source(n, "laterals", 30),
    "martindale":     lambda n, city, state, website, founder: scrape_martindale_source(n),
}

SCRAPER_LABELS = {
    "courtlistener": ("CourtListener",       "Federal case filings & dockets",    "Monthly"),
    "google_places":  ("Google Places API",   "Star rating & review count",        "Monthly"),
    "avvo":           ("Avvo",                "Attorney rating (1–10 scale)",      "Monthly"),
    "firm_website":   ("Firm Website",        "Headcount, CRM, practice areas",   "Quarterly"),
    "state_bar":      ("State Bar",           "Bar admission year & discipline",   "Monthly"),
    "linkedin":       ("LinkedIn",            "Headcount & hiring signals",        "Monthly"),
    "sec_edgar":      ("SEC EDGAR",           "M&A transactions advised",          "Monthly"),
    "news_press":     ("NewsAPI",             "Total press mention count",         "Weekly"),
    "news_wins":      ("NewsAPI",             "Notable case wins / verdicts",      "Weekly"),
    "news_negative":  ("NewsAPI",             "Negative press / controversy",      "Weekly"),
    "news_laterals":  ("NewsAPI",             "Lateral hires & departures",        "Monthly"),
    "martindale":     ("Martindale-Hubbell",  "AV Preeminent® status & rating",   "Quarterly"),
}



async def scrape_manta_revenue(firm_name: str, city: str, state: str) -> dict:
    """
    Scrapes Manta.com for annual revenue listed on the firm's business profile.
    Manta aggregates public business data and lists revenue ranges for most
    registered US businesses, including small law firms.
    Returns: { revenue_raw, revenue_low, revenue_high, manta_url, confidence }
    """
    from urllib.parse import quote_plus
    query = quote_plus(f"{firm_name} {city} {state}")
    search_url = f"https://www.manta.com/search?search_source=nav&q={query}&pt=all"

    REVENUE_PATTERNS = [
        # "$1.5 million", "$500,000", "$2.3M"
        (r'\$\s*([\d,]+(?:\.\d+)?)\s*(?:million|mil\.?|M)', 1_000_000),
        (r'\$\s*([\d,]+(?:\.\d+)?)\s*(?:thousand|K)',        1_000),
        (r'\$\s*([\d,]+(?:\.\d+)?)',                          1),
        # "1.5M", "500K" without dollar sign
        (r'([\d,]+(?:\.\d+)?)\s*M(?:illion)?',             1_000_000),
        (r'([\d,]+(?:\.\d+)?)\s*K',                        1_000),
    ]

    try:
        async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
            # Search results page
            resp = await client.get(search_url)
            soup = BeautifulSoup(resp.text, "html.parser")

            # Find first result that matches firm name
            results = soup.select(".search-results .col-sm-8, article.search-result, .company-info, [class*='result']")
            profile_url = None

            # Try to find a direct link to firm profile
            for link in soup.select("a[href*='/c/']"):
                href = link.get("href", "")
                link_text = link.get_text(" ", strip=True).lower()
                if any(w.lower() in link_text for w in firm_name.split()[:2] if len(w) > 3):
                    profile_url = "https://www.manta.com" + href if href.startswith("/") else href
                    break

            # If no profile found, try searching directly
            if not profile_url:
                # Try direct company name search
                slug = firm_name.lower().replace(" ", "-").replace(",", "").replace(".", "").replace("&", "and")
                profile_url = f"https://www.manta.com/search?search_source=nav&q={query}"

            # Scrape the search result page for revenue
            page_text = soup.get_text(" ", strip=True)

            # Look for revenue in search result snippets
            revenue_raw = None
            for section in soup.select("[class*='revenue'], [class*='annual'], [class*='sales']"):
                revenue_raw = section.get_text(strip=True)
                if revenue_raw:
                    break

            # Also scan page text for revenue patterns near firm-name context
            if not revenue_raw:
                name_idx = page_text.lower().find(firm_name.split()[0].lower())
                if name_idx >= 0:
                    window = page_text[name_idx:name_idx+500]
                    rev_idx = window.lower().find("annual revenue")
                    if rev_idx >= 0:
                        revenue_raw = window[rev_idx:rev_idx+50]

            # Parse revenue value
            if revenue_raw:
                for pattern, multiplier in REVENUE_PATTERNS:
                    m = re.search(pattern, revenue_raw, re.I)
                    if m:
                        val = float(m.group(1).replace(",", "")) * multiplier
                        # Sanity check: law firms $100K–$500M
                        if 100_000 <= val <= 500_000_000:
                            return {
                                "revenue_raw":  revenue_raw.strip()[:60],
                                "revenue_mid":  int(val),
                                "revenue_low":  int(val * 0.7),
                                "revenue_high": int(val * 1.4),
                                "manta_url":    profile_url or search_url,
                                "confidence":   "medium",
                                "source":       "Manta.com",
                            }

            # If we got to the profile page, try there too
            if profile_url and "manta.com/c/" in (profile_url or ""):
                resp2 = await client.get(profile_url)
                soup2 = BeautifulSoup(resp2.text, "html.parser")
                for el in soup2.select("[class*='revenue'], dt, dd, li, span"):
                    txt = el.get_text(strip=True)
                    if "revenue" in txt.lower() or "annual" in txt.lower():
                        for pattern, multiplier in REVENUE_PATTERNS:
                            m = re.search(pattern, txt, re.I)
                            if m:
                                val = float(m.group(1).replace(",", "")) * multiplier
                                if 100_000 <= val <= 500_000_000:
                                    return {
                                        "revenue_raw":  txt[:60],
                                        "revenue_mid":  int(val),
                                        "revenue_low":  int(val * 0.7),
                                        "revenue_high": int(val * 1.4),
                                        "manta_url":    profile_url,
                                        "confidence":   "high",
                                        "source":       "Manta.com (profile)",
                                    }
    except Exception as e:
        log.debug(f"Manta revenue scrape failed for {firm_name}: {e}")

    return {}


async def scrape_spyfu_adspend(domain: str) -> dict:
    """
    Scrapes SpyFu's public overview page for estimated monthly Google Ads spend.
    SpyFu is free for public domain overviews (no API key needed).

    Revenue proxy formula for PI law firms:
        Annual ad spend = monthly_spend × 12
        Estimated revenue = annual_ad_spend ÷ 0.10
        (PI firms typically spend 8–15% of revenue on advertising)

    Returns: { monthly_adspend, annual_adspend, revenue_est, confidence, source }
    """
    if not domain:
        return {}

    clean_domain = domain.replace("www.", "").strip("/")
    url = f"https://www.spyfu.com/overview/domain?query={clean_domain}"

    SPEND_PATTERNS = [
        (r'\$\s*([\d,]+(?:\.\d+)?)\s*(?:million|mil\.?|M)', 1_000_000),
        (r'\$\s*([\d,]+(?:\.\d+)?)\s*(?:thousand|K)',        1_000),
        (r'\$\s*([\d,]+(?:\.\d+)?)',                          1),
    ]

    try:
        hdrs = {**HEADERS, "Accept": "text/html,application/xhtml+xml"}
        async with httpx.AsyncClient(timeout=15, headers=hdrs, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {}

            soup = BeautifulSoup(resp.text, "html.parser")
            text = soup.get_text(" ", strip=True)

            # SpyFu shows "Monthly Ad Budget", "Paid Clicks", "Google Ads"
            monthly_spend = None
            for section in soup.select("[class*='budget'], [class*='adspend'], [class*='paid'], [class*='monthly']"):
                t = section.get_text(strip=True)
                if any(kw in t.lower() for kw in ["budget", "spend", "ads", "paid"]):
                    for pattern, mult in SPEND_PATTERNS:
                        m = re.search(pattern, t, re.I)
                        if m:
                            val = float(m.group(1).replace(",", "")) * mult
                            if 100 <= val <= 10_000_000:  # $100 to $10M/month
                                monthly_spend = int(val)
                                break

            # Fallback: scan full text for spend near keywords
            if not monthly_spend:
                for kw in ["monthly ad budget", "google ads budget", "paid search budget"]:
                    idx = text.lower().find(kw)
                    if idx >= 0:
                        window = text[idx:idx+80]
                        for pattern, mult in SPEND_PATTERNS:
                            m = re.search(pattern, window, re.I)
                            if m:
                                val = float(m.group(1).replace(",", "")) * mult
                                if 100 <= val <= 10_000_000:
                                    monthly_spend = int(val)
                                    break

            if monthly_spend and monthly_spend > 0:
                annual_spend = monthly_spend * 12
                # PI firms: 8–12% of revenue on ads. Use 10% midpoint.
                revenue_est = annual_spend / 0.10
                return {
                    "monthly_adspend": monthly_spend,
                    "annual_adspend":  annual_spend,
                    "revenue_est":     int(revenue_est),
                    "revenue_low":     int(revenue_est * 0.7),
                    "revenue_high":    int(revenue_est * 1.4),
                    "spyfu_url":       url,
                    "confidence":      "medium",
                    "source":          "SpyFu (ad spend proxy)",
                }
    except Exception as e:
        log.debug(f"SpyFu scrape failed for {domain}: {e}")

    return {}


async def _estimate_firm_revenue(firm_name: str, website: str,
                                  city: str = "", state: str = "") -> dict:
    """
    Multi-source revenue estimator. Sources in priority order:
      1. Manta.com  — direct revenue listing (best free source)
      2. SpyFu      — Google Ads spend proxy (accurate for PI firms)
      3. Website scrape → attorney count × PI benchmark
      4. Name-pattern fallback

    Returns dict with revenue_low, revenue_high, revenue_label, revenue_source.
    """
    import asyncio as _aio

    # Get domain for SpyFu
    domain = ""
    if website:
        try:
            from urllib.parse import urlparse
            domain = urlparse(website if website.startswith("http") else "https://"+website).netloc.replace("www.","")
        except Exception:
            pass

    # Run Manta + SpyFu concurrently
    manta_result, spyfu_result = await _aio.gather(
        scrape_manta_revenue(firm_name, city, state),
        scrape_spyfu_adspend(domain),
        return_exceptions=True
    )
    if isinstance(manta_result, Exception): manta_result = {}
    if isinstance(spyfu_result, Exception): spyfu_result = {}

    def fmt(n):
        if n >= 1_000_000:
            return f"${n/1_000_000:.1f}M"
        return f"${n//1000}K"

    # ── Priority 1: Manta direct revenue ──────────────────────
    if manta_result.get("revenue_mid"):
        low  = manta_result["revenue_low"]
        high = manta_result["revenue_high"]
        return {
            "attorney_count_est": None,
            "revenue_low":    low,
            "revenue_high":   high,
            "revenue_label":  f"{fmt(low)}–{fmt(high)}/yr",
            "revenue_source": manta_result["source"],
            "revenue_raw":    manta_result.get("revenue_raw", ""),
            "adspend_monthly": spyfu_result.get("monthly_adspend"),
        }

    # ── Priority 2: SpyFu ad-spend proxy ──────────────────────
    if spyfu_result.get("revenue_est"):
        low  = spyfu_result["revenue_low"]
        high = spyfu_result["revenue_high"]
        return {
            "attorney_count_est": None,
            "revenue_low":    low,
            "revenue_high":   high,
            "revenue_label":  f"{fmt(low)}–{fmt(high)}/yr",
            "revenue_source": spyfu_result["source"],
            "adspend_monthly": spyfu_result["monthly_adspend"],
            "adspend_note":    f"Based on ~${spyfu_result['monthly_adspend']:,}/mo Google Ads spend",
        }

    # ── Priority 3: Website → attorney count benchmark ────────
    attorney_count = None
    rev_source = "benchmark"

    if website:
        try:
            ws_url = website if website.startswith("http") else "https://" + website
            async with httpx.AsyncClient(timeout=10, headers=HEADERS, follow_redirects=True) as client:
                resp = await client.get(ws_url)
                soup = BeautifulSoup(resp.text, "html.parser")
                text = soup.get_text(" ", strip=True).lower()
                attorney_count = _extract_attorney_count(soup, text)
                if attorney_count:
                    rev_source = "website (attorney count)"
        except Exception:
            pass

    # ── Priority 4: Name-pattern fallback ────────────────────
    if not attorney_count:
        name_lower = firm_name.lower()
        if any(x in name_lower for x in ["group", "partners", "associates", "& associates"]):
            attorney_count = 5
        elif any(x in name_lower for x in ["llp", "law firm", "legal group"]):
            attorney_count = 4
        else:
            attorney_count = 2
        rev_source = "name inference (low confidence)"

    # PI benchmark: $500K–$800K per attorney (conservative)
    REV_PER_ATTORNEY = 550_000
    mid  = attorney_count * REV_PER_ATTORNEY
    low  = int(mid * 0.55)
    high = int(mid * 1.45)

    return {
        "attorney_count_est": attorney_count,
        "revenue_low":    low,
        "revenue_high":   high,
        "revenue_label":  f"Est. {fmt(low)}–{fmt(high)}/yr",
        "revenue_source": rev_source,
        "adspend_monthly": spyfu_result.get("monthly_adspend"),
    }



# ─────────────────────────────────────────────────────────────
# FIRM DISCOVERY — surface new acquisition targets by market
# ─────────────────────────────────────────────────────────────

PRACTICE_AREA_MAP = {
    "personal injury":      "personal-injury",
    "auto accident":        "auto-accident",
    "workers compensation": "workers-comp",
    "immigration":          "immigration",
    "bankruptcy":           "bankruptcy",
    "criminal defense":     "criminal-defense",
    "family law":           "family",
    "medical malpractice":  "medical-malpractice",
    "employment law":       "employment",
    "real estate":          "real-estate",
    "business law":         "business",
    "estate planning":      "estate-planning",
    "civil litigation":     "civil-rights",
    "social security":      "social-security-disability",
}


async def _discover_google_places(city: str, state: str, practice_area: str,
                                   api_key: str, max_results: int = 20) -> list:
    """Google Places text search + detail fetch for each result."""
    query = f"{practice_area} law firm {city} {state}"
    search_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    detail_url = "https://maps.googleapis.com/maps/api/place/details/json"

    results = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(search_url, params={
                "query": query, "type": "lawyer", "key": api_key,
            })
            data = resp.json()
            places = data.get("results", [])[:max_results]

            for place in places:
                place_id = place.get("place_id")
                det_resp = await client.get(detail_url, params={
                    "place_id": place_id,
                    "fields": "name,rating,user_ratings_total,website,"
                              "formatted_phone_number,formatted_address",
                    "key": api_key,
                })
                det = det_resp.json().get("result", {})
                addr = det.get("formatted_address", "")
                # Parse city/state from address string
                addr_parts = addr.split(",")
                result_city = addr_parts[1].strip() if len(addr_parts) > 1 else city
                results.append({
                    "name":               det.get("name", place.get("name", "")),
                    "city":               result_city,
                    "state":              state,
                    "address":            addr,
                    "phone":              det.get("formatted_phone_number", ""),
                    "website":            det.get("website", ""),
                    "google_stars":       det.get("rating", 0),
                    "google_review_count": det.get("user_ratings_total", 0),
                    "rating_source":       "Google",
                    "practice_areas":     [practice_area],
                    "source":             "Google Places",
                })
    except Exception as e:
        log.warning(f"Google Places discovery failed: {e}")
    return results


async def _discover_serp(city: str, state: str, practice_area: str,
                          api_key: str, max_results: int = 20) -> list:
    """SerpAPI Google Maps search — paginates to get up to 40 results when max_results > 20."""
    query = f"{practice_area} law firm {city} {state}"
    url = "https://serpapi.com/search.json"
    results = []
    # Google Maps returns ~20 per page; fetch page 2 if we need more
    offsets = [0] if max_results <= 20 else [0, 20]
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            for start in offsets:
                if len(results) >= max_results:
                    break
                resp = await client.get(url, params={
                    "engine": "google_maps", "q": query,
                    "api_key": api_key, "type": "search", "start": start,
                })
                data = resp.json()
                for place in data.get("local_results", []):
                    if len(results) >= max_results:
                        break
                    results.append({
                        "name":               place.get("title", ""),
                        "city":               city,
                        "state":              state,
                        "address":            place.get("address", ""),
                        "phone":              place.get("phone", ""),
                        "website":            place.get("website", ""),
                        "google_stars":       place.get("rating", 0),
                        "google_review_count": place.get("reviews", 0),
                        "rating_source":       "Google",
                    "practice_areas":     [practice_area],
                    "source":             "SerpAPI",
                })
    except Exception as e:
        log.warning(f"SerpAPI discovery failed: {e}")
    return results


async def scrape_google_serpapi(firm_name: str, city: str, state: str) -> dict:
    """
    SerpAPI Google Maps — real Google rating + review count for a single firm.
    Uses engine=google_maps, returns the top result.
    """
    api_key = os.getenv("SERP_API_KEY", "")
    if not api_key:
        return {"google_stars": None, "google_review_count": None, "rating_source": None}
    query = f"{firm_name} {city} {state}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get("https://serpapi.com/search.json", params={
                "engine":  "google_maps",
                "q":       query,
                "api_key": api_key,
                "type":    "search",
                "num":     1,
            })
            if resp.status_code != 200:
                return {"google_stars": None, "google_review_count": None, "rating_source": None}
            data = resp.json()
            results = data.get("local_results", [])
            if results:
                place = results[0]
                return {
                    "google_stars":        round(float(place.get("rating") or 0), 1),
                    "google_review_count": int(place.get("reviews") or 0),
                    "rating_source":       "Google",
                    "website":             place.get("website", ""),
                }
    except Exception as e:
        log.warning(f"SerpAPI single-firm lookup failed: {e}")
    return {"google_stars": None, "google_review_count": None, "rating_source": None}


async def scrape_google_outscraper(firm_name: str, city: str, state: str) -> dict:
    """
    Outscraper Google Maps — returns real Google rating + review count for a single firm.
    Used for per-firm rating enrichment.
    """
    api_key = os.getenv("OUTSCRAPER_API_KEY", "")
    if not api_key:
        return {"google_stars": None, "google_review_count": None, "rating_source": None}
    query = f"{firm_name} {city} {state}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                OUTSCRAPER_ENDPOINT,
                params={"query": query, "limit": 1, "async": "false", "fields": "name,rating,reviews,site,phone,full_address"},
                headers={"X-API-KEY": api_key},
            )
            if resp.status_code != 200:
                return {"google_stars": None, "google_review_count": None, "rating_source": None}
            data = resp.json()
            # data is [[{place_obj}]] — outer list = queries, inner list = results per query
            places = data.get("data", [])
            if places and len(places) > 0 and len(places[0]) > 0:
                place = places[0][0]
                return {
                    "google_stars":        round(float(place.get("rating") or 0), 1),
                    "google_review_count": int(place.get("reviews") or 0),
                    "rating_source":       "Google",
                }
    except Exception as e:
        log.warning(f"Outscraper single-firm lookup failed: {e}")
    return {"google_stars": None, "google_review_count": None, "rating_source": None}


async def _discover_outscraper(city: str, state: str, practice_area: str,
                                api_key: str, max_results: int = 20) -> list:
    """
    Outscraper Google Maps search — discovers law firms AND returns real Google
    rating + review count in one call. Replaces both Yelp (discovery) and
    Decodo (SERP rating scraping).
    Sign up free: https://outscraper.com ($3 free credit, no card required)
    """
    query = f"{practice_area} law firm {city} {state}"
    results = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                OUTSCRAPER_ENDPOINT,
                params={
                    "query":  query,
                    "limit":  min(max_results, 20),  # Outscraper Maps limit per query
                    "async":  "false",
                    "fields": "name,rating,reviews,site,phone,full_address,city,state,category",
                },
                headers={"X-API-KEY": api_key},
            )
            if resp.status_code != 200:
                log.warning(f"Outscraper discovery HTTP {resp.status_code}: {resp.text[:200]}")
                return []
            data = resp.json()
            places = data.get("data", [])
            if not places:
                return []
            for place in (places[0] if places else []):
                name = place.get("name", "").strip()
                if not name:
                    continue
                # Skip directories / aggregators
                if any(skip in name.lower() for skip in ["yelp", "avvo", "findlaw", "martindale", "justia"]):
                    continue
                addr = place.get("full_address", "")
                results.append({
                    "name":               name,
                    "city":               place.get("city", city),
                    "state":              place.get("state", state),
                    "address":            addr,
                    "phone":              place.get("phone", ""),
                    "website":            place.get("site", ""),
                    "google_stars":       round(float(place.get("rating") or 0), 1),
                    "google_review_count": int(place.get("reviews") or 0),
                    "rating_source":      "Google",
                    "practice_areas":     [practice_area],
                    "source":             "Outscraper",
                })
    except Exception as e:
        log.warning(f"Outscraper discovery failed: {e}")
    return results



async def _discover_yelp(city: str, state: str, practice_area: str,
                         api_key: str, max_results: int = 20) -> list:
    """
    Yelp Fusion API — free tier (500 req/day, no credit card).
    Get key at: https://www.yelp.com/developers/v3/manage_app
    Returns real local businesses with name, address, phone, stars, review count.
    """
    url = "https://api.yelp.com/v3/businesses/search"
    params = {
        "term":       f"{practice_area} law firm",
        "location":   f"{city}, {state}",
        "categories": "lawyers",
        "limit":      min(max_results, 50),  # Yelp API hard limit is 50
        "sort_by":    "review_count",
    }
    results = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                url, params=params,
                headers={"Authorization": f"Bearer {api_key}"}
            )
            data = resp.json()
            for biz in data.get("businesses", []):
                loc = biz.get("location", {})
                address = loc.get("address1", "")
                city_r  = loc.get("city", city)
                state_r = loc.get("state", state)
                cats    = [c["title"] for c in biz.get("categories", [])]
                results.append({
                    "name":               biz.get("display_phone", biz["name"]) and biz["name"],
                    "city":               city_r,
                    "state":              state_r,
                    "address":            f"{address}, {city_r}, {state_r}".strip(", "),
                    "phone":              biz.get("display_phone", ""),
                    "website":            biz.get("website_url") or "",  # real firm site (from /businesses/{id})
                    "google_stars":       biz.get("rating", 0),
                    "google_review_count": biz.get("review_count", 0),
                    "rating_source":       "Yelp",
                    "practice_areas":     cats if cats else [practice_area],
                    "source":             "Yelp",
                })
    except Exception as e:
        log.warning(f"Yelp discovery failed: {e}")
    return results


async def _discover_courtlistener(city: str, state: str, practice_area: str,
                                   max_results: int = 20) -> list:
    """
    CourtListener RECAP — completely free, no key required.
    Finds law firms that have actively filed federal cases in the given state,
    by searching docket party/attorney data.
    """
    # Map state abbreviation to CourtListener court codes
    STATE_COURTS = {
        "AL": ["almd","alnd","alsd"], "AK": ["akd"], "AZ": ["azd"],
        "AR": ["ared","arwd"], "CA": ["cacd","caed","cand","casd"],
        "CO": ["cod"], "CT": ["ctd"], "DE": ["ded"], "FL": ["flmd","flnd","flsd"],
        "GA": ["gamd","gand","gasd"], "HI": ["hid"], "ID": ["idd"],
        "IL": ["ilcd","ilnd","ilsd"], "IN": ["innd","insd"], "IA": ["iasd","iand"],
        "KS": ["ksd"], "KY": ["kyed","kywd"], "LA": ["laed","lamd","lawd"],
        "ME": ["med"], "MD": ["mdd"], "MA": ["mad"], "MI": ["mied","miwd"],
        "MN": ["mnd"], "MS": ["msnd","mssd"], "MO": ["moed","mowd"],
        "MT": ["mtd"], "NE": ["ned"], "NV": ["nvd"], "NH": ["nhd"],
        "NJ": ["njd"], "NM": ["nmd"], "NY": ["nycd","nyed","nynd","nysd"],
        "NC": ["nced","ncmd","ncwd"], "ND": ["ndd"], "OH": ["ohnd","ohsd"],
        "OK": ["oked","oknd","okwd"], "OR": ["ord"], "PA": ["paed","pamd","pawd"],
        "RI": ["rid"], "SC": ["scd"], "SD": ["sdd"], "TN": ["tned","tnmd","tnwd"],
        "TX": ["txed","txnd","txsd","txwd"], "UT": ["utd"], "VT": ["vtd"],
        "VA": ["vaed","vawd"], "WA": ["waed","wawd"], "WV": ["wved","wvnd"],
        "WI": ["wied","wiwd"], "WY": ["wyd"],
    }
    courts = STATE_COURTS.get(state.upper(), [])
    if not courts:
        return []

    results = []
    seen_firms: set = set()
    # Search recent dockets in the state's courts for practice-area keywords
    pa_term = practice_area.replace(" ", "+")
    court_filter = courts[0]  # use primary court for the state

    try:
        url = "https://www.courtlistener.com/api/rest/v4/dockets/"
        params = {
            "court": court_filter,
            "order_by": "-date_filed",
            "page_size": 50,
        }
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
            for docket in data.get("results", []):
                # Extract attorney/firm from case parties
                attorneys_url = docket.get("absolute_url", "")
                # Use case name as a proxy for involved firms
                case_name = docket.get("case_name", "")
                # Look for firm-like patterns: "Law", "LLP", "PA", "P.A.", "& Associates"
                firm_matches = re.findall(
                    r'([A-Z][a-zA-Z]+(?:\s+[A-Z&][a-zA-Z]*)*'
                    r'\s+(?:Law|LLP|LLC|PA|P\.A\.|PC|P\.C\.|& Associates|Legal))',
                    case_name
                )
                for firm in firm_matches:
                    norm = re.sub(r"[^a-z0-9]", "", firm.lower())
                    if norm in seen_firms or len(firm) < 5:
                        continue
                    seen_firms.add(norm)
                    results.append({
                        "name":               firm.strip(),
                        "city":               city,
                        "state":              state,
                        "address":            f"{city}, {state}",
                        "phone":              "",
                        "website":            "",
                        "google_stars":       0,
                        "google_review_count": 0,
                        "practice_areas":     [practice_area],
                        "source":             "CourtListener",
                        "notes":              f"Active filer in {court_filter.upper()}",
                    })
                if len(results) >= max_results:
                    break
    except Exception as e:
        log.warning(f"CourtListener discovery failed: {e}")
    return results


async def _discover_yellowpages(city: str, state: str, practice_area: str,
                                 max_results: int = 20) -> list:
    """
    Yellow Pages public search — no key required.
    More bot-friendly than Avvo/Martindale.
    """
    pa_slug = practice_area.replace(" ", "+") + "+attorney"
    city_slug = city.lower().replace(" ", "-")
    state_slug = state.lower()
    url = (f"https://www.yellowpages.com/search"
           f"?search_terms={pa_slug}&geo_location_terms={city}%2C+{state}")

    results = []
    try:
        hdrs = {**HEADERS,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"}
        async with httpx.AsyncClient(timeout=15, headers=hdrs, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []
            soup = BeautifulSoup(resp.text, "html.parser")

            for card in soup.select(".result")[:min(max_results, 60)]:
                name_el = card.select_one(".business-name span, h2.n a")
                if not name_el:
                    continue
                name = name_el.get_text(strip=True)
                # Filter: must look like a law firm
                if not re.search(r'law|attorney|legal|llp|p\.?a\.?|counsel', name, re.I):
                    continue

                addr_el  = card.select_one(".street-address")
                phone_el = card.select_one(".phones")
                phone    = phone_el.get_text(strip=True) if phone_el else ""

                rating_el = card.select_one(".result-rating")
                stars = 0.0
                if rating_el:
                    m = re.search(r'(\d+\.?\d*)', rating_el.get("class", [""])[0] + rating_el.get_text())
                    if m:
                        stars = float(m.group(1))

                rev_el = card.select_one(".count")
                reviews = 0
                if rev_el:
                    m = re.search(r'(\d+)', rev_el.get_text())
                    if m:
                        reviews = int(m.group(1))

                address = addr_el.get_text(strip=True) if addr_el else f"{city}, {state}"
                results.append({
                    "name":               name,
                    "city":               city,
                    "state":              state,
                    "address":            address,
                    "phone":              phone,
                    "website":            "",
                    "google_stars":       stars,
                    "google_review_count": reviews,
                    "practice_areas":     [practice_area],
                    "source":             "Yellow Pages",
                })
    except Exception as e:
        log.warning(f"Yellow Pages discovery failed: {e}")
    return results



async def _discover_duckduckgo(city: str, state: str, practice_area: str,
                                max_results: int = 20) -> list:
    """
    Searches DuckDuckGo for law firms in a given market.
    Two-stage approach:
      1. Try the duckduckgo-search library (DDGS) — most reliable
      2. Fall back to scraping DuckDuckGo's HTML endpoint directly (no library needed)

    Extracts firm name, website, snippet from search results, then
    does a quick website scrape for phone/address details.
    No API key required.
    """
    query = f'"{practice_area}" law firm {city} {state} attorney site:.com -indeed -linkedin -yelp'
    results = []
    seen_domains: set = set()

    LAW_FIRM_RE = re.compile(
        r"[A-Z][a-zA-Z&-]+(?:\s+[A-Z&][a-zA-Z&-]*){0,4}"
        r"\s+(?:Law|Legal|LLP|LLC|PA|P\.A\.|PC|P\.C\.|Associates?|Attorneys?|Lawyers?|Trial\s+Lawyers?|Injury\s+Law)",
        re.IGNORECASE
    )

    raw_hits = []   # list of {title, url, snippet}

    # ── Stage 1: duckduckgo-search library ──────────────────────
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=min(max_results * 2, 40)):
                raw_hits.append({
                    "title":   r.get("title", ""),
                    "url":     r.get("href", ""),
                    "snippet": r.get("body", ""),
                })
        log.info(f"DuckDuckGo (library): {len(raw_hits)} hits for '{query}'")
    except Exception as e:
        log.debug(f"DDGS library failed, trying HTML fallback: {e}")

    # ── Stage 2: Direct HTML fallback ──────────────────────────
    if not raw_hits:
        try:
            from urllib.parse import quote_plus
            post_data = f"q={quote_plus(query)}&b=&kl=us-en"
            hdrs = {
                **HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://duckduckgo.com/",
            }
            async with httpx.AsyncClient(timeout=15, headers=hdrs, follow_redirects=True) as client:
                resp = await client.post("https://html.duckduckgo.com/html/", content=post_data)
                soup = BeautifulSoup(resp.text, "html.parser")
                for div in soup.select(".result"):
                    title_el   = div.select_one(".result__a")
                    snippet_el = div.select_one(".result__snippet")
                    link_el    = div.select_one("a.result__a")
                    if not title_el:
                        continue
                    href = link_el.get("href", "") if link_el else ""
                    # DuckDuckGo wraps URLs — extract real URL
                    if "uddg=" in href:
                        from urllib.parse import unquote, parse_qs, urlparse as _up
                        qs = parse_qs(_up(href).query)
                        href = unquote(qs.get("uddg", [""])[0])
                    raw_hits.append({
                        "title":   title_el.get_text(strip=True),
                        "url":     href,
                        "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                    })
            log.info(f"DuckDuckGo (HTML): {len(raw_hits)} hits")
        except Exception as e:
            log.warning(f"DuckDuckGo HTML fallback failed: {e}")

    # ── Parse hits into firm records ────────────────────────────
    skip_domains = {
        "yelp.com", "avvo.com", "martindale.com", "findlaw.com",
        "justia.com", "lawyers.com", "yellowpages.com", "bing.com",
        "google.com", "facebook.com", "bbb.org", "superlawyers.com",
        "indeed.com", "linkedin.com", "nolo.com", "hg.org",
    }

    for hit in raw_hits:
        url     = hit["url"]
        title   = hit["title"]
        snippet = hit["snippet"]

        # Parse domain
        try:
            from urllib.parse import urlparse as _urlparse
            domain = _urlparse(url).netloc.replace("www.", "").lower()
        except Exception:
            domain = ""

        # Skip directories and job sites
        if not domain or any(s in domain for s in skip_domains):
            continue
        if domain in seen_domains:
            continue
        seen_domains.add(domain)

        # Extract firm name from title (strip " | City" suffixes, pipe separators)
        firm_name = re.split(r'\s*[\|\-–—]\s*', title)[0].strip()
        # Try regex extraction if title is a page title, not firm name
        m = LAW_FIRM_RE.search(firm_name) or LAW_FIRM_RE.search(snippet)
        if m:
            firm_name = m.group(0).strip()
        if len(firm_name) < 4 or len(firm_name) > 80:
            continue
        # Must look like a law firm
        if not re.search(r'law|legal|llp|p\.?a\.?|attorney|counsel|injury|trial',
                         firm_name + " " + snippet, re.I):
            continue

        results.append({
            "name":               firm_name,
            "city":               city,
            "state":              state,
            "address":            f"{city}, {state}",
            "phone":              "",
            "website":            f"https://{domain}" if domain else url,
            "google_stars":       0,
            "google_review_count": 0,
            "practice_areas":     [practice_area],
            "source":             "DuckDuckGo",
            "snippet":            snippet[:200] if snippet else "",
        })

        if len(results) >= max_results:
            break

    return results


# ─────────────────────────────────────────────────────────────
# STATE BAR DIRECTORY SCRAPERS — authoritative, no hallucination
# ─────────────────────────────────────────────────────────────

# Maps state abbreviation → (scraper_fn_name, bar_name)
STATE_BAR_REGISTRY = {
    "FL": "florida", "TX": "texas", "CA": "california",
    "NY": "new_york", "GA": "georgia", "IL": "illinois",
    "PA": "pennsylvania", "OH": "ohio", "NC": "north_carolina",
    "AZ": "arizona", "CO": "colorado", "WA": "washington",
}

def _midpoint(lo: int, hi: int) -> int:
    return (lo + hi) // 2

async def _scrape_florida_bar(city: str, practice_area: str, max_results: int = 30) -> list:
    """
    Scrapes the Florida Bar member directory (floridabar.org).
    Groups attorneys by firm name → real, licensed law firms only.
    Returns firms with: name, city, state, attorney_count, bar_admission_year,
                        address, phone, source="Florida Bar"
    """
    results = []
    seen_firms: dict = {}  # firm_name → aggregated data

    search_city = city.strip().title()
    url = "https://www.floridabar.org/directories/find-mbr/"
    params = {
        "city": search_city, "state": "FL",
        "eligible": "Y",   # eligible to practice
        "submit": "Search",
    }

    try:
        async with httpx.AsyncClient(timeout=20, headers={
            **HEADERS,
            "Referer": "https://www.floridabar.org/directories/find-mbr/",
        }) as client:
            resp = await client.get(url, params=params)

        soup = BeautifulSoup(resp.text, "html.parser")

        # Each result row in the FL Bar directory
        for row in soup.select("tr.members-list, tr[class*='member'], .member-row, tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            text = [c.get_text(strip=True) for c in cells]

            # Typical columns: Name | Bar# | City | County | Firm | Status
            # Try to find firm name and admission info
            firm_name = ""
            atty_name = ""
            addr      = ""
            phone     = ""
            bar_year  = None

            for cell in cells:
                t = cell.get_text(strip=True)
                # Bar number column — extract year from admission date nearby
                if re.match(r"^\d{6}$", t):
                    # Look for adjacent date
                    date_m = re.search(r"(\d{4})", row.get_text())
                    if date_m:
                        yr = int(date_m.group(1))
                        if 1960 <= yr <= 2024:
                            bar_year = yr

            # Firm column is usually 5th or contains "Law", "LLP", "PA", "P.A.", "&"
            for t in text:
                if any(w in t for w in ["LLP","LLC","PA","P.A.","PLLC","Law","Legal","Associates","Group","Partners","& "]):
                    if len(t) > 4 and t not in ("Law Firm", "Law Office"):
                        firm_name = t
                        break

            if not firm_name:
                continue

            # Deduplicate by firm name — count attorneys per firm
            key = firm_name.lower().strip()
            if key not in seen_firms:
                seen_firms[key] = {
                    "name":             firm_name,
                    "city":             search_city,
                    "state":            "FL",
                    "attorney_count":   0,
                    "bar_admission_years": [],
                    "source":           "Florida Bar",
                    "phone":            phone,
                }
            seen_firms[key]["attorney_count"] += 1
            if bar_year:
                seen_firms[key]["bar_admission_years"].append(bar_year)

    except Exception as e:
        log.warning(f"Florida Bar scrape failed: {e}")

    # Convert to list, compute earliest bar year (best M&A signal)
    for key, firm in seen_firms.items():
        years = firm.pop("bar_admission_years", [])
        firm["bar_admission_year"] = min(years) if years else None
        results.append(firm)
        if len(results) >= max_results:
            break

    return results


async def _scrape_texas_bar(city: str, practice_area: str, max_results: int = 30) -> list:
    """
    Scrapes Texas State Bar lawyer locator (texasbar.com).
    Groups by firm → returns real licensed firms.
    """
    results = []
    seen_firms: dict = {}

    url = "https://www.texasbar.com/AM/Template.cfm"
    params = {
        "Section": "Find_A_Lawyer",
        "city":    city.strip(),
        "state":   "TX",
        "template": "/Directories/MemberDirectorySearch/SearchResults.cfm",
    }

    try:
        async with httpx.AsyncClient(timeout=20, headers={**HEADERS, "Referer":"https://www.texasbar.com/"}) as client:
            resp = await client.get(url, params=params)

        soup = BeautifulSoup(resp.text, "html.parser")

        for row in soup.select("tr, .result-row, .attorney-result"):
            cells = row.find_all(["td"])
            if len(cells) < 2:
                continue
            row_text = row.get_text(" ", strip=True)

            firm_name = ""
            bar_year  = None

            for cell in cells:
                t = cell.get_text(strip=True)
                if any(w in t for w in ["LLP","LLC","PA","P.A.","PLLC","Law","Legal","Associates","Group","Partners","& "]):
                    if 4 < len(t) < 120:
                        firm_name = t
                        break

            year_m = re.search(r"\b(19[6-9]\d|20[0-2]\d)\b", row_text)
            if year_m:
                bar_year = int(year_m.group(1))

            if not firm_name:
                continue

            key = firm_name.lower().strip()
            if key not in seen_firms:
                seen_firms[key] = {
                    "name":              firm_name,
                    "city":              city.strip().title(),
                    "state":             "TX",
                    "attorney_count":    0,
                    "bar_admission_years": [],
                    "source":            "Texas Bar",
                }
            seen_firms[key]["attorney_count"] += 1
            if bar_year:
                seen_firms[key]["bar_admission_years"].append(bar_year)

    except Exception as e:
        log.warning(f"Texas Bar scrape failed: {e}")

    for key, firm in seen_firms.items():
        years = firm.pop("bar_admission_years", [])
        firm["bar_admission_year"] = min(years) if years else None
        results.append(firm)
        if len(results) >= max_results:
            break

    return results


async def _scrape_california_bar(city: str, practice_area: str, max_results: int = 30) -> list:
    """
    Scrapes California State Bar attorney search (calbar.ca.gov).
    """
    results = []
    seen_firms: dict = {}

    url = "https://apps.calbar.ca.gov/attorney/LicenseeSearch/QuickSearch"
    params = {"category": "firm", "value": city, "returnUrl": ""}

    try:
        async with httpx.AsyncClient(timeout=20, headers={**HEADERS, "Referer":"https://apps.calbar.ca.gov/"}) as client:
            resp = await client.get(url, params=params)

        soup = BeautifulSoup(resp.text, "html.parser")

        for row in soup.select("tr, .result"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            row_text = row.get_text(" ", strip=True)

            firm_name = ""
            for cell in cells:
                t = cell.get_text(strip=True)
                if any(w in t for w in ["LLP","LLC","PC","P.C.","Law","Legal","Associates","Group","& "]):
                    if 4 < len(t) < 120:
                        firm_name = t
                        break

            bar_year = None
            year_m = re.search(r"\b(19[6-9]\d|20[0-2]\d)\b", row_text)
            if year_m:
                bar_year = int(year_m.group(1))

            if not firm_name:
                continue

            key = firm_name.lower().strip()
            if key not in seen_firms:
                seen_firms[key] = {
                    "name":              firm_name,
                    "city":              city.strip().title(),
                    "state":             "CA",
                    "attorney_count":    0,
                    "bar_admission_years": [],
                    "source":            "California Bar",
                }
            seen_firms[key]["attorney_count"] += 1
            if bar_year:
                seen_firms[key]["bar_admission_years"].append(bar_year)

    except Exception as e:
        log.warning(f"California Bar scrape failed: {e}")

    for key, firm in seen_firms.items():
        years = firm.pop("bar_admission_years", [])
        firm["bar_admission_year"] = min(years) if years else None
        results.append(firm)
        if len(results) >= max_results:
            break

    return results


async def _scrape_new_york_bar(city: str, practice_area: str, max_results: int = 30) -> list:
    """
    Scrapes New York attorney registration via iapps.courts.state.ny.us.
    """
    results = []
    seen_firms: dict = {}

    url = "https://iapps.courts.state.ny.us/attorneyservices/search"
    payload = {"1": "Search", "firstName":"", "lastName":"", "firmName": "", "city": city, "state":"NY"}

    try:
        async with httpx.AsyncClient(timeout=20, headers={**HEADERS, "Referer":"https://iapps.courts.state.ny.us/"}) as client:
            resp = await client.post(url, data=payload)

        soup = BeautifulSoup(resp.text, "html.parser")

        for row in soup.select("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            row_text = row.get_text(" ", strip=True)

            firm_name = ""
            for cell in cells:
                t = cell.get_text(strip=True)
                if any(w in t for w in ["LLP","LLC","PC","P.C.","Law","Legal","Associates","Group","& "]):
                    if 4 < len(t) < 120:
                        firm_name = t
                        break

            bar_year = None
            year_m = re.search(r"\b(19[6-9]\d|20[0-2]\d)\b", row_text)
            if year_m:
                bar_year = int(year_m.group(1))

            if not firm_name:
                continue

            key = firm_name.lower().strip()
            if key not in seen_firms:
                seen_firms[key] = {
                    "name":              firm_name,
                    "city":              city.strip().title(),
                    "state":             "NY",
                    "attorney_count":    0,
                    "bar_admission_years": [],
                    "source":            "New York Bar",
                }
            seen_firms[key]["attorney_count"] += 1
            if bar_year:
                seen_firms[key]["bar_admission_years"].append(bar_year)

    except Exception as e:
        log.warning(f"New York Bar scrape failed: {e}")

    for key, firm in seen_firms.items():
        years = firm.pop("bar_admission_years", [])
        firm["bar_admission_year"] = min(years) if years else None
        results.append(firm)
        if len(results) >= max_results:
            break

    return results


async def _discover_statebar(city: str, state: str, practice_area: str,
                              max_results: int = 30) -> list:
    """
    Routes to the correct state bar scraper based on state abbreviation.
    Only real, licensed firms appear here — zero hallucination risk.
    """
    scraper_map = {
        "FL": _scrape_florida_bar,
        "TX": _scrape_texas_bar,
        "CA": _scrape_california_bar,
        "NY": _scrape_new_york_bar,
    }

    fn = scraper_map.get(state.upper())
    if not fn:
        log.info(f"No state bar scraper for {state} yet")
        return []

    try:
        firms = await fn(city, practice_area, max_results)
        log.info(f"State Bar ({state}): {len(firms)} firms found in {city}")
        return firms
    except Exception as e:
        log.warning(f"State bar scraper failed for {state}: {e}")
        return []


# ─────────────────────────────────────────────────────────────
# LEA RESEARCH DATABASE — firms from internal market mapping
# ─────────────────────────────────────────────────────────────

import functools

@functools.lru_cache(maxsize=1)
def _load_lea_firms() -> list:
    """Load pre-researched firms from LEA market mapping spreadsheet (cached)."""
    import json, pathlib
    db_path = pathlib.Path(__file__).parent / "lea_firms.json"
    if not db_path.exists():
        return []
    try:
        with open(db_path) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Could not load lea_firms.json: {e}")
        return []


def _discover_lea_firms(city: str, state: str, practice_area: str, max_results: int = 60) -> list:
    """
    Return LEA pre-researched firms matching the search city/state.
    Matches on state always; also tries city substring match for metro areas
    (e.g. searching 'Miami' returns firms tagged 'Miami' for Miami-Fort Lauderdale market).
    """
    all_firms = _load_lea_firms()
    city_norm  = city.strip().lower()
    state_norm = state.strip().upper()
    pa_norm    = practice_area.strip().lower()

    results = []
    for f in all_firms:
        # State must match
        if f.get("state", "").upper() != state_norm:
            continue
        # City: loose match — "chicago" matches "Chicago", "Chicago Metro", etc.
        firm_city = f.get("city", "").lower()
        firm_market = f.get("market", "").lower()
        city_match = (
            city_norm in firm_city or
            firm_city in city_norm or
            city_norm in firm_market
        )
        if not city_match:
            continue
        # Practice area: LEA firms are all PI, so match any PI-adjacent search
        pa_keywords = ["personal injury", "injury", "accident", "pi", "tort",
                       "wrongful death", "medical malpractice", "slip and fall"]
        pa_match = any(kw in pa_norm for kw in pa_keywords) or pa_norm == ""
        if not pa_match:
            continue

        results.append({
            "name":               f["name"],
            "city":               f["city"],
            "state":              f["state"],
            "address":            "",
            "phone":              "",
            "website":            "",
            "google_stars":       f.get("google_stars", 0),
            "google_review_count": f.get("google_review_count", 0),
            "rating_source":      f.get("rating_source"),
            "practice_areas":     f.get("practice_areas", ["personal injury"]),
            "source":             "LEA Research",
            "lea_rank":           f.get("rank", 0),
            "lea_notes":          f.get("notes", ""),
            "lea_market":         f.get("market", ""),
        })
        if len(results) >= max_results:
            break

    return results


async def discover_firms(city: str, state: str,
                          practice_area: str = "personal injury",
                          max_results: int = 20) -> dict:
    """
    Master discovery function. Sources in priority order:
      1. Google Places API   — best quality (requires GOOGLE_API_KEY, free $200 credit)
      2. Outscraper          — real Google Maps ratings (requires OUTSCRAPER_API_KEY, $3 free at outscraper.com)
      3. SerpAPI             — good quality (requires SERP_API_KEY, 100 free/month)
      4. Yelp Fusion API     — Yelp ratings fallback (requires YELP_API_KEY, free at yelp.com/developers)
      5. Yellow Pages        — free scrape, no key
      6. CourtListener       — free API, finds firms active in federal courts

    Recommended free key: https://outscraper.com (no card required, $3 credit = ~2000 Google Maps lookups)
    """
    import asyncio as _asyncio

    google_key      = os.getenv("GOOGLE_API_KEY", "")
    serp_key        = os.getenv("SERP_API_KEY", "")
    yelp_key        = os.getenv("YELP_API_KEY", "")
    outscraper_key  = os.getenv("OUTSCRAPER_API_KEY", "")

    sources_used = []
    tasks = {}

    # LEA pre-researched firms — always included, highest priority
    lea_firms = _discover_lea_firms(city, state, practice_area, max_results)
    if lea_firms:
        sources_used.append("LEA Research")

    if google_key:
        tasks["google"] = _discover_google_places(city, state, practice_area, google_key, max_results)
        sources_used.append("Google Places")
    if serp_key:
        tasks["serp"] = _discover_serp(city, state, practice_area, serp_key, max_results)
        sources_used.append("SerpAPI")
    if outscraper_key:
        tasks["outscraper"] = _discover_outscraper(city, state, practice_area, outscraper_key, max_results)
        sources_used.append("Outscraper")
    if yelp_key:
        tasks["yelp"] = _discover_yelp(city, state, practice_area, yelp_key, max_results)
        sources_used.append("Yelp")

    # State Bar — highest authority source (only real licensed firms)
    if state.upper() in STATE_BAR_REGISTRY:
        tasks["statebar"] = _discover_statebar(city, state, practice_area, max_results)
        sources_used.append(f"{state.upper()} State Bar")

    # Always run free sources
    tasks["duckduckgo"]     = _discover_duckduckgo(city, state, practice_area, max_results)
    tasks["yellowpages"]    = _discover_yellowpages(city, state, practice_area, max_results)
    tasks["courtlistener"]  = _discover_courtlistener(city, state, practice_area, max_results)
    sources_used.extend(["DuckDuckGo", "Yellow Pages", "CourtListener"])

    gathered = await _asyncio.gather(*tasks.values(), return_exceptions=True)
    all_results = list(lea_firms)   # LEA firms go first — highest priority
    for key, res in zip(tasks.keys(), gathered):
        if isinstance(res, Exception):
            log.warning(f"Discovery source '{key}' failed: {res}")
        elif isinstance(res, list):
            all_results.extend(res)

    # Deduplicate by normalised name
    seen, unique = set(), []
    for r in all_results:
        norm = re.sub(r"[^a-z0-9]", "", r["name"].lower())
        if norm and len(norm) > 3 and norm not in seen:
            seen.add(norm)
            unique.append(r)

    # Sort: state bar first (authoritative), then paid APIs, then free sources
    source_priority = {
        "LEA Research":  0,
        "FL State Bar": 1, "TX State Bar": 1, "CA State Bar": 1, "NY State Bar": 1,
        "GA State Bar": 1, "IL State Bar": 1, "PA State Bar": 1,
        "Google Places": 2, "SerpAPI": 2, "Outscraper": 3, "Yelp": 4,
        "DuckDuckGo": 5, "Yellow Pages": 6, "CourtListener": 7,
    }
    # Also match any "*State Bar*" pattern dynamically
    def _src_priority(src):
        if src == "LEA Research":
            return 0
        if "State Bar" in src or "Bar" in src:
            return 1
        return source_priority.get(src, 8)

    unique.sort(key=lambda x: (_src_priority(x.get("source", "")),
                               -(x.get("google_review_count") or 0)))

    top = unique[:max_results]

    # Enrich discovered firms with real Google ratings via Decodo (for firms missing ratings)
    import asyncio as _asyncio2
    _serp_key2       = os.getenv("SERP_API_KEY", "")
    _outscraper_key  = os.getenv("OUTSCRAPER_API_KEY", "")
    rating_tasks = []
    rating_idxs  = []
    for i, f in enumerate(top):
        if not f.get("google_stars"):          # only fetch if missing
            if _serp_key2:
                rating_tasks.append(scrape_google_serpapi(f["name"], f.get("city",""), f.get("state","")))
            elif _outscraper_key:
                rating_tasks.append(scrape_google_outscraper(f["name"], f.get("city",""), f.get("state","")))
            else:
                rating_tasks.append(scrape_google_decodo(f["name"], f.get("city",""), f.get("state","")))
            rating_idxs.append(i)
    if rating_tasks:
        rating_results = await _asyncio2.gather(*rating_tasks, return_exceptions=True)
        for idx, res in zip(rating_idxs, rating_results):
            if isinstance(res, dict) and res.get("google_stars"):
                top[idx].update(res)

    # Enrich with revenue estimates (run concurrently)
    rev_tasks = [_estimate_firm_revenue(f["name"], f.get("website", ""), f.get("city", ""), f.get("state", "")) for f in top]
    rev_results = await _asyncio2.gather(*rev_tasks, return_exceptions=True)
    for firm, rev in zip(top, rev_results):
        if isinstance(rev, dict):
            firm.update(rev)
        else:
            firm["revenue_label"] = "Private (undisclosed)"
            firm["revenue_source"] = "unknown"

    return {
        "results":       top,
        "total_found":   len(unique),
        "sources_used":  list(dict.fromkeys(sources_used)),
        "city":          city,
        "state":         state,
        "practice_area": practice_area,
    }


# ─────────────────────────────────────────────────────────────
# OUTREACH — Leader identification, email finding, draft
# ─────────────────────────────────────────────────────────────

LEADER_TITLES = [
    "founding partner", "managing partner", "managing member",
    "senior partner", "principal", "owner", "founder",
    "president", "chief executive", "ceo", "chairman",
    "lead attorney", "lead counsel", "head of firm",
]

async def scrape_firm_leadership(website: str, firm_name: str) -> dict:
    """
    Scrapes firm's website (homepage + /about + /team + /attorneys) to find
    the managing partner / founder / owner.
    Returns: { name, title, email (if visible), bio_snippet, source_url }
    """
    if not website:
        return {}
    base = website.rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base

    candidate_urls = [
        base,
        base + "/about",
        base + "/about-us",
        base + "/team",
        base + "/our-team",
        base + "/attorneys",
        base + "/lawyers",
        base + "/people",
    ]

    NAME_RE = re.compile(
        r'\b([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-zA-Z\'-]+(?:\s+(?:Jr|Sr|III|IV|Esq)\.?)?)\b'
    )
    EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')

    best: dict = {}

    try:
        async with httpx.AsyncClient(timeout=12, headers=HEADERS, follow_redirects=True) as client:
            for url in candidate_urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue
                    soup = BeautifulSoup(resp.text, "html.parser")
                    # Remove nav/footer noise
                    for tag in soup.select("nav, footer, script, style"):
                        tag.decompose()
                    text = soup.get_text(" ", strip=True)

                    # Look for title + name patterns
                    for title in LEADER_TITLES:
                        idx = text.lower().find(title)
                        if idx == -1:
                            continue
                        # Search 200 chars around the title for a proper name
                        window = text[max(0, idx-120):idx+200]
                        names = NAME_RE.findall(window)
                        # Filter out firm name words
                        firm_words = set(re.sub(r"[^a-z ]", "", firm_name.lower()).split())
                        names = [n for n in names if not all(
                            w.lower() in firm_words for w in n.split()
                        ) and len(n.split()) >= 2]
                        if names:
                            best = {
                                "name": names[0],
                                "title": title.title(),
                                "source_url": url,
                            }
                            # Also grab a short bio snippet
                            snippet_start = max(0, idx - 30)
                            best["bio_snippet"] = text[snippet_start:snippet_start+300].strip()
                            break

                    # Pick up any visible email on this page
                    if not best.get("email"):
                        found_emails = EMAIL_RE.findall(text)
                        # Prefer emails that aren't generic (info@, contact@, etc.)
                        generic = {"info", "contact", "hello", "admin", "support", "office", "mail"}
                        personal = [e for e in found_emails
                                    if e.split("@")[0].lower() not in generic
                                    and "example" not in e]
                        if personal:
                            best["email"] = personal[0]
                        elif found_emails:
                            best["email"] = found_emails[0]

                    if best.get("name"):
                        break  # found what we need

                except Exception:
                    continue
    except Exception as e:
        log.debug(f"Leadership scrape failed for {website}: {e}")

    return best


def _guess_email_patterns(first: str, last: str, domain: str) -> list[str]:
    """Generate common law firm email patterns to try."""
    f = first.lower()
    l = last.lower()
    return [
        f"{f}@{domain}",
        f"{f}.{l}@{domain}",
        f"{f[0]}{l}@{domain}",
        f"{f[0]}.{l}@{domain}",
        f"{f}_{l}@{domain}",
        f"{l}@{domain}",
        f"{l}.{f}@{domain}",
        f"{f}{l[0]}@{domain}",
    ]


def _verify_email_smtp(email: str, timeout: int = 5) -> bool:
    """
    SMTP handshake verification — checks if an email address exists
    without sending a message. Uses stdlib smtplib only.
    Returns True if server accepts the address, False otherwise.
    Note: some servers always return 250 (catch-all), so treat as probabilistic.
    """
    import smtplib, dns.resolver as _dns  # dns requires dnspython
    try:
        domain = email.split("@")[1]
        # Get MX record
        mx = str(list(_dns.resolve(domain, "MX"))[0].exchange).rstrip(".")
        with smtplib.SMTP(mx, 25, timeout=timeout) as smtp:
            smtp.ehlo("verify.lea.com")
            smtp.mail("verify@lea.com")
            code, _ = smtp.rcpt(email)
            return code == 250
    except Exception:
        return False


async def find_contact_email(
    name: str, domain: str, hunter_api_key: str = ""
) -> dict:
    """
    Find email for a person at a given domain.
    Strategy (in order):
      1. Hunter.io domain search API (best — finds real emails with confidence score)
      2. Pattern guessing + SMTP verification
    Returns: { email, confidence, method }
    """
    result = {"email": None, "confidence": 0, "method": "none", "all_emails": []}

    # ── 1. Hunter.io ────────────────────────────────────────────
    if hunter_api_key:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Domain search — gets all known emails at the domain
                r = await client.get(
                    "https://api.hunter.io/v2/domain-search",
                    params={"domain": domain, "api_key": hunter_api_key, "limit": 10}
                )
                data = r.json().get("data", {})
                emails_found = data.get("emails", [])
                result["all_emails"] = [
                    {"email": e["value"], "confidence": e.get("confidence", 0),
                     "first": e.get("first_name", ""), "last": e.get("last_name", "")}
                    for e in emails_found
                ]

                # Try to match to the leader name we found
                if name and emails_found:
                    name_parts = name.lower().split()
                    first_name = name_parts[0] if name_parts else ""
                    last_name  = name_parts[-1] if len(name_parts) > 1 else ""
                    for e in emails_found:
                        addr = e["value"].lower()
                        if first_name in addr or last_name in addr:
                            result.update({
                                "email": e["value"],
                                "confidence": e.get("confidence", 70),
                                "method": "hunter.io (name match)",
                            })
                            return result

                # If no name match, return highest confidence email
                if emails_found:
                    best = max(emails_found, key=lambda e: e.get("confidence", 0))
                    result.update({
                        "email": best["value"],
                        "confidence": best.get("confidence", 50),
                        "method": "hunter.io (top result)",
                    })
                    return result

                # Email finder — target specific person
                if name:
                    parts = name.split()
                    if len(parts) >= 2:
                        r2 = await client.get(
                            "https://api.hunter.io/v2/email-finder",
                            params={
                                "domain": domain,
                                "first_name": parts[0],
                                "last_name": parts[-1],
                                "api_key": hunter_api_key,
                            }
                        )
                        d2 = r2.json().get("data", {})
                        if d2.get("email"):
                            result.update({
                                "email": d2["email"],
                                "confidence": d2.get("score", 70),
                                "method": "hunter.io (email-finder)",
                            })
                            return result
        except Exception as e:
            log.debug(f"Hunter.io failed for {domain}: {e}")

    # ── 2. Pattern guessing (no external service needed) ────────
    if name and domain:
        parts = name.split()
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            patterns = _guess_email_patterns(first, last, domain)
            # Return top guesses without SMTP (SMTP often blocked in cloud)
            result.update({
                "email": patterns[0],
                "confidence": 35,
                "method": "pattern_guess",
                "guesses": patterns[:5],
            })

    return result


def generate_outreach_email(firm: dict, leader_name: str, practice_area: str = "personal injury") -> str:
    """
    Generate a personalised LEA outreach email for a firm's managing partner.
    Uses firm data (score, location, attorney count, etc.) for personalisation.
    """
    first_name = leader_name.split()[0] if leader_name else "there"
    firm_name  = firm.get("name", "your firm")
    city       = firm.get("city", "")
    state      = firm.get("state", "")
    location   = f"{city}, {state}" if city and state else (city or state or "your market")
    atty_count = firm.get("attorney_count", "")
    atty_str   = f"your {atty_count}-attorney firm" if atty_count else "your firm"
    score      = firm.get("composite_score")
    score_line = (
        f"Based on our proprietary scoring model — which evaluates practice fit, "
        f"market position, brand quality, and growth trajectory — {firm_name} ranks "
        f"in the top tier of acquisition candidates in {location}."
    ) if score and score >= 70 else (
        f"We've been tracking high-quality {practice_area} firms in {location} "
        f"and {firm_name} stood out as a strong candidate."
    )

    return f"""Subject: Partnership Opportunity for {firm_name} — LEA Investment

Dear {first_name},

My name is [Your Name], and I'm reaching out from LEA Investment, a firm that specialises in partnering with and acquiring leading personal injury law firms across the United States.

{score_line}

We work exclusively with PI and litigation-focused firms like {atty_str}, providing capital, operational support, and a clear succession pathway for founding partners who are thinking about the next chapter — whether that's an exit, a growth partnership, or simply taking chips off the table while continuing to practise.

What we offer:
• Liquidity for founding partners without disrupting day-to-day operations
• Growth capital to expand headcount, marketing, and technology
• Access to our national network of PI firms and referral pipeline
• Flexible deal structures (full acquisition, minority stake, or management buyout)

We'd love to have a brief, confidential conversation to explore whether there's a fit. There's absolutely no obligation — just an open discussion about your goals for {firm_name}.

Would you have 20 minutes for a call this week or next?

Best regards,
[Your Name]
LEA Investment
[Phone] | [Email]
[LinkedIn]

---
This message is confidential and intended solely for {first_name} {leader_name.split()[-1] if len(leader_name.split()) > 1 else ''}. If you have received it in error, please disregard.
"""


# ─────────────────────────────────────────────────────────────
# 12. COURT ANALYTICS — Rank firms by attorney & case activity
#     via CourtListener federal docket data (free API)
# ─────────────────────────────────────────────────────────────

import asyncio as _asyncio_court

# Map city names → federal court district IDs used by CourtListener
CITY_TO_COURTS: dict[str, list[str]] = {
    # Florida
    "miami": ["flsd"], "miami beach": ["flsd"], "fort lauderdale": ["flsd"],
    "boca raton": ["flsd"], "west palm beach": ["flsd"], "pompano beach": ["flsd"],
    "coral gables": ["flsd"], "hialeah": ["flsd"], "hollywood": ["flsd"],
    "deerfield beach": ["flsd"], "boynton beach": ["flsd"], "homestead": ["flsd"],
    "tampa": ["flmd"], "orlando": ["flmd"], "st. petersburg": ["flmd"],
    "clearwater": ["flmd"], "sarasota": ["flmd"], "lakeland": ["flmd"],
    "gainesville": ["flmd"], "ocala": ["flmd"],
    "jacksonville": ["flnd"], "tallahassee": ["flnd"], "pensacola": ["flnd"],
    # Texas
    "houston": ["txsd"], "galveston": ["txsd"], "corpus christi": ["txsd"],
    "dallas": ["txnd"], "fort worth": ["txnd"], "amarillo": ["txnd"],
    "lubbock": ["txnd"],
    "austin": ["txwd"], "san antonio": ["txwd"], "el paso": ["txwd"], "waco": ["txwd"],
    "beaumont": ["txed"], "tyler": ["txed"],
    # California
    "los angeles": ["cacd"], "santa ana": ["cacd"], "riverside": ["cacd"],
    "long beach": ["cacd"], "santa barbara": ["cacd"],
    "san francisco": ["cand"], "san jose": ["cand"], "oakland": ["cand"],
    "san diego": ["casd"],
    "sacramento": ["caed"], "fresno": ["caed"],
    # New York
    "new york": ["nysd", "nyed"], "manhattan": ["nysd"],
    "brooklyn": ["nyed"], "queens": ["nyed"], "bronx": ["nysd"],
    "albany": ["nynd"], "buffalo": ["nywd"],
    # Other major metros
    "atlanta": ["gand"], "savannah": ["gasd"],
    "chicago": ["ilnd"], "springfield": ["ilcd"],
    "philadelphia": ["paed"], "pittsburgh": ["pawd"],
    "cleveland": ["ohnd"], "toledo": ["ohnd"], "columbus": ["ohsd"], "cincinnati": ["ohsd"],
    "phoenix": ["azd"], "tucson": ["azd"],
    "denver": ["cod"],
    "seattle": ["wawd"], "spokane": ["waed"],
    "charlotte": ["ncwd"], "raleigh": ["nced"],
    "nashville": ["tnmd"], "memphis": ["tnwd"],
    "las vegas": ["nvd"], "reno": ["nvd"],
    "minneapolis": ["mnd"],
    "st. louis": ["moed"], "kansas city": ["mow"],
    "baltimore": ["mdd"],
    "portland": ["ord"],
    "new orleans": ["laed"],
    "oklahoma city": ["okwd"], "tulsa": ["oknd"],
    "detroit": ["mied"], "grand rapids": ["miwd"],
    "boston": ["mad"],
    "richmond": ["vaed"], "norfolk": ["vaed"],
    "birmingham": ["alnd"], "mobile": ["alsd"],
}

STATE_DEFAULT_COURTS: dict[str, list[str]] = {
    "FL": ["flsd", "flmd"], "TX": ["txsd", "txnd"], "CA": ["cacd", "cand"],
    "NY": ["nysd", "nyed"], "GA": ["gand"], "IL": ["ilnd"],
    "PA": ["paed", "pawd"], "OH": ["ohnd", "ohsd"], "NC": ["ncwd"],
    "AZ": ["azd"], "CO": ["cod"], "WA": ["wawd"], "TN": ["tnmd"],
    "MI": ["mied"], "MO": ["moed"], "NV": ["nvd"], "OR": ["ord"],
    "MD": ["mdd"], "MA": ["mad"], "MN": ["mnd"], "LA": ["laed"],
    "OK": ["okwd"], "AL": ["alnd"], "SC": ["scd"], "VA": ["vaed"],
}

# Map practice area → nature-of-suit codes (CourtListener uses these)
PRACTICE_TO_NOS: dict[str, list[str]] = {
    "personal injury":      ["360", "362", "365", "367", "368", "370", "385", "388"],
    "auto accident":        ["360", "385"],
    "workers compensation": ["710", "720", "730", "740", "790"],
    "medical malpractice":  ["362"],
    "employment law":       ["440", "441", "442", "443", "444", "445", "446"],
    "civil litigation":     ["190", "195", "196"],
    "immigration":          ["462", "463", "465"],
    "criminal defense":     ["510", "530", "535", "540", "550", "555"],
    "social security":      ["861", "862", "863", "864", "865"],
    "real estate":          ["210", "220", "230", "240"],
    "business law":         ["190", "195", "196"],
    "bankruptcy":           [],   # separate bankruptcy courts
    "family law":           [],   # state court only
    "estate planning":      [],
}

NOS_LABELS: dict[str, str] = {
    "360": "Personal Injury",    "362": "Med Malpractice",   "365": "Product Liability",
    "367": "Healthcare/Pharma",  "368": "Asbestos PI",        "370": "Fraud",
    "385": "Property Damage",    "388": "Other PI",
    "440": "Civil Rights",       "441": "Employment",         "442": "Housing",
    "443": "ADA",                "444": "Welfare",            "445": "ADA–Employment",
    "446": "Education",
    "510": "Vacate Sentence",    "530": "Habeas Corpus",      "535": "Death Penalty",
    "540": "Mandamus",           "550": "Civil Rights (Prison)", "555": "Prison Condition",
    "710": "Fair Labor",         "720": "Labor/Mgmt",
    "861": "HIA",                "862": "Black Lung",         "863": "DIWC/DIWW",
    "864": "SSID",               "865": "RSI",
    "462": "Naturalization",     "463": "Habeas–Alien",       "465": "Other Immigration",
    "210": "Land Condemnation",  "220": "Foreclosure",
    "230": "Rent/Lease",         "240": "Torts to Land",
    "190": "Other Contracts",    "195": "Contract Liability", "196": "Franchise",
}


def _normalize_firm_name_court(name: str) -> str:
    """Lowercase + strip suffixes → stable grouping key."""
    if not name:
        return ""
    n = name.strip()
    for sfx in [
        ", P.A.", " P.A.", ", P.C.", " P.C.", ", PLLC", " PLLC",
        ", LLC", " LLC", ", LLP", " LLP", ", APC", " APC",
        ", PA", " PA", ", PC", " PC",
        " & Associates", " and Associates", " & Assoc.", " and Assoc.",
        ", Attorneys at Law", " Attorneys at Law",
        ", Attorney at Law", " Attorney at Law",
        ", Chartered", " Chartered", ", Esq.", " Esq.",
    ]:
        if n.endswith(sfx):
            n = n[:-len(sfx)].strip()
    n = re.sub(r"\s+&\s+", " & ", n)
    n = re.sub(r"\band\b", "&", n, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", n).strip().lower()


def _parse_firm_from_contact(contact_raw: str, attorney_name: str) -> str:
    """
    Extract firm name from CourtListener contact_raw block, e.g.:
        Jane Doe
        Law Offices of Jane Doe, P.A.
        123 Main Street, Suite 200
        Miami, FL 33131
        (305) 555-1234
    Returns the first non-attorney, non-address line — typically the firm name.
    """
    if not contact_raw:
        return ""
    lines = [l.strip() for l in contact_raw.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return ""

    addr_re = re.compile(
        r"\b(suite|ste|floor|ave|blvd|road|rd|street|dr|drive|lane|ln|"
        r"highway|hwy|pkwy|court|p\.o\.)\b"
        r"|\d{5}"                        # zip code
        r"|\(\d{3}\)\s*\d{3}"           # (305) 555
        r"|\d{3}[-.\s]\d{3}[-.\s]\d{4}" # 305-555-1234
        r"|@"                            # email
        r"|^\d+\s"                       # starts with street number
        r"|\b(fax|tel|phone|email)\b",
        re.IGNORECASE,
    )
    atty_lower = attorney_name.strip().lower()

    for line in lines:
        ll = line.lower()
        if ll == atty_lower:
            continue
        if addr_re.search(line):
            continue
        if len(line) < 5:
            continue
        return line.strip()          # first clean line = firm name
    return ""


def _normalize_attorney_name_court(name: str) -> str:
    """Remove title suffixes and lowercase for deduplication."""
    if not name:
        return ""
    for sfx in [", Esq.", " Esq.", ", J.D.", " J.D.", ", JD",
                ", Attorney", " Attorney", ", Esq", " Esq"]:
        if name.endswith(sfx):
            name = name[:-len(sfx)]
    return re.sub(r"\s+", " ", name).strip().lower()


async def analyze_court_data(
    city: str,
    state: str,
    practice_area: str = "personal injury",
    days_back: int = 180,
) -> dict:
    """
    Core court analytics algorithm.

    1. Maps city/state → federal court district(s)
    2. Queries CourtListener dockets filtered by nature-of-suit + date
    3. For each docket fetches party/attorney records
    4. Groups attorneys by firm (via contact_raw parsing + name normalisation)
    5. Returns firms ranked by active case count, with:
         - attorney_count_court  (unique attorneys on docket)
         - active_cases          (distinct case count in period)
         - cases_per_month       (filing velocity)
         - case_type_breakdown   (nature-of-suit breakdown)
         - monthly_trend         (filings per month dict)
         - courts                (district codes seen)
    """
    from datetime import timedelta

    city_key  = city.strip().lower()
    state_key = state.strip().upper()

    courts = CITY_TO_COURTS.get(city_key) or STATE_DEFAULT_COURTS.get(state_key, [])
    if not courts:
        return {
            "firms": [], "total_firms": 0, "total_cases_analyzed": 0,
            "courts_searched": [], "days_back": days_back,
            "city": city, "state": state, "practice_area": practice_area,
            "error": f"No federal court mapping found for {city}, {state}",
        }

    nos_codes  = PRACTICE_TO_NOS.get(practice_area.lower().strip(), ["360"])
    date_from  = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    cl_token = os.getenv("COURTLISTENER_TOKEN", "")
    req_headers = {**HEADERS}
    if cl_token:
        req_headers["Authorization"] = f"Token {cl_token}"

    firms: dict[str, dict] = {}
    total_dockets = 0

    async with httpx.AsyncClient(timeout=25, headers=req_headers, follow_redirects=True) as client:

        for court in courts[:2]:    # cap at 2 districts to stay within rate limits

            # ── Phase 1: collect docket IDs ──────────────────────────────
            docket_infos: list[dict] = []
            for nos in (nos_codes[:3] if nos_codes else [""]):
                params: dict = {
                    "court":           court,
                    "date_filed__gte": date_from,
                    "order_by":        "-date_filed",
                    "page_size":       "20",
                    "format":          "json",
                }
                if nos:
                    params["nature_of_suit"] = nos

                for page in range(1, 4):    # max 3 pages × 20 = 60 per NOS
                    params["page"] = str(page)
                    try:
                        r = await client.get(
                            "https://www.courtlistener.com/api/rest/v3/dockets/",
                            params=params,
                        )
                        if r.status_code != 200:
                            break
                        body = r.json()
                        for d in body.get("results", []):
                            docket_infos.append({
                                "id":            d["id"],
                                "date_filed":    d.get("date_filed", ""),
                                "nature_of_suit": str(d.get("nature_of_suit", "")),
                            })
                        if not body.get("next"):
                            break
                        await _asyncio_court.sleep(0.2)
                    except Exception as exc:
                        log.warning(f"CourtListener dockets {court}/{nos} p{page}: {exc}")
                        break

            # Deduplicate docket IDs
            seen_ids: set = set()
            unique_dockets = []
            for di in docket_infos:
                if di["id"] not in seen_ids:
                    seen_ids.add(di["id"])
                    unique_dockets.append(di)
            total_dockets += len(unique_dockets)

            # ── Phase 2: fetch parties for each docket (async batches) ───
            async def _get_parties(dinfo: dict) -> tuple[dict, list]:
                try:
                    r = await client.get(
                        "https://www.courtlistener.com/api/rest/v3/parties/",
                        params={"docket": dinfo["id"], "page_size": "50", "format": "json"},
                    )
                    if r.status_code == 200:
                        return dinfo, r.json().get("results", [])
                except Exception:
                    pass
                return dinfo, []

            for i in range(0, len(unique_dockets), 5):
                batch  = unique_dockets[i:i+5]
                combos = await _asyncio_court.gather(*[_get_parties(d) for d in batch])

                for dinfo, parties in combos:
                    nos_label = NOS_LABELS.get(dinfo["nature_of_suit"], dinfo["nature_of_suit"] or "Other")
                    month_key = (dinfo.get("date_filed") or "")[:7]

                    for party in parties:
                        for atty_role in party.get("attorneys", []):
                            atty_obj = atty_role.get("attorney") or {}
                            if isinstance(atty_obj, str):
                                continue    # URL ref — skip to avoid extra round-trips

                            atty_name_raw = atty_obj.get("name", "")
                            contact_raw   = atty_obj.get("contact_raw", "")

                            firm_display = _parse_firm_from_contact(contact_raw, atty_name_raw)
                            firm_key     = _normalize_firm_name_court(firm_display)
                            atty_norm    = _normalize_attorney_name_court(atty_name_raw)

                            if not firm_key or len(firm_key) < 4 or not atty_norm:
                                continue

                            if firm_key not in firms:
                                firms[firm_key] = {
                                    "display_name": firm_display,
                                    "attorneys":    set(),
                                    "case_ids":     set(),
                                    "case_types":   {},
                                    "courts":       set(),
                                    "monthly":      {},
                                }

                            firms[firm_key]["attorneys"].add(atty_norm)
                            firms[firm_key]["case_ids"].add(dinfo["id"])
                            firms[firm_key]["courts"].add(court.upper())
                            if nos_label:
                                firms[firm_key]["case_types"][nos_label] = \
                                    firms[firm_key]["case_types"].get(nos_label, 0) + 1
                            if month_key:
                                firms[firm_key]["monthly"][month_key] = \
                                    firms[firm_key]["monthly"].get(month_key, 0) + 1

                await _asyncio_court.sleep(0.3)     # polite rate-limiting

    # ── Phase 3: build ranked output ────────────────────────────────────────
    rows = []
    for fkey, data in firms.items():
        case_count  = len(data["case_ids"])
        atty_count  = len(data["attorneys"])
        months_seen = len(data["monthly"])
        cpm         = round(case_count / max(months_seen, 1), 1)
        top_types   = sorted(data["case_types"].items(), key=lambda x: x[1], reverse=True)
        monthly_sorted = dict(sorted(data["monthly"].items()))

        rows.append({
            "name":                  data["display_name"],
            "attorney_count_court":  atty_count,
            "active_cases":          case_count,
            "cases_per_month":       cpm,
            "courts":                sorted(data["courts"]),
            "top_case_types":        [t[0] for t in top_types[:3]],
            "case_type_breakdown":   {t[0]: t[1] for t in top_types[:5]},
            "monthly_trend":         monthly_sorted,
            "source":                "CourtListener",
            # Compatibility with FirmDetailPanel
            "city":              city,
            "state":             state,
            "practice_areas":    [practice_area],
            "website":           "",
            "phone":             "",
            "google_stars":      0,
            "google_review_count": 0,
        })

    rows.sort(key=lambda x: (x["active_cases"], x["attorney_count_court"]), reverse=True)

    return {
        "firms":                rows,
        "total_firms":          len(rows),
        "total_cases_analyzed": total_dockets,
        "courts_searched":      courts,
        "days_back":            days_back,
        "city":                 city,
        "state":                state,
        "practice_area":        practice_area,
    }
