"""Read a few pages of a business's site: their published business email, company number, whether they
already have a chat widget. Polite like the demo crawler (robots.txt, identified, a handful of pages)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from . import companies, crawl

log = logging.getLogger("outreach_agents.contacts")

# bounded and anchored: an unbounded pattern can spend minutes on one odd page
EMAIL = re.compile(r"(?<![A-Za-z0-9._%+'-])[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,4}\.[A-Za-z]{2,24}\b")
MAX_SCAN = 600_000  # characters of each cleaned page we search


def _clean(html: str) -> str:
    """Make a page safe to search: trim it, drop huge unbroken runs (inline images, fonts, minified code)."""
    html = re.sub(r"[^\s<>\"']{200,}", " ", html)
    return re.sub(r"\s{2,}", " ", html)[:MAX_SCAN]


CONTACTISH = re.compile(r"contact|about|get-in-touch|find-us|enquir|terms|privacy|legal|imprint|company", re.I)
ROLE = ["info", "hello", "enquiries", "enquiry", "contact", "office", "admin", "sales", "bookings", "booking",
        "reception", "team", "mail", "help", "support", "orders"]
BAD_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|bounce|mailer|postmaster|abuse|webmaster|privacy|gdpr|dpo|"
                       r"data|careers|jobs|recruit|accounts|invoices?|billing|press|media|example|test|user|name|"
                       r"your-?email|you|email)$", re.I)
BAD_DOMAIN = re.compile(r"(example\.|sentry|wixpress|wix\.com|godaddy|squarespace|domain\.com|email\.com|"
                        r"yourdomain|website\.com|mysite|ingest\.|\.png$|\.jpe?g$|\.gif$|\.webp$|\.svg$)", re.I)
FREEMAIL = re.compile(r"@(gmail|googlemail|hotmail|outlook|live|yahoo|ymail|icloud|me|aol|btinternet|sky|"
                      r"virginmedia|talktalk|protonmail|proton|gmx|mail)\.", re.I)
CHAT_WIDGETS = re.compile(r"tidio|intercom|driftt?\.com|crisp\.chat|livechatinc|tawk\.to|hs-scripts|"
                          r"zendesk|freshchat|chatbase|manychat|botpress|landbot|olark|smartsupp|userlike|"
                          r"chatra|livechat|jivosite|wati\.io|collect\.chat", re.I)
MAX_PAGES = 5


@dataclass
class Contacts:
    url: str
    emails: list[str] = field(default_factory=list)
    numbers: list[str] = field(default_factory=list)
    has_chatbot: bool = False
    blocked: bool = False
    reachable: bool = False


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def clean_email(e: str) -> str:
    return e.strip().strip(".,;:'\"()<>[]").lower()


def rank(emails: list[str], site_domain: str) -> list[str]:
    """Usable business addresses, best first: on their own domain, role addresses before named people."""
    good = []
    for e in dict.fromkeys(clean_email(x) for x in emails):
        local, _, dom = e.partition("@")
        if not local or not dom or BAD_LOCAL.match(local) or BAD_DOMAIN.search(e) or FREEMAIL.search(e):
            continue
        same = dom == site_domain or dom.endswith("." + site_domain) or site_domain.endswith("." + dom)
        if not same:
            continue  # only addresses on the business's own domain: clearly theirs, clearly a business
        score = (ROLE.index(local) if local in ROLE else len(ROLE) + 1)
        good.append((score, e))
    return [e for _, e in sorted(good)]


def scan(url: str) -> Contacts:
    url = crawl.normalise(url)
    f = crawl.Fetcher()
    root = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
    f.load_robots(root)
    out = Contacts(url=url)
    if not f.allowed(url):
        out.blocked = True
        return out
    got = f.get(url)
    if not got:
        return out
    out.reachable = True
    final, html = got
    out.url = final
    pages = [html]
    links = re.findall(r'href=["\']([^"\']+)["\']', html, re.I)
    seen = {final.rstrip("/")}
    for href in links:
        if len(pages) >= MAX_PAGES:
            break
        u = urljoin(final, href.strip())
        if not u.startswith("http") or _domain(u) != _domain(final) or not CONTACTISH.search(urlparse(u).path):
            continue
        if u.rstrip("/") in seen or crawl.SKIP.search(u.replace("privacy", "").replace("terms", "")):
            continue
        seen.add(u.rstrip("/"))
        g = f.get(u)
        if g:
            pages.append(g[1])
    text = "\n".join(_clean(p) for p in pages)
    mailtos = re.findall(r'mailto:([^"\'?>\s]+)', text, re.I)
    decoded = re.sub(r" ?(\[at\]|\(at\)|&#64;|&commat;) ?", "@", text, flags=re.I)
    out.emails = rank(mailtos + EMAIL.findall(decoded), _domain(final))
    plain = re.sub(r"<[^>]+>", " ", text)
    out.numbers = companies.numbers_in(plain)
    out.has_chatbot = bool(CHAT_WIDGETS.search(text))
    return out


def _dns():
    import dns.resolver  # dnspython
    return dns.resolver


def accepts_mail(email: str) -> bool:
    """The address's domain has a mail server (MX record). Catches typos and dead domains before sending."""
    dom = email.rpartition("@")[2]
    try:
        r = _dns()
        ans = r.resolve(dom, "MX", lifetime=8)
        return any(str(a.exchange).strip(".") for a in ans)
    except Exception as e:  # noqa: BLE001
        log.info("no MX for %s (%s)", dom, type(e).__name__)
        return False
