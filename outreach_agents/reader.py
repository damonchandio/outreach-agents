"""Reader: reads replies in the outreach mailbox (read-only peek: messages stay unread for you).

- Bounces: the address is marked bounced and suppressed for good.
- Out-of-office: the next follow-up waits a week.
- "Unsubscribe" / "remove me" / "not interested": suppressed, instantly and permanently.
- Any other human reply: the sequence stops. The model sorts it and drafts an answer for you to send yourself;
  interested replies and questions are passed to `notify` (prints by default; plug in Slack, Telegram, email).
"""
from __future__ import annotations

import datetime as dt
import email
import imaplib
import logging
import re
from email import policy
from email.utils import parseaddr

from . import config, llm
from .store import Store

log = logging.getLogger("outreach_agents.reader")

OPTOUT = re.compile(r"\b(unsubscribe|remove (me|us)|take (me|us) off|stop (emailing|contacting|sending)|"
                    r"opt[- ]?out|do not (email|contact)|don'?t (email|contact)|no thanks|not interested|"
                    r"gdpr|spam)\b", re.I)
OOO = re.compile(r"out of (the )?office|automatic reply|auto[- ]?reply|autoreply|on (annual )?leave|"
                 r"away from (the|my) (office|desk)|holiday", re.I)
BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster|mail delivery|delivery status", re.I)

SORT = """You sort replies to a short cold email offering a small business a free demo of a website chat
assistant. Reply with JSON only:
{"label": "interested" | "question" | "not_interested" | "other",
 "summary": "one plain sentence: what they said",
 "draft": "a short, warm reply (under 90 words, plain British English, no hype). For interested: thank them and
 suggest a 15-minute call. For a question: answer only if the answer is in the original offer, otherwise say you'll
 find out. For not_interested: a one-line polite thanks. Never promise anything not in the offer."}"""


def notify(prospect: dict, label: str, summary: str, draft: str) -> None:
    print(f"[{label}] {prospect['name']} <{prospect['email']}>: {summary}\n--- draft reply ---\n{draft}\n")


def text_of(msg) -> str:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        body = part.get_content() if part else ""
    except Exception:  # noqa: BLE001
        body = ""
    if "<" in body and ">" in body:
        body = re.sub(r"<[^>]+>", " ", body)
    # keep only their new words, not our quoted email underneath
    cut = re.split(r"\n\s*(On .{5,120}wrote:|-----\s*Original Message|From: .*\n.*Sent: |>\s)", body, maxsplit=1)
    return re.sub(r"[ \t]+", " ", cut[0]).strip()[:4000]


def find_prospect(store: Store, msg) -> dict | None:
    ids = " ".join(filter(None, [msg.get("In-Reply-To", ""), msg.get("References", "")]))
    for mid in re.findall(r"<[^>]+>", ids):
        pid = store.prospect_for_message(mid)
        if pid:
            return store.prospect(pid)
    sender = parseaddr(msg.get("From", ""))[1].lower()
    return store.by_email(sender) if sender else None


def bounced_address(msg) -> str:
    for h in ("X-Failed-Recipients", "Final-Recipient", "Original-Recipient"):
        v = msg.get(h, "")
        if v:
            return v.split(";")[-1].strip().lower()
    raw = msg.as_string()
    m = re.search(r"(?:Final|Original)-Recipient:\s*rfc822;\s*([^\s>]+)", raw, re.I)
    if m:
        return m.group(1).lower()
    m = re.search(r"<?([A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})>?\s*(?:\(|:)?\s*(?:host|user|mailbox|"
                  r"address|recipient)", raw, re.I)
    return m.group(1).lower() if m else ""


def hard_bounce(msg) -> bool:
    raw = msg.as_string()
    m = re.search(r"Status:\s*([245])\.\d+\.\d+", raw)
    if m:
        return m.group(1) == "5"
    return bool(re.search(r"\b5\.\d\.\d\b|user unknown|does not exist|no such user|mailbox unavailable|"
                          r"address rejected|recipient rejected", raw, re.I))


def handle(store: Store, msg) -> str:
    sender = parseaddr(msg.get("From", ""))[1].lower()
    if sender == config.settings.email.lower():
        return "own"
    subject = str(msg.get("Subject", ""))
    mid = msg.get("Message-ID")
    # --- bounces
    if BOUNCE_FROM.search(sender + " " + str(msg.get("From", ""))) or msg.get_content_type() == "multipart/report":
        addr = bounced_address(msg)
        if not addr:
            return "bounce (no address found)"
        if not hard_bounce(msg):
            return f"soft bounce {addr}"
        store.suppress(addr, "hard bounce")
        p = store.by_email(addr)
        if p:
            store.update(p["id"], status="bounced", outcome="bounced", next_send_at=None)
            store.log_message(p["id"], "in", "bounce", subject=subject[:200], status="received", message_id=mid)
        return f"hard bounce {addr}"
    p = find_prospect(store, msg)
    if not p:
        return "not outreach"
    text = text_of(msg)
    # --- out of office
    auto = (msg.get("Auto-Submitted", "no").lower() != "no" or msg.get("X-Autoreply") or msg.get("X-Autorespond")
            or msg.get("Precedence", "").lower() in ("auto_reply", "bulk", "junk"))
    if auto or OOO.search(subject):
        store.log_message(p["id"], "in", "ooo", subject=subject[:200], status="received", message_id=mid)
        if p["status"] == "contacted":
            later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat()
            store.update(p["id"], next_send_at=later)
        return f"out of office: {p['name']}"
    # --- a person replied: stop the sequence whatever they said
    store.update(p["id"], status="replied", replied_at=dt.datetime.now(dt.timezone.utc).isoformat(), next_send_at=None)
    if OPTOUT.search(text[:600]) or OPTOUT.search(subject):
        store.suppress(p["email"], "asked not to be emailed")
        store.update(p["id"], status="opted_out", outcome="opted_out")
        store.log_message(p["id"], "in", "optout", subject=subject[:200], status="received", message_id=mid)
        return f"opted out: {p['name']}"
    try:
        sort = llm.chat_json(SORT, f"Business: {p['name']}\nTheir reply:\n{text[:2500]}", max_tokens=350)
    except Exception:  # noqa: BLE001
        sort = {}
    label = sort.get("label") if sort.get("label") in ("interested", "question", "not_interested", "other") else "other"
    store.log_message(p["id"], "in", label, subject=subject[:200], body=text[:4000], status="received", message_id=mid)
    if label == "not_interested":
        store.suppress(p["email"], "said not interested")
        store.update(p["id"], status="not_interested", outcome="not_interested")
        return f"not interested: {p['name']}"
    store.update(p["id"], outcome=label)
    notify(p, label, str(sort.get("summary", "")), str(sort.get("draft") or "").strip())
    return f"{label}: {p['name']}"


def poll(store: Store) -> list[str]:
    """Check the inbox for new mail since last time. Returns what happened to each message."""
    s = config.settings
    st = store.get_state("reader", {"last_uid": 0, "uidvalidity": ""})
    out = []
    with imaplib.IMAP4_SSL(s.imap_host, s.imap_port, timeout=60) as im:
        im.login(s.email, s.password)
        im.select("INBOX", readonly=True)
        _, v = im.response("UIDVALIDITY")
        validity = v[0].decode() if v and v[0] else ""
        if validity != st.get("uidvalidity"):   # first run: start from mail arriving after now
            _, d = im.uid("search", None, "ALL")
            uids = [int(x) for x in (d[0] or b"").split()]
            store.set_state("reader", {"last_uid": max(uids) if uids else 0, "uidvalidity": validity})
            return ["started watching the inbox"]
        _, d = im.uid("search", None, f"UID {st['last_uid'] + 1}:*")
        for uid in sorted(int(x) for x in (d[0] or b"").split() if int(x) > st["last_uid"])[:50]:
            _, parts = im.uid("fetch", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
            try:
                out.append(handle(store, email.message_from_bytes(raw, policy=policy.default)))
            except Exception as e:  # noqa: BLE001
                log.exception("couldn't process message %s", uid)
                out.append(f"error: {type(e).__name__}")
            st["last_uid"] = uid
            store.set_state("reader", st)
    return out
