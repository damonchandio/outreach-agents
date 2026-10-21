"""Postman: sends the emails, safely.

- The first OUTREACH_APPROVE_FIRST emails wait for you to approve them (`outreach-agents drafts` / `approve`).
- Volume ramps up week by week (OUTREACH_RAMP), then stays at OUTREACH_DAILY_CAP. Keep both low.
- Sending days and hours only, spread out with random gaps (never bursts). Follow-ups first, then new ones.
- The suppression list is checked right before every single send.
- Every email: who's sending and from where, why them, a working opt-out link and a one-click unsubscribe header.
- Stops itself on: too many bounces, the sending domain on Spamhaus, provider limit errors, a failed login.
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from . import builder, config, writer
from .store import Store

log = logging.getLogger("outreach_agents.postman")


class SendError(RuntimeError):
    def __init__(self, msg: str, kind: str):
        super().__init__(msg)
        self.kind = kind  # recipient / limit / auth / other / suppressed


# --- volume and timing -----------------------------------------------------------------------------------
def quota(send_days: list[str], today: str) -> int:
    """Emails allowed today, by how many sending days have happened before today."""
    s = config.settings
    week = len([d for d in send_days if d < today]) // 5
    return min(s.ramp[week] if week < len(s.ramp) else s.daily_cap, s.daily_cap)


def _hm(text: str) -> dt.time:
    h, m = (int(x) for x in text.split(":"))
    return dt.time(h, m)


def in_window(now: dt.datetime) -> bool:
    s = config.settings
    return now.weekday() in s.send_days and _hm(s.send_from) <= now.time() < _hm(s.send_until)


def gap_seconds(now: dt.datetime, left_today: int) -> float:
    """Spread what's left over the rest of the window, with jitter. At least a minute between emails."""
    end = dt.datetime.combine(now.date(), _hm(config.settings.send_until))
    room = max(60.0, (end - now).total_seconds())
    return max(60.0, room / max(1, left_today) * random.uniform(0.6, 1.4))


def next_send_day(after: dt.datetime, days: int) -> str:
    """The follow-up date `days` later, moved off non-sending days, at the start of the window."""
    d = after + dt.timedelta(days=days)
    while d.weekday() not in config.settings.send_days:
        d += dt.timedelta(days=1)
    t = _hm(config.settings.send_from)
    return d.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0).astimezone(dt.timezone.utc).isoformat()


# --- messages ---------------------------------------------------------------------------------------------
def optout_url(token: str) -> str:
    return f"{config.settings.public_url}/optout?t={token}"


def demo_link(slug: str) -> str:
    return f"{config.settings.public_url}/demo/{slug}"


def build_message(to: str, subject: str, body: str, token: str, reply_to_id: str = "") -> EmailMessage:
    s = config.settings
    m = EmailMessage()
    m["From"] = formataddr((s.from_name, s.email))
    m["To"] = to
    m["Subject"] = subject
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain=s.email.rpartition("@")[2] or None)
    if token:
        m["List-Unsubscribe"] = f"<{optout_url(token)}>, <mailto:{s.email}?subject=unsubscribe>"
        m["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if reply_to_id:
        m["In-Reply-To"] = reply_to_id
        m["References"] = reply_to_id
    m.set_content(body)
    m.add_alternative(writer.to_html(body), subtype="html")
    return m


def smtp_send(m: EmailMessage) -> None:
    s = config.settings
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=60) as conn:
            conn.starttls(context=ssl.create_default_context())
            conn.login(s.email, s.password)
            refused = conn.send_message(m)
            if refused:
                raise SendError(f"recipient refused: {refused}", "recipient")
    except smtplib.SMTPAuthenticationError as e:
        raise SendError(f"the mail server rejected the login ({e.smtp_code})", "auth") from None
    except smtplib.SMTPRecipientsRefused as e:
        raise SendError(f"recipient refused: {list(e.recipients.values())[:1]}", "recipient") from None
    except smtplib.SMTPResponseException as e:
        text = (e.smtp_error or b"").decode(errors="replace")
        if e.smtp_code in (421, 450, 451, 452) or "limit" in text.lower():
            raise SendError(f"provider limit or busy ({e.smtp_code} {text[:80]})", "limit") from None
        if 500 <= e.smtp_code < 600:
            raise SendError(f"refused ({e.smtp_code} {text[:80]})", "recipient") from None
        raise SendError(f"SMTP error {e.smtp_code}", "other") from None
    except (OSError, smtplib.SMTPException) as e:
        raise SendError(f"couldn't reach the mail server ({type(e).__name__})", "other") from None


def compose(store: Store, p: dict) -> dict:
    """The next email for this prospect: {subject, body, step, reply_to_id}. Builds their demo first if needed."""
    step = int(p.get("step") or 0)
    slug = p.get("demo_slug")
    if not slug:
        slug = builder.build(store, p["website"], p["name"], prospect_id=p["id"])["slug"]
        store.update(p["id"], demo_slug=slug)
    if step == 0:
        subject, body = writer.first(p)
        reply_to = ""
    else:
        prev = store.first_sent(p["id"])
        body = writer.followup(p, step)
        subject = "Re: " + (prev["subject"] if prev else f"A quick demo for {p['name']}")
        reply_to = prev["message_id"] if prev else ""
    body = writer.finish(body.replace(writer.LINK, demo_link(slug)))
    body += writer.footer(optout_url(p["optout_token"]), p.get("legal_name") or p["name"])
    return {"subject": subject, "body": body, "step": step, "reply_to_id": reply_to}


def send_prospect(store: Store, p: dict, mail: dict) -> None:
    """Send one composed email and record it. Raises SendError."""
    kind = "first" if mail["step"] == 0 else "followup"
    if store.suppressed(p["email"]):   # checked at the last moment: an opt-out can arrive at any time
        store.update(p["id"], status="opted_out", next_send_at=None)
        raise SendError(f"{p['email']} is on the suppression list", "suppressed")
    if writer.FOOT_MARK not in mail["body"] or optout_url(p["optout_token"]) not in mail["body"]:
        raise SendError("refusing to send an email without the sender's details and opt-out link", "other")
    m = build_message(p["email"], mail["subject"], mail["body"], p["optout_token"], mail.get("reply_to_id", ""))
    try:
        smtp_send(m)
    except SendError as e:
        store.log_message(p["id"], "out", kind, step=mail["step"], subject=mail["subject"], status="failed",
                          error=str(e)[:300])
        if e.kind == "recipient":
            store.update(p["id"], status="bounced", outcome="bounced", next_send_at=None)
            store.suppress(p["email"], "refused at send")
        raise
    step = mail["step"] + 1
    days = config.settings.followup_days
    more = step <= len(days)
    store.log_message(p["id"], "out", kind, step=mail["step"], subject=mail["subject"], body=mail["body"],
                      message_id=m["Message-ID"], status="sent")
    gap = days[step - 1] - (days[step - 2] if step >= 2 else 0) if more else 0
    store.update(p["id"], status="contacted" if more else "finished", step=step, last_sent_at=writer_now(),
                 next_send_at=next_send_day(dt.datetime.now().astimezone(), gap) if more else None)


def writer_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# --- stop rules ------------------------------------------------------------------------------------------
def bounce_rate(store: Store) -> tuple[float, int]:
    sent = store.recent_sent(config.settings.bounce_window)
    if not sent:
        return 0.0, 0
    bounced = sum((store.prospect(pid) or {}).get("status") == "bounced" for pid in set(sent))
    return bounced / len(sent), len(sent)


def on_spamhaus() -> bool | None:
    """True if the sending domain is on the Spamhaus domain blocklist; None if Spamhaus wouldn't say
    (it refuses lookups through big public DNS resolvers)."""
    from . import contacts
    dom = config.settings.email.rpartition("@")[2]
    try:
        ans = [str(a) for a in contacts._dns().resolve(f"{dom}.dbl.spamhaus.org", "A", lifetime=8)]
    except Exception:  # noqa: BLE001  (NXDOMAIN = not listed)
        return False
    if any(a.startswith("127.255.255.") for a in ans):
        return None
    return any(a.startswith("127.0.1.") for a in ans)


def safety_checks(store: Store, state: dict, today: str) -> str:
    """'' if it's fine to send, else why not."""
    s = config.settings
    if state.get("dbl_checked") != today:
        state["dbl_checked"] = today
        if on_spamhaus():
            return "the sending domain is on the Spamhaus blocklist"
    rate, n = bounce_rate(store)
    if n >= s.bounce_min_sends and rate > s.max_bounce_rate and round(rate * n) >= s.bounce_min_count:
        return f"{rate:.0%} of the last {n} emails bounced (limit {s.max_bounce_rate:.0%})"
    return ""


# --- approval of the first emails ---------------------------------------------------------------------------
def queue_for_approval(store: Store, p: dict, mail: dict) -> None:
    store.set_state(f"draft:{p['id']}", mail)
    store.update(p["id"], status="awaiting_approval")


def drafts(store: Store) -> list[tuple[dict, dict]]:
    return [(p, store.get_state(f"draft:{p['id']}")) for p in store.by_status("awaiting_approval", 50)]


def approve(store: Store, pid: str, new_body: str = "") -> None:
    mail = store.get_state(f"draft:{pid}")
    if mail is None:
        raise KeyError(pid)
    if new_body:   # your own words, keeping the link, signature and legal footer
        foot = mail["body"][mail["body"].find(writer.FOOT_MARK):]
        mail["body"] = writer.finish(new_body.strip()) + foot
        store.set_state(f"draft:{pid}", mail)
    store.update(pid, status="approved")
    state = store.get_state("postman", {})
    state["approved"] = state.get("approved", 0) + 1
    store.set_state("postman", state)


def skip(store: Store, pid: str) -> None:
    store.update(pid, status="skipped")


# --- the loop ------------------------------------------------------------------------------------------------
def tick(store: Store, now: dt.datetime | None = None) -> str:
    """Call this every so often (say every 15 seconds). Sends at most one email. Returns what it did."""
    now = now or dt.datetime.now()
    s = config.settings
    if s.missing():
        return "setup needed: " + ", ".join(s.missing())
    state = store.get_state("postman", {})
    today = now.date().isoformat()
    if state.get("paused"):
        if state.get("paused_until") and today >= state["paused_until"]:
            state.update(paused="", paused_until="")
        else:
            return "paused: " + state["paused"]
    if state.get("today") != today:
        state.update(today=today, sent_today=0, next_at=0.0)
    try:
        if not in_window(now):
            return "outside sending hours"
        q = quota(state.get("send_days", []), today)
        if state["sent_today"] >= q:
            return f"done for today ({state['sent_today']}/{q})"
        if time.time() < state.get("next_at", 0):
            return "waiting for the next slot"
        why = safety_checks(store, state, today)
        if why:
            state["paused"] = why
            return "paused: " + why
        rows = store.due(5)
        if state.get("approved", 0) < s.approve_first:
            fresh = [r for r in rows if r["status"] == "ready"]
            waiting = store.count("awaiting_approval")
            if fresh and waiting < 3 and state.get("approved", 0) + waiting < s.approve_first:
                p = fresh[0]
                try:
                    queue_for_approval(store, p, compose(store, p))
                except builder.DemoError as e:
                    store.update(p["id"], status="rejected", reject_reason=f"demo failed: {e}"[:200])
            rows = [r for r in rows if r["status"] in ("approved", "contacted")]
        if not rows:
            return "nothing to send right now"
        p = rows[0]
        mail = store.get_state(f"draft:{p['id']}") if p["status"] == "approved" else None
        try:
            mail = mail or compose(store, p)
            send_prospect(store, p, mail)
        except builder.DemoError as e:
            store.update(p["id"], status="rejected", reject_reason=f"demo failed: {e}"[:200])
            return f"skipped {p['name']}: {e}"
        except SendError as e:
            if e.kind == "limit":
                state.update(paused=str(e), paused_until=(now.date() + dt.timedelta(days=1)).isoformat())
            elif e.kind == "auth":
                state["paused"] = str(e)
            elif e.kind != "suppressed":
                state["next_at"] = time.time() + 300
            return f"not sent: {e}"
        days = state.get("send_days", [])
        if today not in days:
            state["send_days"] = (days + [today])[-400:]
        state["sent_today"] += 1
        state["next_at"] = time.time() + gap_seconds(now, q - state["sent_today"])
        return f"sent to {p['name']} ({state['sent_today']}/{q} today)"
    finally:
        store.set_state("postman", state)
