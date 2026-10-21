"""Read a business's public website politely: robots.txt respected, a few pages at most, identified as a bot.

crawl(url) -> Site: the pages' text (boilerplate repeated on every page kept once) and the business's
structured data (JSON-LD: address, phone, opening hours...).
Nothing here logs in, submits forms or reads anything a visitor couldn't.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import requests

from . import config

log = logging.getLogger("outreach_agents.crawl")


def ua() -> str:
    return config.settings.user_agent


MAX_PAGES = 12
MAX_BYTES = 2_000_000
PAGE_CHARS = 20_000
# pages worth reading first (what customers ask about)
GOOD = re.compile(r"menu|food|order|takeaway|price|pricing|fees|tariff|services?|treatments?|about|contact|"
                  r"find-us|location|opening|hours|faq|questions|book|booking|appointments?|delivery|"
                  r"rooms?|stay|classes|timetable|membership|team|lettings?|landlords?|tenants?|sales", re.I)
SKIP = re.compile(r"privacy|cookie|terms|policy|login|log-in|sign-?in|register|account|cart|basket|checkout|"
                  r"wp-admin|wp-login|feed|/tag/|/category/|/author/|\?|\.(pdf|jpe?g|png|gif|webp|svg|zip|mp4|"
                  r"mp3|docx?|xlsx?|css|js|xml)$", re.I)
BLOCK_TAGS = {"p", "div", "section", "article", "li", "ul", "ol", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5",
              "h6", "br", "hr", "table", "header", "footer", "main", "aside", "nav", "dd", "dt", "address",
              "blockquote", "figcaption", "label", "option"}
HIDDEN_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "head"}


@dataclass
class Page:
    url: str
    title: str
    text: str


@dataclass
class Site:
    url: str
    name: str = ""
    description: str = ""
    pages: list[Page] = field(default_factory=list)
    facts: str = ""            # from JSON-LD, written as plain sentences
    blocked: bool = False      # robots.txt says no

    @property
    def total_chars(self) -> int:
        return sum(len(p.text) for p in self.pages)


class _Parser(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.meta: dict[str, str] = {}
        self.icons: list[tuple[str, str]] = []
        self.jsonld: list[str] = []
        self.styles: list[str] = []
        self.stylesheets: list[str] = []
        self.title = ""
        self._hidden = 0
        self._in_title = False
        self._in_jsonld = False
        self._in_style = False
        self._a_href: str | None = None
        self._a_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link":
            rel = a.get("rel", "").lower()
            href = a.get("href", "")
            if "icon" in rel and href:
                self.icons.append((rel + " " + a.get("sizes", ""), urljoin(self.base, href)))
            if "stylesheet" in rel and href:
                self.stylesheets.append(urljoin(self.base, href))
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_jsonld = True
        elif tag == "style":
            self._in_style = True
        if tag in HIDDEN_TAGS:
            self._hidden += 1
        if tag == "a" and a.get("href"):
            self._a_href = urljoin(self.base, a["href"])
            self._a_text = []
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "img" and a.get("alt") and not self._hidden:
            alt = a["alt"].strip()
            if 3 < len(alt) < 120:
                self.parts.append(f" {alt} ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "script":
            self._in_jsonld = False
        if tag == "style":
            self._in_style = False
        if tag in HIDDEN_TAGS and self._hidden:
            self._hidden -= 1
        if tag == "a" and self._a_href:
            self.links.append((self._a_href, " ".join(self._a_text).strip()))
            self._a_href = None
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_jsonld:
            self.jsonld.append(data)
            return
        if self._in_style:
            self.styles.append(data)
            return
        if self._hidden:
            return
        self.parts.append(data)
        if self._a_href is not None:
            self._a_text.append(data.strip())

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in raw.split("\n")]
        return "\n".join(ln for ln in lines if len(ln) > 1)


def _same_site(a: str, b: str) -> bool:
    ha, hb = urlparse(a).hostname or "", urlparse(b).hostname or ""
    return ha.removeprefix("www.") == hb.removeprefix("www.")


def normalise(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return urldefrag(url)[0]


class Fetcher:
    def __init__(self, session: requests.Session | None = None):
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": ua(), "Accept": "text/html,application/xhtml+xml",
                               "Accept-Language": "en-GB,en;q=0.8"})
        self.robots: robotparser.RobotFileParser | None = None

    def load_robots(self, base: str) -> None:
        rp = robotparser.RobotFileParser()
        try:
            r = self.s.get(urljoin(base, "/robots.txt"), timeout=10)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            rp.parse([])
        self.robots = rp

    def allowed(self, url: str) -> bool:
        return self.robots is None or self.robots.can_fetch(ua(), url)

    def get(self, url: str) -> tuple[str, str] | None:
        """(final url, html) or None."""
        if not self.allowed(url):
            log.info("robots.txt says no: %s", url)
            return None
        try:
            r = self.s.get(url, timeout=15, stream=True, allow_redirects=True)
        except requests.RequestException as e:
            log.info("couldn't fetch %s: %s", url, e)
            return None
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or "html" not in ctype.lower():
            r.close()
            return None
        body = b""
        for part in r.iter_content(65536):
            body += part
            if len(body) > MAX_BYTES:
                break
        r.close()
        enc = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
        return r.url, body.decode(enc, errors="replace")

    def get_text(self, url: str, limit: int = 300_000) -> str:
        try:
            r = self.s.get(url, timeout=10)
            return r.text[:limit] if r.status_code == 200 else ""
        except requests.RequestException:
            return ""


# --- structured data (JSON-LD) -----------------------------------------------------------------------
def _walk(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
    elif isinstance(node, dict):
        yield node
        for k in ("@graph", "mainEntity", "itemListElement"):
            if k in node:
                yield from _walk(node[k])


def jsonld_facts(blobs: list[str]) -> tuple[str, str]:
    """(plain-English facts, business name) from schema.org data."""
    lines, name = [], ""
    for blob in blobs:
        try:
            data = json.loads(blob.strip())
        except ValueError:
            continue
        for n in _walk(data):
            t = n.get("@type")
            types = " ".join(t) if isinstance(t, list) else str(t or "")
            if not re.search(r"Business|Restaurant|Organization|Store|Dentist|Hotel|Accountant|Gym|Physician|"
                             r"Agent|Service|Clinic|Place", types):
                continue
            if n.get("name") and not name:
                name = str(n["name"])
            addr = n.get("address")
            if isinstance(addr, dict):
                a = ", ".join(str(addr.get(k)) for k in ("streetAddress", "addressLocality", "postalCode")
                              if addr.get(k))
                if a:
                    lines.append(f"Address: {a}.")
            for key, label in (("telephone", "Phone"), ("email", "Email"), ("priceRange", "Price range"),
                               ("servesCuisine", "Cuisine"), ("acceptsReservations", "Takes reservations")):
                v = n.get(key)
                if v:
                    lines.append(f"{label}: {', '.join(map(str, v)) if isinstance(v, list) else v}.")
            oh = n.get("openingHours")
            if oh:
                lines.append("Opening hours: " + ("; ".join(oh) if isinstance(oh, list) else str(oh)) + ".")
            spec = n.get("openingHoursSpecification")
            for s in (spec if isinstance(spec, list) else [spec] if spec else []):
                if isinstance(s, dict) and s.get("opens"):
                    days = s.get("dayOfWeek", "")
                    days = ", ".join(d.rsplit("/", 1)[-1] for d in days) if isinstance(days, list) \
                        else str(days).rsplit("/", 1)[-1]
                    lines.append(f"Open {days}: {s.get('opens')} to {s.get('closes')}.")
    return "\n".join(dict.fromkeys(lines)), name


# --- crawling ------------------------------------------------------------------------------------------
def _score(url: str, text: str) -> int:
    path = urlparse(url).path.lower()
    s = 10 if GOOD.search(path) or GOOD.search(text or "") else 0
    return s - path.count("/")


def crawl(url: str, max_pages: int = MAX_PAGES, fetcher: Fetcher | None = None) -> Site:
    """shortcut: no JavaScript rendering, so sites built entirely in JS come back nearly empty; add a headless
    browser (e.g. Playwright) here if you need them."""
    url = normalise(url)
    f = fetcher or Fetcher()
    root = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
    f.load_robots(root)
    site = Site(url=url)
    if not f.allowed(url):
        site.blocked = True
        return site
    got = f.get(url)
    if not got:
        return site
    final, html = got
    site.url = final
    p = _Parser(final)
    p.feed(html)
    site.description = p.meta.get("description") or p.meta.get("og:description", "")
    facts, ld_name = jsonld_facts(p.jsonld)
    site.name = (p.meta.get("og:site_name") or ld_name or "").strip()
    pages = [(final, p)]
    seen = {final.rstrip("/")}
    queue = sorted({(_score(u, t), u, t) for u, t in p.links
                    if u.startswith("http") and _same_site(u, final) and not SKIP.search(u)},
                   key=lambda x: -x[0])
    for _, u, _t in queue:
        if len(pages) >= max_pages:
            break
        u = urldefrag(u)[0]
        if u.rstrip("/") in seen:
            continue
        seen.add(u.rstrip("/"))
        got = f.get(u)
        if not got:
            continue
        pp = _Parser(got[0])
        pp.feed(got[1])
        pages.append((got[0], pp))
        more, _ = jsonld_facts(pp.jsonld)
        if more:
            facts += "\n" + more
    seen_lines: set[str] = set()
    for page_url, pp in pages:  # nav/footer lines repeat on every page: keep each line once (first page)
        keep = []
        for ln in pp.text().split("\n"):
            key = ln.lower()
            if key in seen_lines and len(ln) < 200:
                continue
            seen_lines.add(key)
            keep.append(ln)
        text = "\n".join(keep)[:PAGE_CHARS]
        if len(text) > 40:
            title = re.sub(r"\s+", " ", pp.title).strip()[:120] or urlparse(page_url).path
            site.pages.append(Page(page_url, title, text))
    site.facts = "\n".join(dict.fromkeys(x for x in facts.split("\n") if x.strip()))
    if not site.name and pages:
        site.name = re.split(r"\s[|\-–—:]\s", re.sub(r"\s+", " ", pages[0][1].title).strip())[0][:80]
    return site


# --- chunks for the vector search ----------------------------------------------------------------------
def chunks(site: Site, size: int = 900, overlap: int = 150, limit: int = 150) -> list[dict]:
    out: list[dict] = []
    if site.facts:
        out.append({"url": site.url, "title": "Key details", "content": site.facts[:2000]})
    for page in site.pages:
        buf = ""
        for line in page.text.split("\n"):
            while len(line) > size:  # one huge line: cut it
                out.append({"url": page.url, "title": page.title, "content": (buf + " " + line[:size]).strip()})
                buf, line = "", line[size - overlap:]
            if len(buf) + len(line) + 1 > size and buf:
                out.append({"url": page.url, "title": page.title, "content": buf.strip()})
                buf = buf[-overlap:].split("\n", 1)[-1] if overlap else ""
            buf += line + "\n"
        if buf.strip():
            out.append({"url": page.url, "title": page.title, "content": buf.strip()})
        if len(out) >= limit:
            break
    return [c for c in out if len(c["content"]) > 30][:limit]
