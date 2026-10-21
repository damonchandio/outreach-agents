"""Builder: a business's web address in, a personalised chatbot demo out.

1. Read their site politely (crawl): up to 12 pages, robots.txt respected.
2. The language model writes the demo's greeting and 4 sample questions, using only what's on the site.
3. The text is kept in small chunks. Questions are answered from the best-matching chunks (retrieval), and the
   model is told to answer only from them, so the demo never invents facts about the business.
4. `outreach-agents serve` shows the demo at /demo/<slug>, clearly labelled as a demo.
"""
from __future__ import annotations

import math
import re
import secrets
from collections import Counter
from urllib.parse import urlparse

from . import crawl, llm
from .store import Store

PROFILE = """You set up a demo website assistant for a UK small business, using ONLY the website text given.
Reply with JSON only:
{"business_name": "the business's name as customers know it",
 "welcome": "the assistant's first message: one friendly sentence naming the business and 2-3 things it can help with",
 "suggestions": ["4 short questions a real customer would ask that the text CLEARLY answers"]}
Never invent facts. If the text doesn't answer something, don't suggest a question about it."""

ANSWER = """You are a demo website assistant for {name}. Answer the customer's question using ONLY the website
extracts below. If they don't contain the answer, say you're not sure and suggest contacting {name} directly.
Short, friendly, plain British English. Never invent prices, times or policies.

Website extracts:
{context}"""


class DemoError(RuntimeError):
    pass


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    s = re.sub(r"-(ltd|limited|llp|plc|uk)$", "", s)
    return (s[:40].strip("-") or "demo") + "-" + "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789")
                                                          for _ in range(4))


def profile(site: crawl.Site, name_hint: str = "") -> dict:
    text = (f"Website: {site.url}\nSite name: {site.name}\nDescription: {site.description}\n"
            f"Known name: {name_hint}\n\nKey details:\n{site.facts}\n\n" +
            "\n\n".join(f"## {p.title}\n{p.text[:2500]}" for p in site.pages[:6]))[:12000]
    try:
        data = llm.chat_json(PROFILE, text, max_tokens=600, temperature=0.3)
    except Exception:  # noqa: BLE001  # the demo still works with a plain greeting
        data = {}
    name = (name_hint or data.get("business_name") or site.name or urlparse(site.url).hostname or "This business")
    sugg = [q.strip() for q in data.get("suggestions", []) if isinstance(q, str) and 5 < len(q) < 90][:4]
    return {"business_name": name.strip()[:80], "suggestions": sugg,
            "welcome": str(data.get("welcome") or f"Hi! Ask me anything about {name}.")[:300]}


def build(store: Store, url: str, name: str = "", prospect_id: str | None = None, slug: str | None = None) -> dict:
    site = crawl.crawl(url)
    if site.blocked:
        raise DemoError("their robots.txt asks bots not to read the site")
    if not site.pages:
        raise DemoError("couldn't read anything from that site")
    if site.total_chars < 400:
        raise DemoError("there's hardly any text on that site, so a demo would have nothing to answer from")
    parts = crawl.chunks(site, limit=150)
    prof = profile(site, name)
    slug = slug or slugify(prof["business_name"])
    store.save_demo(slug, prospect_id=prospect_id, business_name=prof["business_name"], site_url=site.url,
                    welcome=prof["welcome"], suggestions=prof["suggestions"], chunks=parts, status="live")
    return {"slug": slug, "name": prof["business_name"], "pages": len(site.pages), "chunks": len(parts)}


# --- retrieval ------------------------------------------------------------------------------------------
STOP = set("the a an and or to of for in on at is are be it this that what how do does can i you your we our "
           "with from my me any have has".split())


def _terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9£]+", text.lower()) if w not in STOP and len(w) > 1]


def top_chunks(chunks: list[dict], question: str, k: int = 5) -> list[dict]:
    """BM25 over the demo's chunks.
    shortcut: keyword retrieval, no embeddings; swap in a vector index if questions are often worded unlike the site."""
    docs = [_terms(f"{c.get('title', '')} {c.get('content', '')}") for c in chunks]
    if not docs:
        return []
    avg = sum(map(len, docs)) / len(docs)
    df = Counter(t for d in docs for t in set(d))
    q = _terms(question)

    def score(d: list[str]) -> float:
        tf, s = Counter(d), 0.0
        for t in q:
            if tf[t]:
                idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(d) / avg))
        return s
    ranked = sorted(range(len(docs)), key=lambda i: -score(docs[i]))
    return [chunks[i] for i in ranked[:k] if score(docs[i]) > 0]


def answer(store: Store, slug: str, question: str) -> dict:
    d = store.demo(slug)
    if not d:
        raise DemoError("no such demo")
    hits = top_chunks(d["chunks"], question[:500])
    if not hits:
        return {"answer": f"I'm not sure about that one. It's best to ask {d['business_name']} directly.",
                "answered": False}
    context = "\n\n".join(f"[{h.get('title', '')}]\n{h['content']}" for h in hits)
    text = llm.chat(ANSWER.format(name=d["business_name"], context=context), question[:500], max_tokens=350)
    return {"answer": text, "answered": True, "sources": list(dict.fromkeys(h["url"] for h in hits))}

