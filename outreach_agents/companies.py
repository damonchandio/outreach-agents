"""Companies House: is this business an active limited company (a 'corporate subscriber' under PECR)?

Only corporate bodies may be emailed without consent. Sole traders and partnerships are individuals in law,
so anything that can't be positively matched to an active company is never emailed.
Free API: https://developer.company-information.service.gov.uk (key as the basic-auth username).
"""
from __future__ import annotations

import logging
import re
import time

import requests

from . import config

log = logging.getLogger("outreach_agents.companies")
API = "https://api.company-information.service.gov.uk"

# Corporate bodies only. Partnerships (except LLPs) and anything unknown are left out.
CORPORATE_TYPES = {
    "ltd", "plc", "llp", "private-unlimited", "private-unlimited-nsc", "private-limited-guarant-nsc",
    "private-limited-guarant-nsc-limited-exemption", "private-limited-shares-section-30-exemption",
    "old-public-company", "registered-society-non-jurisdictional", "charitable-incorporated-organisation",
    "scottish-charitable-incorporated-organisation", "uk-establishment",
}
NUMBER = re.compile(
    r"\b(?:company|registration|registered|reg\.?|co\.?|crn)\s{0,3}(?:no\.?|number|num\.?|#)?\s{0,3}[:.\-–]?\s{0,3}"
    r"(?:is\s{0,3})?((?:SC|NI|OC|SO|NC|R0|NF|FC)\s?\d{6}|\d{8}|\d{7}|\d{6})\b", re.I)
_last = [0.0]


def _get(path: str, params: dict | None = None) -> dict | None:
    key = config.settings.companies_house_key
    if not key:
        raise RuntimeError("COMPANIES_HOUSE_API_KEY is not set")
    wait = 0.55 - (time.monotonic() - _last[0])  # stay well under 600 requests per 5 minutes
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.monotonic()
    for attempt in range(3):
        try:
            r = requests.get(API + path, params=params, auth=(key, ""), timeout=20)
        except requests.RequestException as e:
            log.info("Companies House unreachable (%s)", type(e).__name__)
            time.sleep(3)
            continue
        if r.status_code == 404:
            return None
        if r.status_code == 429:
            time.sleep(60)
            continue
        if r.status_code == 401:
            raise RuntimeError("Companies House rejected the API key (check COMPANIES_HOUSE_API_KEY)")
        if r.ok:
            return r.json()
        log.info("Companies House said %s for %s", r.status_code, path)
        return None
    return None


def numbers_in(text: str) -> list[str]:
    """Company numbers written on a page ('Company No. 01234567', 'Registered in England No 1234567')."""
    out = []
    for m in NUMBER.finditer(text or ""):
        raw = m.group(1).replace(" ", "").upper()
        if raw.isdigit():
            if len(raw) < 6:
                continue
            raw = raw.zfill(8)
        out.append(raw)
    return list(dict.fromkeys(out))


def profile(number: str) -> dict | None:
    return _get(f"/company/{number}")


def _plain(name: str) -> str:
    n = name.lower().replace("&", " and ")
    n = re.sub(r"\b(the|limited|ltd|llp|plc|uk|co|company)\b", " ", n)
    return re.sub(r"[^a-z0-9]+", "", n)


def _outward(postcode: str) -> str:
    return (postcode or "").upper().replace(" ", "")[:-3] if postcode and len(postcode.replace(" ", "")) > 4 else ""


def is_corporate(p: dict) -> bool:
    return p.get("company_status") == "active" and p.get("type") in CORPORATE_TYPES


def verify(name: str, postcode: str, numbers: list[str]) -> tuple[dict | None, str]:
    """(company profile, how it was matched) or (None, why not).

    1. A company number published on their own website, active and corporate: accepted.
    2. Otherwise a name search: exactly one active corporate company with the same name AND the same
       postcode area as the business. Anything less certain is rejected.
    """
    for num in numbers[:3]:
        p = profile(num)
        if p and is_corporate(p):
            return p, f"company number {num} on their website"
        if p:
            return None, f"company {num} is {p.get('company_status')} / {p.get('type')}"
    if not name:
        return None, "no name to search"
    res = _get("/search/companies", {"q": name, "items_per_page": 10}) or {}
    want, out = _plain(name), _outward(postcode)
    hits = [i for i in res.get("items", [])
            if _plain(i.get("title", "")) == want and i.get("company_status") == "active"]
    if len(hits) != 1:
        return None, "no single exact company match" if not hits else "several companies with that name"
    hit = hits[0]
    office = (hit.get("address") or {}).get("postal_code", "")
    if not out or _outward(office) != out:
        return None, "name matches but the postcode area doesn't"
    p = profile(hit["company_number"])
    if p and is_corporate(p):
        return p, "exact name and postcode match"
    return None, "the matching company isn't an active limited company"
