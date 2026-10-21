"""Scout: finds businesses worth emailing and checks each one properly before it can be emailed.

1. OpenStreetMap (Overpass API, free) lists local businesses that have a website, area by area.
   Chains are skipped: the same website on 2+ shops in an area, or branch/store-locator pages.
2. Their site is read (a few pages): a business email on their own domain, a company number, any chat widget.
   A site that now redirects to another company's domain (bought out, merged) is skipped.
3. Companies House confirms an active limited company (a corporate subscriber). Otherwise: never emailed.
4. The email's domain must have a mail server, and must not be on the suppression list.
Passed: status 'ready'. Failed: status 'rejected' with the reason (kept, so it's never checked twice).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from urllib.parse import urlparse

import requests

from . import companies, config, contacts
from .store import Store

log = logging.getLogger("outreach_agents.scout")
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]

# Businesses where a website assistant earns its keep (lots of repeat customer questions)
KINDS = {
    "amenity": "restaurant|fast_food|cafe|pub|bar|dentist|clinic|doctors|veterinary|driving_school|"
               "childcare|kindergarten|car_rental|car_wash|vehicle_inspection",
    "shop": ".",
    "office": "estate_agent|lawyer|accountant|insurance|financial|architect|consulting|travel_agent|"
              "employment_agency|it|property_management|surveyor|tax_advisor|company",
    "craft": ".",
    "healthcare": ".",
    "tourism": "hotel|guest_house|hostel|apartment|attraction",
    "leisure": "fitness_centre|sports_centre|dance|escape_game|bowling_alley",
    "beauty": ".",
}
SKIP_SHOPS = re.compile(r"^(vacant|yes|no)$")
NOT_THEIR_SITE = re.compile(r"facebook|instagram|linktr|google\.|wix(site)?\.com|just-eat|deliveroo|ubereats|"
                            r"tripadvisor|booking\.com")


def _host(url: str) -> str:
    return (urlparse(url if "://" in url else "https://" + url).hostname or "").removeprefix("www.")


def _query(area: str) -> str:
    name = re.escape(area)
    parts = [f'nwr(area.a)["website"]["name"]["{k}"~"^({v})$"][!"brand:wikidata"];' if v != "." else
             f'nwr(area.a)["website"]["name"]["{k}"][!"brand:wikidata"];' for k, v in KINDS.items()]
    return (f'[out:json][timeout:180];area["boundary"="administrative"]'
            f'["name"~"^(City( and County)? of |Borough of |Metropolitan Borough of |London Borough of |Royal Borough of )?{name}( City)?( Council)?$",i]->.a;'
            f'({"".join(parts)});out tags center;')


def parse_osm(data: dict, area: str) -> list[dict]:
    """Independent businesses with their own website, from an Overpass answer."""
    out, seen = [], set()
    branches: dict[str, int] = {}   # the same website on 2+ shops in one area = a chain's branch pages
    for el in data.get("elements", []):
        h = _host((el.get("tags", {}).get("website") or el.get("tags", {}).get("contact:website") or "").strip())
        if h:
            branches[h] = branches.get(h, 0) + 1
    for el in data.get("elements", []):
        t = el.get("tags", {})
        web = (t.get("website") or t.get("contact:website") or "").strip()
        if not web or not t.get("name") or SKIP_SHOPS.match(t.get("shop", "")):
            continue
        host = _host(web)
        if branches.get(host, 0) > 1 or re.search(r"/(stores?|branch(es)?|locations?|shops?|find-us)/", web, re.I):
            continue
        if not host or host in seen or NOT_THEIR_SITE.search(host):
            continue
        seen.add(host)
        out.append({"osm_id": f"{el['type'][0]}{el['id']}", "name": t["name"][:120], "website": web,
                    "postcode": t.get("addr:postcode", ""), "city": t.get("addr:city", "") or area,
                    "sector": next((f"{k}={t[k]}" for k in KINDS if k in t), ""),
                    "osm_email": t.get("email") or t.get("contact:email") or ""})
    return out


def osm_businesses(area: str) -> list[dict]:
    """Every business with a website in the area (cached for a week: OSM data changes slowly)."""
    cache = config.settings.data_dir / "osm"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{re.sub(r'[^a-z0-9]+', '_', area.lower())}.json"
    if path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
        return json.loads(path.read_text(encoding="utf-8"))
    data = None
    for url in OVERPASS:
        try:
            r = requests.post(url, data={"data": _query(area)}, headers={"User-Agent": config.settings.user_agent},
                              timeout=240)
            if r.ok:
                data = r.json()
                break
        except (requests.RequestException, ValueError) as e:
            log.info("Overpass %s failed (%s)", url, type(e).__name__)
    if data is None:
        raise RuntimeError("OpenStreetMap's Overpass service didn't answer; try again later")
    out = parse_osm(data, area)
    path.write_text(json.dumps(out), encoding="utf-8")
    return out


def base_domain(host: str) -> str:
    """The registrable part of a host: shop.example.co.uk -> example.co.uk, www.example.com -> example.com."""
    parts = host.lower().split(".")
    n = 3 if len(parts) > 2 and parts[-1] == "uk" and parts[-2] in ("co", "org", "ltd", "plc", "me", "net", "ac") else 2
    return ".".join(parts[-n:])


def check(store: Store, b: dict) -> str:
    """Check one business; save it as ready or rejected. Returns the status."""
    def save(status: str, reason: str = "", **extra) -> None:
        store.add_prospect(name=b["name"], website=extra.pop("website", b["website"]).rstrip("/"),
                           osm_id=b.get("osm_id"), postcode=b.get("postcode") or None, city=b.get("city") or None,
                           sector=b.get("sector") or None, status=status, reject_reason=reason[:200] or None, **extra)

    try:
        c = contacts.scan(b["website"])
    except Exception as e:  # noqa: BLE001
        save("rejected", f"couldn't read the site ({type(e).__name__})")
        return "rejected"
    if c.blocked:
        save("rejected", "robots.txt asks bots not to read the site")
        return "rejected"
    if not c.reachable:
        save("rejected", "website didn't load")
        return "rejected"
    if base_domain(contacts._domain(b["website"])) != base_domain(contacts._domain(c.url)):
        # bought by a bigger firm: the email would reach someone else's head office
        save("rejected", f"website now goes to another company's site ({contacts._domain(c.url)})", website=c.url)
        return "rejected"
    if c.has_chatbot:
        save("rejected", "already has a chat widget", website=c.url)
        return "rejected"
    osm_email = contacts.rank([b["osm_email"]], contacts._domain(c.url)) if b.get("osm_email") else []
    emails = list(dict.fromkeys(osm_email + list(c.emails)))
    if not emails:
        save("rejected", "no business email on their own domain", website=c.url)
        return "rejected"
    prof, how = companies.verify(b["name"], b.get("postcode", ""), c.numbers)
    if not prof:
        save("rejected", f"not a confirmed limited company: {how}", website=c.url)
        return "rejected"
    email = next((e for e in emails if not store.suppressed(e) and contacts.accepts_mail(e)), "")
    if not email:
        save("rejected", "email on the suppression list or its domain takes no mail", website=c.url)
        return "rejected"
    save("ready", website=c.url, email=email, company_number=prof.get("company_number"),
         legal_name=prof.get("company_name"), notes=f"Verified: {how}")
    return "ready"


def run(store: Store, limit: int = 40, progress=log.info) -> dict:
    """Check up to `limit` new businesses, area by area. Returns counts."""
    if not config.settings.companies_house_key:
        raise RuntimeError("COMPANIES_HOUSE_API_KEY is not set")
    ids, sites = store.known()
    hosts = {_host(w) for w in sites}
    done = {"checked": 0, "ready": 0, "area": "", "fails": 0}
    areas = config.settings.areas
    start = dt.date.today().toordinal() % max(1, len(areas))   # a different starting area each day
    for area in areas[start:] + areas[:start]:
        try:
            todo = [b for b in osm_businesses(area) if b["osm_id"] not in ids and _host(b["website"]) not in hosts]
        except RuntimeError as e:
            progress(f"{area}: {e}")
            done["fails"] += 1
            if done["fails"] >= 2:    # Overpass is down: try again next run rather than hammering every area
                return done
            continue
        done["area"] = area
        for b in todo:
            if done["checked"] >= limit:
                return done
            done["ready"] += check(store, b) == "ready"
            done["checked"] += 1
    return done
