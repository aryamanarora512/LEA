"""
LEA AI Engine  —  powered by Groq (free)
=========================================
Uses Groq's free API to run LLaMA 3.3 70B (Meta's best open-source model).

Free tier:  14,400 requests / day  |  No credit card required
Get a key:  console.groq.com  →  API Keys  →  Create API Key

Falls back to Google Gemini Flash if GEMINI_API_KEY is set instead.

No extra Python packages required — uses httpx (already installed).
"""

from __future__ import annotations
import os, re, json, logging
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("lea.ai_engine")

# ── API endpoints ─────────────────────────────────────────────────────────────
GROQ_API      = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL    = "llama-3.3-70b-versatile"   # best free model on Groq
GEMINI_API    = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

MAX_CONTENT_CHARS = 10_000   # safe limit for LLaMA context

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

EXTRA_PATHS = [
    "/about", "/about-us", "/our-story",
    "/team", "/attorneys", "/lawyers", "/our-team", "/meet-the-team",
    "/reviews", "/testimonials", "/client-reviews",
    "/results", "/case-results", "/verdicts", "/settlements",
    "/community", "/espanol", "/spanish",
]


# ─────────────────────────────────────────────────────────────────────────────
# Detect which AI backend is configured
# ─────────────────────────────────────────────────────────────────────────────
def get_ai_backend() -> str | None:
    if os.getenv("GROQ_API_KEY", "").strip():
        return "groq"
    if os.getenv("GEMINI_API_KEY", "").strip():
        return "gemini"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Website scraper (multi-page, same as before)
# ─────────────────────────────────────────────────────────────────────────────
def _clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script","style","noscript","header","footer","nav","meta","link"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s{3,}", "  ", text).strip()


async def fetch_website_content(base_url: str, max_chars: int = MAX_CONTENT_CHARS) -> dict:
    """Fetch homepage + up to 4 high-value sub-pages. Returns cleaned text."""
    if not base_url:
        return {"pages_fetched": 0, "raw_text": "", "urls_tried": []}
    if not base_url.startswith("http"):
        base_url = "https://" + base_url

    origin = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
    collected, fetched, urls_tried = [], 0, []

    async with httpx.AsyncClient(timeout=12, headers=SCRAPE_HEADERS,
                                  follow_redirects=True, verify=False) as client:
        # Homepage
        try:
            r = await client.get(base_url)
            if r.status_code == 200:
                collected.append(f"[Homepage]\n{_clean_html(r.text)[:4000]}")
                fetched += 1; urls_tried.append(base_url)
        except Exception as e:
            log.warning(f"Homepage fetch failed: {e}")

        # Sub-pages
        for path in EXTRA_PATHS:
            if sum(len(t) for t in collected) >= max_chars or fetched >= 5:
                break
            url = urljoin(origin, path)
            urls_tried.append(url)
            try:
                r = await client.get(url)
                if r.status_code == 200 and len(r.text) > 500:
                    text = _clean_html(r.text)
                    if len(text) > 200:
                        label = path.strip("/").replace("-"," ").title()
                        collected.append(f"[{label}]\n{text[:2500]}")
                        fetched += 1
            except Exception:
                pass

    return {
        "pages_fetched": fetched,
        "raw_text": "\n\n".join(collected)[:max_chars],
        "urls_tried": urls_tried,
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM call — Groq (primary) or Gemini (fallback)
# ─────────────────────────────────────────────────────────────────────────────
async def _call_groq(messages: list, max_tokens: int = 1200, json_mode: bool = False) -> str:
    """Call Groq API (OpenAI-compatible). json_mode forces valid JSON output."""
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            GROQ_API,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Groq API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"].strip()


async def _call_gemini(prompt: str, max_tokens: int = 1200) -> str:
    """Call Google Gemini Flash API (free tier)."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{GEMINI_API}?key={api_key}",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


async def _call_llm(system: str, user: str, max_tokens: int = 1200, json_mode: bool = False) -> str:
    """Route to whichever LLM backend is configured."""
    backend = get_ai_backend()
    if backend == "groq":
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        return await _call_groq(messages, max_tokens=max_tokens, json_mode=json_mode)
    elif backend == "gemini":
        # Gemini has no system role — prepend it to the user message
        full_prompt = f"{system}\n\n{user}" if system else user
        return await _call_gemini(full_prompt, max_tokens=max_tokens)
    else:
        raise ValueError("No AI API key configured. Add GROQ_API_KEY to .env (free at console.groq.com)")


# ─────────────────────────────────────────────────────────────────────────────
# Attorney roster counter — scrapes /attorneys, /team, etc. and counts profiles
# ─────────────────────────────────────────────────────────────────────────────

# Pages most likely to list every attorney at the firm
ATTORNEY_PATHS = [
    "/attorneys", "/attorneys-at-law", "/our-attorneys", "/meet-our-attorneys",
    "/lawyers", "/our-lawyers", "/meet-our-lawyers",
    "/team", "/our-team", "/meet-the-team", "/meet-our-team",
    "/people", "/professionals", "/staff",
    "/partners", "/associates",
    "/about/team", "/about/attorneys", "/about/lawyers",
]

# CSS selectors that law firm website builders commonly use for attorney cards
ATTORNEY_SELECTORS = [
    # Generic team/profile card patterns
    "[class*='attorney']", "[class*='lawyer']", "[class*='team-member']",
    "[class*='staff-member']", "[class*='person-card']", "[class*='profile-card']",
    "[class*='bio-card']", "[class*='attorney-card']", "[class*='lawyer-card']",
    "[class*='team-card']", "[class*='our-team']",
    # Schema.org
    "[itemtype*='schema.org/Person']",
    # Common WordPress/Divi/Elementor patterns
    ".et_pb_team_member", ".team_member_description", ".elementor-team-member",
    ".wpb_single_image.vc_align_center",
    # Avvo / Martindale-style embeds
    "[class*='attorney-bio']", "[class*='lawyer-bio']",
]

# Legal title keywords used to detect attorney names in text
TITLE_KEYWORDS = [
    "partner", "associate", "attorney", "counsel", "esq", "j.d", "ll.m",
    "founding partner", "managing partner", "senior partner", "of counsel",
    "paralegal",  # count separately but still a signal of firm size
]


def _count_from_html(html: str, url: str) -> dict:
    """
    Try multiple strategies to count attorneys from a single page's HTML.
    Returns {count, method, names} for the best strategy found.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Remove nav/footer noise
    for tag in soup(["nav", "footer", "header", "script", "style", "noscript"]):
        tag.decompose()

    # ── Strategy 1: CSS class selectors ──────────────────────────────────────
    for sel in ATTORNEY_SELECTORS:
        try:
            items = soup.select(sel)
            if len(items) >= 2:
                # Extract names from headings inside each card
                names = []
                for item in items:
                    h = item.find(["h1","h2","h3","h4","h5","strong","b"])
                    if h:
                        n = h.get_text(strip=True)
                        if 4 < len(n) < 60 and not any(c.isdigit() for c in n):
                            names.append(n)
                count = len(items)
                return {"count": count, "method": f"CSS:{sel}", "names": names[:30]}
        except Exception:
            pass

    # ── Strategy 2: Schema.org Person markup ─────────────────────────────────
    people = soup.find_all(attrs={"itemtype": re.compile(r"schema.org/Person", re.I)})
    if len(people) >= 2:
        names = []
        for p in people:
            n_el = p.find(attrs={"itemprop": "name"}) or p.find(["h2","h3","h4"])
            if n_el:
                names.append(n_el.get_text(strip=True))
        return {"count": len(people), "method": "schema.org/Person", "names": names[:30]}

    # ── Strategy 3: Repeating heading pattern (grid of names) ────────────────
    for tag in ["h2", "h3", "h4"]:
        headings = soup.find_all(tag)
        # Filter to name-like headings: 2+ words, no digits, < 60 chars
        name_headings = [
            h.get_text(strip=True) for h in headings
            if 5 < len(h.get_text(strip=True)) < 60
            and not any(ch.isdigit() for ch in h.get_text())
            and len(h.get_text(strip=True).split()) in range(2, 6)
        ]
        if len(name_headings) >= 3:
            # Check that sibling elements contain legal titles (validates these are attorney headings)
            validated = []
            for h in soup.find_all(tag):
                text_block = h.get_text(" ", strip=True).lower()
                parent_text = (h.parent.get_text(" ", strip=True) if h.parent else "").lower()
                if any(kw in text_block or kw in parent_text for kw in TITLE_KEYWORDS):
                    validated.append(h.get_text(strip=True))
            if len(validated) >= 2:
                return {"count": len(validated), "method": f"heading-{tag}+title", "names": validated[:30]}
        # If many headings but no title validation, still return if >= 4
        if len(name_headings) >= 4:
            return {"count": len(name_headings), "method": f"heading-{tag}-pattern", "names": name_headings[:30]}

    # ── Strategy 4: Regex — names followed by legal titles ───────────────────
    full_text = soup.get_text("\n", strip=True)
    # Match "Firstname Lastname\nPartner" or "Firstname Lastname, Esq."
    title_pattern = re.compile(
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z\.]+){1,4})"   # Name
        r"(?:\s*,\s*|\s*\n\s*)"                    # separator
        r"(?:Esq|J\.D|LL\.M|Partner|Associate|Counsel|Attorney|Paralegal)",
        re.MULTILINE
    )
    matches = list({m.group(1).strip() for m in title_pattern.finditer(full_text)})
    if len(matches) >= 2:
        return {"count": len(matches), "method": "regex-title", "names": matches[:30]}

    # ── Strategy 5: Count images with attorney-related alt text ──────────────
    atty_imgs = [
        img for img in soup.find_all("img")
        if any(kw in (img.get("alt","") + img.get("title","")).lower()
               for kw in ["attorney","lawyer","partner","counsel","esq"])
    ]
    if len(atty_imgs) >= 2:
        names = [img.get("alt","") for img in atty_imgs if img.get("alt","")]
        return {"count": len(atty_imgs), "method": "img-alt-text", "names": names[:30]}

    return {"count": 0, "method": "none", "names": []}


async def count_attorneys_from_website(base_url: str) -> dict:
    """
    Find the firm's attorney roster page and count how many attorneys are listed.

    Tries up to 8 candidate paths (e.g. /attorneys, /team, /lawyers).
    For each page, applies 5 parsing strategies in order of reliability.
    Falls back to LLM if HTML parsing yields nothing.

    Returns:
        attorney_count_website  – int (0 if not found)
        attorney_page_url       – str (URL that worked, or "")
        attorney_names          – list of names found (up to 30)
        attorney_count_method   – how the count was determined
    """
    if not base_url:
        return {"attorney_count_website": 0, "attorney_page_url": "", "attorney_names": [], "attorney_count_method": "none"}
    if not base_url.startswith("http"):
        base_url = "https://" + base_url

    origin = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"

    # Also check the homepage itself — some single-page sites list all attorneys there
    candidate_urls = [urljoin(origin, p) for p in ATTORNEY_PATHS]

    best = {"count": 0, "method": "none", "names": [], "url": ""}

    async with httpx.AsyncClient(timeout=15, headers=SCRAPE_HEADERS,
                                  follow_redirects=True, verify=False) as client:

        # First: scan homepage for links to the attorney roster page
        try:
            hp = await client.get(base_url)
            if hp.status_code == 200:
                hp_soup = BeautifulSoup(hp.text, "html.parser")
                # Try parsing the homepage directly (some small firms list all attorneys there)
                hp_result = _count_from_html(hp.text, base_url)
                if hp_result["count"] >= 2:
                    best = {**hp_result, "url": base_url}

                # Find internal links that look like attorney roster pages
                for a in hp_soup.find_all("a", href=True):
                    href = a["href"].lower()
                    link_text = a.get_text(strip=True).lower()
                    if any(kw in href or kw in link_text for kw in
                           ["attorney", "lawyer", "team", "people", "staff", "professional"]):
                        full_url = urljoin(base_url, a["href"])
                        # Prepend discovered links so they get tried first
                        if full_url not in candidate_urls:
                            candidate_urls.insert(0, full_url)
        except Exception:
            pass

        # Try each candidate page
        for url in candidate_urls[:10]:  # cap at 10 requests
            try:
                r = await client.get(url)
                if r.status_code != 200 or len(r.text) < 300:
                    continue
                result = _count_from_html(r.text, url)
                if result["count"] > best["count"]:
                    best = {**result, "url": url}
                if best["count"] >= 3:  # good enough — stop searching
                    break
            except Exception:
                continue

    # ── LLM fallback: if HTML parsing found nothing, ask the LLM ─────────────
    if best["count"] < 2 and get_ai_backend():
        try:
            # Re-fetch whichever page had the most text
            for url in ([best["url"]] if best["url"] else []) + candidate_urls[:4]:
                try:
                    async with httpx.AsyncClient(timeout=12, headers=SCRAPE_HEADERS,
                                                  follow_redirects=True, verify=False) as c2:
                        r2 = await c2.get(url)
                    if r2.status_code == 200 and len(r2.text) > 500:
                        page_text = _clean_html(r2.text)[:3000]
                        llm_resp = await _call_llm(
                            "You are a precise data extractor. Answer only with a JSON object.",
                            f"""Count the individual attorneys/lawyers listed on this law firm page.
Do NOT count staff, paralegals, or support roles — only attorneys/lawyers/partners/associates.
If you cannot find a list of attorneys, return 0.

PAGE TEXT:
{page_text}

Return ONLY: {{"attorney_count": <number>, "names": ["Name1", "Name2", ...up to 20], "confidence": "high/medium/low"}}""",
                            max_tokens=300,
                            json_mode=True,
                        )
                        llm_data = json.loads(llm_resp)
                        if llm_data.get("attorney_count", 0) >= 1:
                            return {
                                "attorney_count_website":  llm_data["attorney_count"],
                                "attorney_page_url":       url,
                                "attorney_names":          llm_data.get("names", []),
                                "attorney_count_method":   f"llm ({llm_data.get('confidence','?')} confidence)",
                            }
                        break
                except Exception:
                    continue
        except Exception as e:
            log.warning(f"LLM attorney count fallback failed: {e}")

    return {
        "attorney_count_website":  best["count"],
        "attorney_page_url":       best.get("url", ""),
        "attorney_names":          best.get("names", []),
        "attorney_count_method":   best.get("method", "none"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Firm analysis prompt
# ─────────────────────────────────────────────────────────────────────────────
ANALYSIS_SYSTEM = """You are an M&A analyst at LEA Investment, a firm that acquires US law firms.
Read the law firm's website content and extract specific, concrete intelligence to personalise an outreach email.
Only reference things actually present in the content — never invent details.
Return ONLY a valid JSON object. No markdown, no explanation, just the JSON."""

ANALYSIS_PROMPT = """Analyse this website content for {firm_name} ({city}, {state}).

WEBSITE CONTENT:
{content}

Return a JSON object with exactly these keys:

{{
  "community_focus": "One sentence on any specific community or demographic this firm explicitly serves (e.g. Hispanic community, veterans, Chinese-speaking clients). Empty string if none found.",
  "unique_differentiators": ["Up to 3 short strings of specific things this firm does differently — e.g. '24/7 intake line', 'Bilingual Spanish staff', 'In-house investigators'. Only things explicitly on the site."],
  "notable_results": ["Up to 3 short strings of specific verdicts or settlements mentioned with amounts — e.g. '$4.2M truck accident verdict'. Empty list if none found."],
  "firm_culture": "One sentence on the firm's stated culture or founding story — e.g. 'Family-owned since 1991, founded by Maria Gonzalez after her own accident'. Empty string if not clear.",
  "acquisition_signals": ["Up to 3 signals the firm might be open to a deal — e.g. 'Founding partner appears senior (35+ years mentioned)', 'Rapid multi-office expansion', 'Heavy paid ad spend detected'. Only infer from real evidence."],
  "outreach_hooks": ["2-3 specific talking points to personalise the email. Reference real things from the site — e.g. 'Mention their Hispanic community focus and ask how they manage growth in that segment', 'Reference the $4.2M verdict as evidence of their calibre'. Make these feel genuine."],
  "best_contact_guess": "Name and title of the most senior person on the site — e.g. 'Alex Hanna, Founding Partner'. Empty string if unclear.",
  "website_language": "Primary language(s) — e.g. 'English', 'English + Spanish'.",
  "confidence": "high, medium, or low — based on how much useful content was found"
}}"""


async def analyze_firm(
    firm_name: str,
    city: str,
    state: str,
    website_url: str,
    google_review_snippets: list[str] | None = None,
) -> dict:
    """Scrape website + run LLM analysis + count attorneys. All run concurrently."""
    import asyncio as _asyncio

    # Run website content fetch and attorney counting in parallel
    web_task      = fetch_website_content(website_url)
    attorney_task = count_attorneys_from_website(website_url)
    web, attorney_data = await _asyncio.gather(web_task, attorney_task)

    page_content = web["raw_text"]

    if google_review_snippets:
        review_text = "\n".join(f"• {r}" for r in google_review_snippets[:15])
        page_content += f"\n\n[Google Reviews]\n{review_text}"

    if not page_content.strip():
        return {
            "error": "Could not fetch any website content — the site may be JS-only or blocking scrapers.",
            "pages_fetched": 0,
            "community_focus": "",
            "unique_differentiators": [],
            "notable_results": [],
            "firm_culture": "",
            "acquisition_signals": [],
            "outreach_hooks": [],
            "best_contact_guess": "",
            "website_language": "Unknown",
            "confidence": "low",
            **attorney_data,
        }

    user_prompt = ANALYSIS_PROMPT.format(
        firm_name=firm_name,
        city=city,
        state=state,
        content=page_content[:MAX_CONTENT_CHARS],
    )

    try:
        raw = await _call_llm(ANALYSIS_SYSTEM, user_prompt, max_tokens=1200, json_mode=True)
        raw = re.sub(r"^```json\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw.strip())
        result = json.loads(raw)
        result["pages_fetched"]          = web["pages_fetched"]
        result["urls_scraped"]           = web["urls_tried"][:8]
        # Merge attorney count data
        result["attorney_count_website"] = attorney_data.get("attorney_count_website", 0)
        result["attorney_page_url"]      = attorney_data.get("attorney_page_url", "")
        result["attorney_names"]         = attorney_data.get("attorney_names", [])
        result["attorney_count_method"]  = attorney_data.get("attorney_count_method", "none")
        return result
    except json.JSONDecodeError as e:
        log.error(f"LLM returned non-JSON for {firm_name}: {raw[:200]}")
        return {"error": f"JSON parse error: {e}", "raw_response": raw[:400],
                "confidence": "low", "pages_fetched": web["pages_fetched"], **attorney_data}
    except Exception as e:
        log.error(f"AI analysis failed for {firm_name}: {e}")
        return {"error": str(e), "confidence": "low", "pages_fetched": web["pages_fetched"], **attorney_data}


# ─────────────────────────────────────────────────────────────────────────────
# Personalised email generator
# ─────────────────────────────────────────────────────────────────────────────
EMAIL_SYSTEM = """You are a senior associate at LEA Investment writing a cold outreach email to a law firm founder.
The email must feel genuinely researched — not a mass template. Be concise (under 200 words), warm but professional.
End with a low-friction ask: a short confidential call. Return ONLY the email body — no subject line, no markdown."""

EMAIL_PROMPT = """Write a personalised outreach email to {contact} at {firm_name} ({city}, {state}).

INTELLIGENCE TO WEAVE IN NATURALLY (don't copy-paste these robotically):
- Community focus: {community_focus}
- What makes them unique: {differentiators}
- Notable results: {results}
- Outreach hooks: {hooks}

SENDER CONTEXT:
LEA Investment acquires and partners with high-quality US law firms. We offer founding partners liquidity,
growth capital, and succession planning — without disrupting operations. Email is from {sender}.

Open with something specific to {firm_name}. Keep it under 200 words. End by asking for a 20-min call."""


async def generate_outreach_email(
    firm_name: str,
    city: str,
    state: str,
    insights: dict,
    contact_name: str = "",
    sender_name: str = "[Your Name]",
) -> str:
    """Generate AI-personalised outreach email using insights dict."""
    if not get_ai_backend():
        return _fallback_email(firm_name, city, state, contact_name, sender_name, insights)

    if not contact_name:
        contact_name = insights.get("best_contact_guess") or "there"
    first = contact_name.split(",")[0].split(" ")[0] if contact_name != "there" else "there"

    user_prompt = EMAIL_PROMPT.format(
        contact=first,
        firm_name=firm_name,
        city=city,
        state=state,
        community_focus=insights.get("community_focus") or "not specified",
        differentiators="; ".join(insights.get("unique_differentiators") or []) or "not specified",
        results="; ".join(insights.get("notable_results") or []) or "none mentioned",
        hooks="; ".join(insights.get("outreach_hooks") or []) or "none",
        sender=sender_name,
    )

    try:
        return await _call_llm(EMAIL_SYSTEM, user_prompt, max_tokens=500)
    except Exception as e:
        log.error(f"AI email generation failed: {e}")
        return _fallback_email(firm_name, city, state, contact_name, sender_name, insights)


def _fallback_email(firm_name, city, state, contact_name, sender_name, insights):
    first = (contact_name or "there").split(",")[0].split(" ")[0]
    hook  = (insights.get("outreach_hooks") or [""])[0]
    comm  = insights.get("community_focus", "")
    return f"""Dear {first},

I came across {firm_name} while researching leading law firms in {city}, {state}{' — your work with ' + comm if comm else ''}, and I wanted to reach out directly.

My name is {sender_name} from LEA Investment, a firm that specialises in partnering with and acquiring high-quality US law firms.

{hook + chr(10) + chr(10) if hook else ""}We work with founding partners to provide liquidity, growth capital, and a clear succession pathway — without disrupting the firm's culture or day-to-day operations.

Would you have 20 minutes for a confidential conversation this week?

Best regards,
{sender_name}
LEA Investment"""


# ─────────────────────────────────────────────────────────────────────────────
# LEA INVESTMENT SCORE  —  0-100 composite with LLM synthesis
# ─────────────────────────────────────────────────────────────────────────────

SCORE_SYSTEM = """You are a senior investment analyst at LEA Investment, a private equity firm that acquires US personal injury law firms.
Your job is to evaluate a law firm as an acquisition target and assign an investment score from 0 to 100.

Scoring philosophy:
- 80-100: Exceptional target. Strong client volume, quality reputation, right scale, clear acquisition signals. Prioritise immediately.
- 60-79:  Good target. Solid fundamentals with one or two gaps. Worth outreach.
- 40-59:  Average. Monitor but do not prioritise. May improve with more data.
- 20-39:  Weak signals. Too small, too new, or insufficient data to justify outreach.
- 0-19:   Not a fit. Poor ratings, no activity, or clearly not acquisition-ready.

Return ONLY a valid JSON object. No markdown."""

SCORE_PROMPT = """Score this law firm as an LEA acquisition target.

FIRM: {firm_name}, {city}, {state}

QUANTITATIVE SIGNALS:
- Google rating: {google_stars} / 5.0  (reviews: {google_review_count})
- Attorney count: {attorney_count} (source: {attorney_source})
- LinkedIn headcount: {linkedin_headcount}
- Federal court cases (90 days): {active_cases}
- LEA internal rank: {lea_rank} (market: {lea_market})

AI INTELLIGENCE FROM WEBSITE:
- Community focus: {community_focus}
- Unique differentiators: {differentiators}
- Notable results: {notable_results}
- Acquisition signals detected: {acquisition_signals}
- Firm culture: {firm_culture}
- Confidence of AI analysis: {ai_confidence}

LEA RESEARCH NOTES:
{lea_notes}

Return a JSON object with exactly these keys:
{{
  "score": <integer 0-100>,
  "grade": <"A+"|"A"|"A-"|"B+"|"B"|"B-"|"C+"|"C"|"D"|"F">,
  "investment_thesis": "<2-3 sentence WHY LEA should or should not invest. Be specific, reference real data points. This is the core output.>",
  "sub_scores": {{
    "client_volume":        <0-100, based on review count — proxy for deal flow>,
    "reputation":           <0-100, based on rating and rankings>,
    "scale":                <0-100, based on attorney count and headcount>,
    "market_activity":      <0-100, based on court cases and growth signals>,
    "acquisition_readiness":<0-100, based on AI signals — succession, culture, founder signals>
  }},
  "key_strengths": ["<up to 3 specific strengths>"],
  "key_risks": ["<up to 2 specific risks or data gaps>"],
  "recommended_action": "<Prioritise outreach|Monitor|Low priority|Insufficient data>"
}}"""


def _compute_rule_score(firm: dict, insights: dict | None) -> dict:
    """
    Compute structured sub-scores from raw signals.
    Used as grounding for the LLM — prevents hallucination on numeric data.
    """
    g_reviews  = firm.get("google_review_count") or 0
    g_stars    = firm.get("google_stars") or 0
    atty_count = (firm.get("attorney_count_website") or
                  firm.get("attorney_count_web") or
                  firm.get("attorney_count") or
                  firm.get("linkedin_headcount") or 0)
    cases_90d  = firm.get("active_cases") or firm.get("case_count_90d") or 0
    lea_rank   = firm.get("lea_rank") or 0

    # Client volume: log-scaled, 500+ reviews = ~90, 100 = ~60, 20 = ~35
    import math
    vol_score = min(100, int(math.log1p(g_reviews) / math.log1p(2000) * 100)) if g_reviews else 0

    # Reputation: stars 4.8+ = 95, 4.5 = 80, 4.0 = 60, <3.5 = 20
    rep_score = 0
    if g_stars >= 4.8:  rep_score = 95
    elif g_stars >= 4.5: rep_score = 80
    elif g_stars >= 4.2: rep_score = 68
    elif g_stars >= 4.0: rep_score = 58
    elif g_stars >= 3.5: rep_score = 40
    elif g_stars > 0:    rep_score = 20

    # Scale: 50+ attorneys = 90, 20 = 70, 10 = 50, 5 = 35, 1-2 = 15
    scale_score = 0
    if atty_count >= 50:   scale_score = 90
    elif atty_count >= 20: scale_score = 72
    elif atty_count >= 10: scale_score = 55
    elif atty_count >= 5:  scale_score = 38
    elif atty_count >= 2:  scale_score = 20
    elif atty_count == 1:  scale_score = 10

    # Market activity: court cases
    activity_score = min(100, int(math.log1p(cases_90d) / math.log1p(50) * 100)) if cases_90d else 30

    # LEA rank bonus (if in our pre-researched list, it's already a vetted target)
    if lea_rank > 0:
        lea_bonus = max(0, 50 - (lea_rank - 1) * 2)  # rank 1 = +50, rank 15 = +22, rank 25+ = 0
    else:
        lea_bonus = 0

    # Acquisition readiness from AI signals
    acq_score = 50  # default (unknown)
    if insights:
        signals = insights.get("acquisition_signals") or []
        if len(signals) >= 3: acq_score = 85
        elif len(signals) == 2: acq_score = 70
        elif len(signals) == 1: acq_score = 60
        if not signals and insights.get("confidence") == "high": acq_score = 40

    return {
        "client_volume":         vol_score,
        "reputation":            rep_score,
        "scale":                 scale_score,
        "market_activity":       activity_score,
        "acquisition_readiness": acq_score,
        "lea_bonus":             lea_bonus,
        "_rule_total": int(
            vol_score * 0.25 +
            rep_score * 0.20 +
            scale_score * 0.20 +
            activity_score * 0.15 +
            acq_score * 0.15 +
            lea_bonus * 0.05
        ),
    }


async def score_firm(
    firm: dict,
    insights: dict | None = None,
) -> dict:
    """
    Produce a 0-100 LEA investment score for a firm.
    Uses rule-based sub-scores as grounded inputs, then LLM synthesises
    the final score, thesis, and recommendation.

    firm dict should include: name, city, state, google_stars,
    google_review_count, attorney_count, active_cases, lea_rank, lea_notes, etc.
    insights dict is the output of analyze_firm() — optional but improves accuracy.
    """
    rule = _compute_rule_score(firm, insights)

    firm_name  = firm.get("name", "Unknown Firm")
    city       = firm.get("city", "")
    state      = firm.get("state", "")
    ins        = insights or {}

    atty_count = (firm.get("attorney_count_website") or
                  firm.get("attorney_count_web") or
                  firm.get("attorney_count") or
                  firm.get("linkedin_headcount") or 0)
    atty_src   = ("website scrape" if firm.get("attorney_count_website")
                  else "LinkedIn" if firm.get("linkedin_headcount")
                  else "court dockets" if firm.get("attorney_count")
                  else "unknown")

    prompt = SCORE_PROMPT.format(
        firm_name          = firm_name,
        city               = city,
        state              = state,
        google_stars       = firm.get("google_stars") or "unknown",
        google_review_count= firm.get("google_review_count") or 0,
        attorney_count     = atty_count or "unknown",
        attorney_source    = atty_src,
        linkedin_headcount = firm.get("linkedin_headcount_range") or firm.get("linkedin_headcount") or "unknown",
        active_cases       = firm.get("active_cases") or firm.get("case_count_90d") or "unknown",
        lea_rank           = f"#{firm.get('lea_rank')}" if firm.get("lea_rank") else "not in LEA database",
        lea_market         = firm.get("lea_market") or firm.get("market") or city,
        community_focus    = ins.get("community_focus") or "not analysed",
        differentiators    = "; ".join(ins.get("unique_differentiators") or []) or "not analysed",
        notable_results    = "; ".join(ins.get("notable_results") or []) or "not analysed",
        acquisition_signals= "; ".join(ins.get("acquisition_signals") or []) or "none detected",
        firm_culture       = ins.get("firm_culture") or "not analysed",
        ai_confidence      = ins.get("confidence") or "none (no AI analysis run)",
        lea_notes          = firm.get("lea_notes") or "none",
    )

    # If no AI backend, return rule-based score only
    if not get_ai_backend():
        r = rule["_rule_total"]
        grade = ("A+" if r>=95 else "A" if r>=90 else "A-" if r>=85 else
                 "B+" if r>=80 else "B" if r>=75 else "B-" if r>=70 else
                 "C+" if r>=65 else "C" if r>=55 else "D" if r>=40 else "F")
        return {
            "score": r,
            "grade": grade,
            "investment_thesis": f"Rule-based score only (no AI key). Sub-scores: volume={rule['client_volume']}, reputation={rule['reputation']}, scale={rule['scale']}, activity={rule['market_activity']}.",
            "sub_scores": {k: v for k, v in rule.items() if not k.startswith("_") and k != "lea_bonus"},
            "key_strengths": [],
            "key_risks": ["No AI analysis available — add GROQ_API_KEY for full scoring"],
            "recommended_action": "Insufficient data",
            "rule_score": r,
        }

    try:
        raw = await _call_llm(SCORE_SYSTEM, prompt, max_tokens=800, json_mode=True)
        raw = re.sub(r"^```json\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw.strip())
        result = json.loads(raw)
        result["rule_score"]  = rule["_rule_total"]
        result["sub_scores"]  = result.get("sub_scores") or {
            k: v for k, v in rule.items() if not k.startswith("_") and k != "lea_bonus"
        }
        # Sanity-clamp score
        result["score"] = max(0, min(100, int(result.get("score", rule["_rule_total"]))))
        return result
    except Exception as e:
        log.error(f"Score generation failed for {firm_name}: {e}")
        r = rule["_rule_total"]
        return {
            "score": r,
            "grade": "?",
            "investment_thesis": f"Scoring error: {e}",
            "sub_scores": {k: v for k, v in rule.items() if not k.startswith("_") and k != "lea_bonus"},
            "key_strengths": [],
            "key_risks": [str(e)],
            "recommended_action": "Insufficient data",
            "rule_score": r,
        }
