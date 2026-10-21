"""Writer: short, honest, personal emails. The model writes the first one; follow-ups are fixed and short.

Rules the model is held to (and checked in code): 45 to 150 words, plain text, no invented facts about the
business, no hype, no fake familiarity, the demo link exactly once. If a draft fails the checks twice, a plain
template is used instead. The legal footer is added in code, never by the model.
"""
from __future__ import annotations

import html
import re

from . import config, llm

LINK = "{DEMO_LINK}"
FOOT_MARK = "\n\n--\n"   # everything after this is the small print

SYSTEM = """You write a short first email from {sender} to a small UK business they have never spoken to.
{sender} has set up a free demo for them: a chat assistant that answers customers' questions using only what's
on their own website.

Write like a real person: warm, plain British English, specific to this business, no hype, no exclamation
marks, no buzzwords ("revolutionise", "leverage", "cutting-edge", "game-changer", "AI-powered solution"),
no flattery, no claims about their business you weren't given, no prices, no statistics.
Mention one concrete kind of question their customers probably ask, based only on what kind of business it is.
Put the exact text {{DEMO_LINK}} on its own line where the link goes. End with one easy question, then sign off
with just the first name. 60 to 110 words in the body. No footer, no P.S.

Reply with JSON only: {{"subject": "short, low-key subject, under 7 words, sentence case, no clickbait, no emoji",
"body": "..."}}"""

BANNED = re.compile(r"revolutioni[sz]e|leverage|cutting[- ]edge|game[- ]?changer|synerg|unlock|supercharge|"
                    r"!|\[|\]|guarantee|\d+%|lorem|dear sir|to whom it may concern", re.I)


def sender() -> str:
    return config.settings.from_name or "the sender"


def sector_words(sector: str) -> str:
    k, _, v = (sector or "").partition("=")
    v = v.replace("_", " ")
    return {"shop": f"{v} shop" if v and v != "yes" else "shop", "amenity": v, "office": f"{v} office",
            "craft": f"{v} business", "healthcare": f"{v} practice", "tourism": v, "leisure": v,
            "beauty": "beauty salon"}.get(k, v or "local business")


def why_not(subject: str, body: str) -> str:
    """'' if the draft is fine, else the reason."""
    words = len(body.split())
    if not subject or len(subject) > 60:
        return "subject missing or too long"
    if body.count(LINK) != 1:
        return f"demo link appears {body.count(LINK)} times"
    if not 45 <= words <= 150:
        return f"{words} words"
    m = BANNED.search(subject + " " + body.replace(LINK, ""))
    return f"banned wording: {m.group(0)!r}" if m else ""


def tidy(subject: str, body: str) -> tuple[str, str]:
    """Fix the small things models do, instead of throwing a good draft away."""
    subject = subject.replace("!", "").strip()
    body = body.replace("!", ".").replace("..", ".")
    if LINK not in body and "DEMO_LINK" in body:
        body = re.sub(r"\{?\s*DEMO_LINK\s*\}?", LINK, body)
    if LINK not in body:  # put the link on its own line before the closing question
        parts = body.rsplit("\n\n", 2)
        body = (parts[0] + f"\n\n{LINK}\n\n" + "\n\n".join(parts[1:])) if len(parts) > 1 else body + f"\n\n{LINK}"
    while body.count(LINK) > 1:
        i = body.rfind(LINK)
        body = (body[:i] + body[i + len(LINK):]).replace("\n\n\n", "\n\n")
    return subject, body.strip()


def first_name() -> str:
    return (config.settings.from_name or "").split(" ")[0] or "Thanks"


def fallback(p: dict) -> tuple[str, str]:
    name = p.get("name") or "your business"
    return (f"Quick idea for {name}"[:60],
            f"Hi,\n\nI build website assistants for small businesses. I've set up a free demo for {name}: a chat "
            f"assistant that answers customers' questions using only what's on your own website, so people get an "
            f"answer at 10pm instead of waiting for a call back.\n\n{LINK}\n\nWould something like that be useful "
            f"for a {sector_words(p.get('sector', ''))} like yours?\n\n{first_name()}")


def first(p: dict) -> tuple[str, str]:
    """(subject, body with {DEMO_LINK}) for a prospect row."""
    facts = (f"Business name: {p.get('name')}\nKind of business: {sector_words(p.get('sector', ''))}\n"
             f"Town: {p.get('city') or 'the UK'}\nWebsite: {p.get('website')}")
    for attempt in range(2):
        try:
            out = llm.chat_json(SYSTEM.format(sender=sender()), facts, temperature=0.5 + 0.1 * attempt, max_tokens=400)
        except Exception:  # noqa: BLE001  # model unavailable: use the template
            break
        subject = re.sub(r"\s+", " ", str(out.get("subject", ""))).strip().strip('"')
        body = re.sub(r"\n{3,}", "\n\n", str(out.get("body", "")).replace("\r", "").strip())
        subject, body = tidy(subject, body)
        if not why_not(subject, body):
            return subject, body
    return fallback(p)


def followup(p: dict, step: int) -> str:
    """Follow-ups are deliberately short and fixed (no model): step 1 and step 2 (the last)."""
    name = p.get("name") or "your business"
    if step == 1:
        offer = f" {config.settings.offer_line}" if config.settings.offer_line else ""
        return (f"Hi,\n\nJust floating this back up in case it got buried. The {name} demo is still here if you'd "
                f"like a look:\n\n{LINK}\n\n{offer.strip()}{' ' if offer else ''}If not, no worries at all."
                f"\n\n{first_name()}")
    return (f"Hi,\n\nLast note from me on this. If a website assistant isn't for {name} right now, that's "
            f"completely fine and I won't email again.\n\nThe demo is here until it expires:\n\n{LINK}\n\n"
            f"Thanks for reading.\n\n{first_name()}")


def finish(body: str) -> str:
    """Swap the bare first-name sign-off for the full signature (OUTREACH_SIGNATURE), if one is set."""
    sig = config.settings.signature
    if not sig:
        return body.rstrip()
    lines = body.rstrip().split("\n")
    if lines and lines[-1].strip().rstrip(",.") in (first_name(), config.settings.from_name):
        lines = lines[:-1]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n\n" + sig


def nice_name(name: str) -> str:
    """'ALL TERRAIN CYCLES LIMITED' (Companies House style) -> 'All Terrain Cycles Ltd'."""
    if name and name.isupper():
        name = " ".join(w if w in ("UK", "GB", "LLP", "PLC", "CIC") else w.capitalize() for w in name.split())
    return re.sub(r"\bLimited$", "Ltd", name or "")


def footer(optout_url: str, legal_name: str) -> str:
    """Who is sending, why this address, and how to stop: in every email."""
    s = config.settings
    who = s.legal_name + (f", company no. {s.company_number}" if s.company_number else "")
    how = f" How I found you: {s.privacy_url}" if s.privacy_url else ""
    return (f"{FOOT_MARK}{who}, {s.address}.\n"
            f"I'm emailing because {nice_name(legal_name) or 'your company'} lists this address on its website. "
            f"Not interested? Unsubscribe here and I won't email again: {optout_url} (or just reply "
            f"\"unsubscribe\").{how}")


def to_html(text: str) -> str:
    """A light HTML twin of the plain email: no images, no tracking pixels, clickable links, small grey footer."""
    body, _, small = text.partition(FOOT_MARK)

    def linkify(seg: str) -> str:
        out = []
        for part in re.split(r"(https?://\S+)", seg):
            if part.startswith(("http://", "https://")):
                url = part.rstrip(".,)")
                label = "Unsubscribe" if "/optout" in url else url.split("://", 1)[1]
                out.append(f'<a href="{html.escape(url)}">{html.escape(label)}</a>' + html.escape(part[len(url):]))
            else:
                out.append(html.escape(part))
        return "".join(out).replace("\n", "<br>")

    paras = "".join(f"<p>{linkify(p)}</p>" for p in body.strip().split("\n\n") if p.strip())
    foot = f'<p style="color:#8a939d;font-size:12px">{linkify(small.strip())}</p>' if small.strip() else ""
    return f'<!doctype html><html><body style="font-family:Arial,sans-serif;font-size:15px">{paras}{foot}</body></html>'
