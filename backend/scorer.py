"""
LEA Scoring Engine
Implements the 5-factor weighted algorithm from law-firm-ranking-algorithm.md
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import re


# ──────────────────────────────────────────────
# Data containers
# ──────────────────────────────────────────────

TIER1_AREAS = {
    "personal injury", "pi", "premises liability", "auto accident",
    "auto accidents", "workers compensation", "workers comp",
    "immigration", "consumer bankruptcy", "bankruptcy",
    "mass tort", "criminal", "traffic", "slip and fall",
    "wrongful death", "catastrophic injury", "trucking accident",
    "motorcycle accident", "dog bite",
}

TIER2_AREAS = {
    "family law", "divorce", "child custody", "consumer disputes",
    "consumer protection", "employment law", "discrimination",
    "social security disability",
}

TIER3_AREAS = {
    "medical malpractice", "product liability", "sexual abuse",
    "sexual assault", "minor", "juvenile",
}

TIER_SCORES = {"tier1": 5, "tier2": 3, "tier3": -2}


@dataclass
class PracticeAreaData:
    areas: list[str] = field(default_factory=list)


@dataclass
class BrandData:
    google_stars: float = 0.0
    google_review_count: int = 0
    avvo_rating: float = 0.0
    bbb_grade: str = ""           # "A+", "A", "B+", ...
    nlp_sentiment_score: float = 0.0  # 0-100
    response_rate: float = 0.0    # 0-100 %
    seo_domain_authority: int = 0
    google_ads_active: bool = False
    tv_advertising: bool = False
    super_lawyers: bool = False
    martindale_rating: str = ""   # "AV Preeminent", "BV Distinguished", ""


@dataclass
class MarketData:
    city: str = ""
    state: str = ""
    population_growth_rate: float = 0.0   # annual %
    median_hh_income: int = 0
    unemployment_rate: float = 0.0
    competitor_count: int = 0
    google_rank_primary_kw: int = 0       # their rank for "[city] PI lawyer"
    favorable_tort_state: bool = False    # no damage caps
    high_immigrant_population: bool = False
    injury_prone_industry: bool = False   # construction etc in area


@dataclass
class FinancialData:
    contingency_pct: float = 0.0          # 0-100 %
    avg_settlement_value: int = 0
    case_closure_rate: float = 0.0        # 0-100 %
    client_referral_rate: float = 0.0     # 0-100 %
    intake_conversion_rate: float = 0.0   # 0-100 %
    paralegals_per_attorney: float = 0.0
    legal_tech_crm: str = ""              # "Clio", "MyCase", etc.
    bar_complaint_count: int = 0
    active_malpractice_claims: int = 0
    has_professional_liability: bool = True
    medical_referral_network: bool = False


@dataclass
class GrowthData:
    attorney_growth_12mo: int = 0     # attorneys added
    new_offices_12mo: int = 0
    staff_growth_pct: float = 0.0     # YoY %
    ad_spend_increased: bool = False
    seo_authority_improved: bool = False
    new_practice_areas_24mo: int = 0
    crm_recently_adopted: bool = False
    new_referral_partnerships: int = 0
    social_growth_pct: float = 0.0    # follower growth YoY %


@dataclass
class MAReadinessData:
    bar_admission_year: Optional[int] = None
    founder_count: int = 1
    has_junior_partners: bool = False
    website_last_updated_year: Optional[int] = None
    firm_founded_year: Optional[int] = None
    attorney_count: int = 0
    office_count: int = 1


# ──────────────────────────────────────────────
# Factor scorers (each returns 0-100)
# ──────────────────────────────────────────────

def score_practice_area(data: PracticeAreaData) -> tuple[float, list[str]]:
    reasons = []
    tier1_count = 0
    tier2_count = 0
    tier3_count = 0

    for area in data.areas:
        normalized = area.lower().strip()
        if any(t in normalized for t in TIER1_AREAS):
            tier1_count += 1
        elif any(t in normalized for t in TIER2_AREAS):
            tier2_count += 1
        elif any(t in normalized for t in TIER3_AREAS):
            tier3_count += 1

    raw = (tier1_count * 5) + (tier2_count * 3) + (tier3_count * -2)

    # Diversification bonus
    if tier1_count >= 4:
        raw += 5
        reasons.append(f"Strong diversification across {tier1_count} Tier-1 practice areas")
    if tier1_count >= 2 and tier2_count >= 1:
        raw += 3
        reasons.append("Tier-1/Tier-2 pairing creates cross-referral opportunity")
    if tier3_count > 0 and tier3_count / max(len(data.areas), 1) > 0.5:
        raw -= 5
        reasons.append(f"WARNING: {tier3_count} Tier-3 areas concentrate risk")

    max_raw = 65.0
    score = max(0.0, min(100.0, (raw / max_raw) * 100))

    if tier1_count > 0 and score >= 60:
        reasons.append(f"{tier1_count} Tier-1 area(s): {', '.join(a for a in data.areas[:3]).title()}")

    return round(score, 1), reasons


def score_brand(data: BrandData) -> tuple[float, list[str]]:
    reasons = []
    raw = 0.0

    # Google stars (15 pts max)
    if data.google_stars >= 4.8:
        google_pts = 15
    elif data.google_stars >= 4.5:
        google_pts = 10
    elif data.google_stars >= 4.0:
        google_pts = 5
    else:
        google_pts = 0

    # Volume multiplier
    if data.google_review_count >= 200:
        vol_mult = 1.2
        reasons.append(f"{data.google_review_count} Google reviews — high intake volume signal")
    elif data.google_review_count >= 50:
        vol_mult = 1.0
    else:
        vol_mult = 0.8
        if data.google_review_count < 20:
            reasons.append(f"Low Google review count ({data.google_review_count}) — limited market presence")

    raw += google_pts * vol_mult
    if data.google_stars >= 4.5:
        reasons.append(f"{data.google_stars}★ Google rating")

    # AVVO (8 pts max)
    if data.avvo_rating >= 9.0:
        raw += 8
        reasons.append("AVVO Superb (9.0+)")
    elif data.avvo_rating >= 7.0:
        raw += 5

    # BBB (5 pts max)
    bbb_map = {"A+": 5, "A": 5, "B+": 2}
    raw += bbb_map.get(data.bbb_grade, 0)

    # Martindale (5 pts max)
    if data.martindale_rating == "AV Preeminent":
        raw += 5
        reasons.append("AV Preeminent (Martindale-Hubbell)")
    elif data.martindale_rating == "BV Distinguished":
        raw += 3

    # NLP sentiment (10 pts max)
    if data.nlp_sentiment_score >= 80:
        raw += 10
        reasons.append("Review sentiment strongly positive (>80%)")
    elif data.nlp_sentiment_score >= 60:
        raw += 5

    # Response rate (5 pts max)
    if data.response_rate >= 80:
        raw += 5
        reasons.append("Responds to >80% of reviews — strong client focus")
    elif data.response_rate >= 50:
        raw += 2

    # SEO + Ads (10 pts max)
    if data.seo_domain_authority >= 40:
        raw += 5
    elif data.seo_domain_authority >= 25:
        raw += 3

    if data.google_ads_active:
        raw += 5
        reasons.append("Active Google Ads — signals revenue confidence & intake scale")

    # TV advertising (5 pts)
    if data.tv_advertising:
        raw += 5
        reasons.append("TV/billboard advertising — durable brand moat (non-partner-dependent)")

    # Awards (5 pts max)
    if data.super_lawyers:
        raw += 5
        reasons.append("Super Lawyers designation")

    score = max(0.0, min(100.0, (raw / 73.0) * 100))
    return round(score, 1), reasons


def score_market(data: MarketData) -> tuple[float, list[str]]:
    reasons = []
    raw = 0.0

    # Population growth (8 pts max)
    if data.population_growth_rate > 3.0:
        raw += 8
        reasons.append(f"High population growth ({data.population_growth_rate:.1f}%/yr) — expanding client base")
    elif data.population_growth_rate >= 1.0:
        raw += 4
    else:
        reasons.append("Stagnant or declining local population — growth ceiling risk")

    # Income (6 pts max)
    if data.median_hh_income > 75_000:
        raw += 6
    elif data.median_hh_income >= 50_000:
        raw += 3

    # Unemployment (6 pts max) — low unemployment = healthy economy
    if data.unemployment_rate < 4.0:
        raw += 6
    elif data.unemployment_rate < 6.0:
        raw += 3
    else:
        reasons.append(f"High unemployment ({data.unemployment_rate}%) — economic risk")

    # Tailwinds
    if data.favorable_tort_state:
        raw += 5
        reasons.append("Favorable tort state — no cap on non-economic damages → higher case values")
    if data.high_immigrant_population:
        raw += 5
        reasons.append("High immigrant population — strong immigration + bankruptcy demand")
    if data.injury_prone_industry:
        raw += 5
        reasons.append("Major injury-prone industry in market — strong workers comp + PI pipeline")

    # Competition density (10 pts max)
    if data.competitor_count < 10:
        raw += 10
        reasons.append(f"Low competition density ({data.competitor_count} firms) — blue ocean market")
    elif data.competitor_count < 25:
        raw += 5
    else:
        reasons.append(f"Saturated market ({data.competitor_count}+ competing firms)")

    # Google rank (10 pts max)
    if 1 <= data.google_rank_primary_kw <= 3:
        raw += 10
        reasons.append(f"Ranks #{data.google_rank_primary_kw} on Google for primary practice keyword")
    elif 4 <= data.google_rank_primary_kw <= 10:
        raw += 5

    score = max(0.0, min(100.0, (raw / 55.0) * 100))
    return round(score, 1), reasons


def score_financial(data: FinancialData) -> tuple[float, list[str]]:
    reasons = []
    raw = 0.0

    # Contingency % (15 pts max)
    if data.contingency_pct >= 80:
        raw += 15
        reasons.append(f"{data.contingency_pct:.0f}% contingency model — aligned incentives, scalable")
    elif data.contingency_pct >= 50:
        raw += 7

    # Avg settlement (10 pts max)
    if data.avg_settlement_value >= 75_000:
        raw += 10
        reasons.append(f"Avg settlement ${data.avg_settlement_value:,} — strong bargaining power")
    elif data.avg_settlement_value >= 40_000:
        raw += 5
    elif data.avg_settlement_value > 0:
        reasons.append(f"Low avg settlement (${data.avg_settlement_value:,}) — volume-dependent model")

    # Closure rate (10 pts max)
    if data.case_closure_rate >= 80:
        raw += 10
        reasons.append("Fast case closure rate (>80%) — capital efficient, high turnover")
    elif data.case_closure_rate >= 60:
        raw += 5

    # Referral rate (15 pts max) — top signal
    if data.client_referral_rate >= 60:
        raw += 15
        reasons.append(f"{data.client_referral_rate:.0f}% of new clients from referrals — near-zero CAC")
    elif data.client_referral_rate >= 40:
        raw += 7

    # Intake conversion (10 pts max)
    if data.intake_conversion_rate >= 40:
        raw += 10
    elif data.intake_conversion_rate >= 25:
        raw += 5

    # Staff leverage (10 pts max)
    if data.paralegals_per_attorney >= 3:
        raw += 10
        reasons.append(f"{data.paralegals_per_attorney:.1f} paralegals/attorney — high-leverage model")
    elif data.paralegals_per_attorney >= 1.5:
        raw += 5

    # Medical referral network
    if data.medical_referral_network:
        raw += 7
        reasons.append("Medical referral network — near-zero CAC intake moat")

    # Legal tech (8 pts max)
    if data.legal_tech_crm and data.legal_tech_crm.lower() not in ("none", ""):
        raw += 8
        reasons.append(f"Modern CRM: {data.legal_tech_crm} — operational maturity")
    else:
        reasons.append("No modern CRM detected — operational risk, upgrade opportunity")

    # Risk flags (penalty)
    if data.bar_complaint_count >= 5 or data.active_malpractice_claims >= 3:
        return 0.0, ["⛔ AUTO-DISQUALIFIED: Excessive bar complaints or malpractice claims"]
    elif data.bar_complaint_count >= 2:
        raw -= 10
        reasons.append(f"WARNING: {data.bar_complaint_count} bar complaints on record")
    else:
        raw += 8  # clean record bonus
        reasons.append("Clean bar record")

    score = max(0.0, min(100.0, (raw / 93.0) * 100))
    return round(score, 1), reasons


def score_growth(data: GrowthData) -> tuple[float, list[str]]:
    reasons = []
    raw = 0.0

    # Hiring
    if data.attorney_growth_12mo >= 3:
        raw += 15
        reasons.append(f"+{data.attorney_growth_12mo} attorneys hired (12mo) — scaling up")
    elif data.attorney_growth_12mo >= 1:
        raw += 7

    if data.new_offices_12mo >= 1:
        raw += 15
        reasons.append(f"{data.new_offices_12mo} new office(s) opened — active expansion")

    if data.staff_growth_pct >= 20:
        raw += 10
        reasons.append(f"{data.staff_growth_pct:.0f}% staff growth YoY")
    elif data.staff_growth_pct >= 10:
        raw += 5

    # Digital signals
    if data.ad_spend_increased:
        raw += 10
        reasons.append("Ad spend trending up — revenue confidence signal")
    if data.seo_authority_improved:
        raw += 10

    if data.social_growth_pct >= 30:
        raw += 5
        reasons.append(f"Social media growing {data.social_growth_pct:.0f}% YoY")

    # Operational signals
    if data.new_practice_areas_24mo >= 1:
        raw += 10
        reasons.append(f"Added {data.new_practice_areas_24mo} new practice area(s) — strategic expansion")

    if data.crm_recently_adopted:
        raw += 8
        reasons.append("Recently adopted CRM — digitizing operations")

    if data.new_referral_partnerships >= 1:
        raw += 7
        reasons.append(f"{data.new_referral_partnerships} new referral partnership(s)")

    score = max(0.0, min(100.0, (raw / 90.0) * 100))
    return round(score, 1), reasons


def score_ma_readiness(data: MAReadinessData) -> tuple[float, list[str]]:
    """
    Higher score = more likely to want to exit = better M&A target.
    Factors: owner age, succession gap, firm age.
    """
    reasons = []
    raw = 0.0
    current_year = 2026

    # Bar admission year → estimated owner age
    if data.bar_admission_year:
        years_in_practice = current_year - data.bar_admission_year
        estimated_age = data.bar_admission_year - 1967 + 25  # approx
        # Rough: admitted ~25-28, so age ≈ (admission_year - 1970) + 25
        est_age = (data.bar_admission_year - 1970) + 27
        if est_age >= 62:
            raw += 40
            reasons.append(f"Founder bar admission {data.bar_admission_year} — likely in retirement window (est. age {est_age})")
        elif est_age >= 55:
            raw += 25
            reasons.append(f"Founder bar admission {data.bar_admission_year} — approaching retirement window (est. age ~{est_age})")
        elif est_age >= 48:
            raw += 10

    # Succession gap
    if not data.has_junior_partners:
        raw += 20
        reasons.append("No junior partners identified on team page — succession gap signals acquisition urgency")

    if data.founder_count == 1:
        raw += 10
        reasons.append("Single founder — concentrated ownership simplifies deal structure")

    # Website staleness (indicates readiness to sell / not investing in growth)
    if data.website_last_updated_year and data.website_last_updated_year <= current_year - 4:
        raw += 15
        reasons.append(f"Website last updated {data.website_last_updated_year} — stagnation signal (owner not reinvesting)")
    elif data.website_last_updated_year and data.website_last_updated_year >= current_year - 1:
        raw -= 10  # actively investing = less urgency to sell

    # Firm size / scalability for acquirer
    if data.attorney_count >= 10:
        raw += 10
        reasons.append(f"{data.attorney_count} attorneys across {data.office_count} location(s) → scalable platform")
    elif data.attorney_count >= 5:
        raw += 5

    # Firm age
    if data.firm_founded_year:
        firm_age = current_year - data.firm_founded_year
        if firm_age >= 20:
            raw += 5
            reasons.append(f"Established firm (founded {data.firm_founded_year}) — {firm_age}yr track record")

    score = max(0.0, min(100.0, (raw / 100.0) * 100))
    return round(score, 1), reasons


# ──────────────────────────────────────────────
# Master scorer
# ──────────────────────────────────────────────

WEIGHTS = {
    "practice_area": 0.30,
    "brand": 0.25,
    "market": 0.20,
    "financial": 0.15,
    "growth": 0.10,
}


def compute_full_score(
    practice: PracticeAreaData,
    brand: BrandData,
    market: MarketData,
    financial: FinancialData,
    growth: GrowthData,
    ma: MAReadinessData,
) -> dict:
    pa_score, pa_reasons = score_practice_area(practice)
    br_score, br_reasons = score_brand(brand)
    mk_score, mk_reasons = score_market(market)
    fi_score, fi_reasons = score_financial(financial)
    gr_score, gr_reasons = score_growth(growth)
    ma_score, ma_reasons = score_ma_readiness(ma)

    # Check for auto-disqualifiers
    if fi_score == 0.0 and any("AUTO-DISQUALIFIED" in r for r in fi_reasons):
        return {
            "composite": 0,
            "investment_tier": "Pass — Disqualified",
            "signal": "disqualified",
            "sub_scores": {
                "practice_fit": pa_score,
                "brand_quality": br_score,
                "market_position": mk_score,
                "financial_health": 0,
                "growth_momentum": gr_score,
                "ma_readiness": ma_score,
            },
            "why_highlights": fi_reasons,
        }

    composite = round(
        pa_score * WEIGHTS["practice_area"] +
        br_score * WEIGHTS["brand"] +
        mk_score * WEIGHTS["market"] +
        fi_score * WEIGHTS["financial"] +
        gr_score * WEIGHTS["growth"],
        1,
    )

    # Investment tier
    if composite >= 85:
        tier = "Strong Buy"
        signal = "strong_buy"
    elif composite >= 70:
        tier = "Buy"
        signal = "buy"
    elif composite >= 60:
        tier = "Monitor"
        signal = "monitor"
    elif composite >= 50:
        tier = "Weak Buy"
        signal = "weak_buy"
    else:
        tier = "Pass"
        signal = "pass"

    # Merge top highlights (max 6)
    all_reasons = pa_reasons + ma_reasons + br_reasons + mk_reasons + fi_reasons + gr_reasons
    highlights = [r for r in all_reasons if not r.startswith("WARNING") and len(r) > 10][:6]
    warnings = [r for r in all_reasons if r.startswith("WARNING")]

    return {
        "composite": composite,
        "investment_tier": tier,
        "signal": signal,
        "sub_scores": {
            "practice_fit": pa_score,
            "brand_quality": br_score,
            "market_position": mk_score,
            "financial_health": fi_score,
            "growth_momentum": gr_score,
            "ma_readiness": ma_score,
        },
        "why_highlights": highlights + warnings,
    }
