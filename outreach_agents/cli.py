"""outreach-agents: command line.

    outreach-agents scout [--limit 40]        find and check new businesses
    outreach-agents build URL [NAME]          build one demo by hand
    outreach-agents drafts                    first emails waiting for your approval
    outreach-agents approve ID | skip ID      approve or skip one
    outreach-agents send                      one Postman step (sends at most one email)
    outreach-agents read                      check the inbox once
    outreach-agents run                       Postman + Reader every 15 s, Scout every hour
    outreach-agents serve [--port 8800]       demo pages and the opt-out page
    outreach-agents status                    counts by status
"""
from __future__ import annotations

import argparse
import logging
import time

from . import builder, config, postman, reader, scout, server
from .store import Store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="outreach-agents", description="Personalised B2B outreach, done carefully.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scout").add_argument("--limit", type=int, default=40)
    b = sub.add_parser("build")
    b.add_argument("url")
    b.add_argument("name", nargs="?", default="")
    sub.add_parser("drafts")
    for name in ("approve", "skip"):
        sub.add_parser(name).add_argument("id")
    for name in ("send", "read", "run", "status"):
        sub.add_parser(name)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8800)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    store = Store(config.settings.data_dir / "outreach.db")

    if a.cmd == "scout":
        print(scout.run(store, a.limit, progress=print))
    elif a.cmd == "build":
        r = builder.build(store, a.url, a.name)
        print(f"{r['name']}: {postman.demo_link(r['slug'])} ({r['pages']} pages, {r['chunks']} chunks)")
    elif a.cmd == "drafts":
        for p, mail in postman.drafts(store):
            print(f"== {p['id']}  {p['name']} <{p['email']}>\nSubject: {mail['subject']}\n\n{mail['body']}\n")
    elif a.cmd == "approve":
        postman.approve(store, a.id)
        print("approved")
    elif a.cmd == "skip":
        postman.skip(store, a.id)
        print("skipped")
    elif a.cmd == "send":
        print(postman.tick(store))
    elif a.cmd == "read":
        print("\n".join(reader.poll(store)) or "nothing new")
    elif a.cmd == "serve":
        print(f"serving on http://{a.host}:{a.port}")
        server.serve(store, a.host, a.port)
    elif a.cmd == "status":
        for st in ("ready", "awaiting_approval", "approved", "contacted", "replied", "finished", "opted_out",
                   "bounced", "rejected"):
            print(f"{st:18} {store.count(st)}")
    elif a.cmd == "run":
        last_scout = 0.0
        while True:
            print(postman.tick(store))
            try:
                for line in reader.poll(store):
                    print(line)
            except Exception as e:  # noqa: BLE001
                print(f"reader: {e}")
            if time.time() - last_scout > 3600 and store.count("ready") < 50:
                last_scout = time.time()
                try:
                    print(scout.run(store, 20, progress=print))
                except Exception as e:  # noqa: BLE001
                    print(f"scout: {e}")
            time.sleep(15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
