"""`outreach-agents serve`: the demo pages and the opt-out page.

GET  /demo/<slug>        the business's demo chatbot (labelled as a demo, hidden from search engines)
POST /demo/<slug>/chat   {"message": "..."} -> {"answer": "..."}
GET  /optout?t=TOKEN     asks "unsubscribe?" with one button. It never unsubscribes by itself: email security
                         scanners open every link in an email, and a GET that unsubscribed would opt people out
                         who never clicked anything.
POST /optout?t=TOKEN     unsubscribes (the button, and mail apps' one-click List-Unsubscribe-Post).
"""
from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import builder
from .store import Store

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta name="robots" content="noindex"><title>{name} assistant (demo)</title>
<style>body{{font-family:Arial,sans-serif;max-width:640px;margin:24px auto;padding:0 16px}}#log p{{margin:8px 0}}
.me{{text-align:right}}input{{width:75%;padding:8px}}button{{padding:8px 14px}}small{{color:#777}}</style></head>
<body><h2>{name}</h2><small>A demo assistant built from {name}'s public website. Not affiliated with {name}.</small>
<div id="log"><p>{welcome}</p></div><p>{chips}</p>
<form id="f"><input id="q" placeholder="Ask a question" maxlength="300"><button>Ask</button></form>
<script>
const log=document.getElementById('log'),q=document.getElementById('q');
function add(t,c){{const p=document.createElement('p');p.textContent=t;if(c)p.className=c;log.appendChild(p)}}
async function ask(t){{add(t,'me');const r=await fetch(location.pathname+'/chat',{{method:'POST',
headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{message:t}})}});add((await r.json()).answer)}}
document.getElementById('f').onsubmit=e=>{{e.preventDefault();if(q.value.trim())ask(q.value.trim());q.value=''}};
document.querySelectorAll('[data-q]').forEach(b=>b.onclick=()=>ask(b.dataset.q));
</script></body></html>"""

OPTOUT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex">
<title>Unsubscribe</title></head><body style="font-family:Arial,sans-serif;max-width:520px;margin:40px auto">
{body}</body></html>"""


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Robots-Tag", "noindex")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/optout":
                token = parse_qs(u.query).get("t", [""])[0]
                if not store.by_token(token):
                    return self._send(404, OPTOUT.format(body="<p>That link isn't valid.</p>"))
                form = (f'<p>Stop all emails to this address?</p><form method="post" action="/optout?t='
                        f'{html.escape(token)}"><button>Yes, unsubscribe me</button></form>')
                return self._send(200, OPTOUT.format(body=form))
            if u.path.startswith("/demo/"):
                d = store.demo(u.path.split("/")[2])
                if not d:
                    return self._send(404, "Not found")
                chips = " ".join(f'<button data-q="{html.escape(s)}">{html.escape(s)}</button>' for s in d["suggestions"])
                return self._send(200, PAGE.format(name=html.escape(d["business_name"]),
                                                   welcome=html.escape(d["welcome"]), chips=chips))
            self._send(404, "Not found")

        def do_POST(self):
            u = urlparse(self.path)
            n = min(int(self.headers.get("Content-Length") or 0), 4000)
            raw = self.rfile.read(n)
            if u.path == "/optout":
                p = store.by_token(parse_qs(u.query).get("t", [""])[0])
                if not p:
                    return self._send(404, OPTOUT.format(body="<p>That link isn't valid.</p>"))
                store.suppress(p["email"], "unsubscribed via link")
                store.update(p["id"], status="opted_out", outcome="opted_out", next_send_at=None)
                return self._send(200, OPTOUT.format(body="<p>Done. You won't get any more emails from us.</p>"))
            if u.path.startswith("/demo/") and u.path.endswith("/chat"):
                try:
                    msg = str(json.loads(raw or b"{}").get("message", ""))[:500]
                    out = builder.answer(store, u.path.split("/")[2], msg) if msg.strip() else {"answer": "Ask me something."}
                except builder.DemoError:
                    return self._send(404, json.dumps({"answer": "This demo has gone."}), "application/json")
                except Exception:  # noqa: BLE001
                    out = {"answer": "Sorry, I couldn't answer just now. Please try again."}
                return self._send(200, json.dumps(out), "application/json")
            self._send(404, "Not found")
    return Handler


def serve(store: Store, host: str = "127.0.0.1", port: int = 8800) -> None:
    ThreadingHTTPServer((host, port), make_handler(store)).serve_forever()
