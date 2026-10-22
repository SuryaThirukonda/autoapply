"""Deterministic triage of SimplifyJobs rows (ported from Hermes sweeplib.classify; those rules
correctly rejected 309 listings). No model is involved."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from urllib.parse import urlparse

from .answers import norm
from .config import SETTINGS

QUEUED = "queued"
MANUAL = "manual"          # a portal the agent has no adapter/lane for -> Surya applies himself
UNSURE = "unsure"
REJECTED = "rejected"
SKIPPED = "skipped"

LANE_NO_ACCOUNT = "no_account"
LANE_ACCOUNT = "account"
LANE_READY_FOR_YOU = "ready_for_you"   # Lever: uniformly hCaptcha-gated -> fill, then park

LANES = {
    "greenhouse": LANE_NO_ACCOUNT,
    "ashby": LANE_NO_ACCOUNT,
    "lever": LANE_READY_FOR_YOU,
    "workday": LANE_ACCOUNT,
    "oracle": LANE_ACCOUNT,
    "icims": LANE_ACCOUNT,
    # the agent applies through these too (NetApp and Corning on SuccessFactors, five SmartRecruiters and sixteen
    # company-hosted forms confirmed by 2026-10-21); Eightfold stays manual: none of its six jobs got through
    "successfactors": LANE_ACCOUNT,
    "smartrecruiters": LANE_NO_ACCOUNT,
    "other": LANE_ACCOUNT,
}


@lru_cache(maxsize=1)
def rules() -> dict:
    return json.loads((SETTINGS.config_dir / "triage_rules.json").read_text(encoding="utf-8"))


def detect_ats(url: str, r: dict | None = None) -> str:
    r = r or rules()
    u = (url or "").lower()
    for name, needles in r.get("ats", {}).items():
        if any(n in u for n in needles):
            return name
    return "other"


def lane_for(ats: str) -> str | None:
    return LANES.get(ats)


ALIGNED = "aligned"     # the roles Surya named first (2026-10-21)
NEUTRAL = "neutral"
OFF_ROLE = "off_role"


def role_fit(title: str, r: dict | None = None) -> str:
    """How well a title fits the roles Surya wants first: SWE, SDE, application development, programming, technology,
    data science, data engineering and data analytics. A title naming one of those is 'aligned' even when it also names
    another field ('Marketing Analytics Intern'); one naming only another field ('Mechanical Engineer Intern',
    'Business Operations Engineer Intern') is 'off_role'; the rest are 'neutral'."""
    r = r or rules()
    t = (title or "").lower()
    if r.get("aligned_title_pattern") and re.search(r["aligned_title_pattern"], t, re.I):
        return ALIGNED
    if r.get("off_role_title_pattern") and re.search(r["off_role_title_pattern"], t, re.I):
        return OFF_ROLE
    return NEUTRAL


def classify(listing: dict, r: dict | None = None) -> tuple[str, str, str, str]:
    """Return (status, reason, ats, variant). Rule order matters: the first failing rule names it."""
    r = r or rules()
    url = listing.get("url") or ""
    title = listing.get("title") or ""
    company = listing.get("company_name") or listing.get("company") or ""
    ats = detect_ats(url, r)
    variant = r.get("categories", {}).get(listing.get("category") or "", "")

    if not listing.get("active", True) or not listing.get("is_visible", True):
        return REJECTED, "inactive_or_hidden", ats, variant
    if not variant:
        return REJECTED, f"category:{listing.get('category')}", ats, variant
    terms = listing.get("terms") or []
    if r.get("required_term") and terms and r["required_term"] not in terms:
        return REJECTED, "term", ats, variant
    degrees = listing.get("degrees") or []
    if degrees and "Bachelor's" not in degrees:
        return REJECTED, "advanced_degree_only", ats, variant
    low_title = title.lower()
    for name, pat in r.get("reject_title_patterns", {}).items():
        if re.search(pat, low_title, re.I):
            return REJECTED, f"title:{name}", ats, variant
    locs = [str(x) for x in (listing.get("locations") or [])]
    if locs:
        foreign = r.get("foreign_location_patterns", [])
        if all(any(re.search(p, loc, re.I) for p in foreign) for loc in locs):
            return REJECTED, "location_outside_us", ats, variant
    host = urlparse(url).netloc.lower() if url else ""
    if any(b in host for b in r.get("blocked_url_hosts", [])):
        return SKIPPED, "blocked_host", ats, variant
    # whole words: 'ibm' (Surya, 2026-10-22: "Don't add IBM roles to the queue") must not block 'Kibmo Labs'
    if any(re.search(r"\b" + re.escape(norm(b)) + r"\b", norm(company)) for b in r.get("blocked_companies", [])):
        return SKIPPED, "do_not_reapply", ats, variant
    for name, pat in r.get("unsure_title_patterns", {}).items():
        if re.search(pat, low_title, re.I):
            return UNSURE, f"title:{name}", ats, variant
    relevant = r.get("relevant_title_pattern")
    if relevant and not re.search(relevant, low_title, re.I):
        return UNSURE, "title:not_obviously_technical", ats, variant
    if role_fit(title, r) == OFF_ROLE:
        return UNSURE, "title:off_role", ats, variant
    if lane_for(ats):
        return QUEUED, "", ats, variant
    return MANUAL, f"portal:{ats}", ats, variant
