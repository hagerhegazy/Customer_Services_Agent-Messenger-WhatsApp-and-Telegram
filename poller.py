"""Fallback for comments: asks Facebook for new comments every few seconds instead of waiting for a webhook.

Turn it on with  POLL_COMMENTS=1  in .env  (optional: POLL_SECONDS=45, POLL_MAX_AGE_MIN=15).
It reuses comments.process_comment, so a polled comment is handled exactly like a webhook comment,
and tools.claim_message makes sure the same comment is never answered twice (webhook + poller).
"""
import os
import time
import threading
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()
import comments
import tools

TOKEN = os.environ["PAGE_ACCESS_TOKEN"].strip()
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v23.0")
INTERVAL = int(os.getenv("POLL_SECONDS", "45"))
MAX_AGE = timedelta(minutes=int(os.getenv("POLL_MAX_AGE_MIN", "15")))   # older comments are never answered
G = f"https://graph.facebook.com/{GRAPH_VERSION}"


def _error_text(r: httpx.Response) -> str:
    try:
        return r.json().get("error", {}).get("message", "")  # never print the whole body: it can contain tokens
    except Exception:
        return ""


def poll_once(page_id: str) -> None:
    r = httpx.get(
        f"{G}/{page_id}/feed",
        params={
            "access_token": TOKEN,
            "limit": 5,
            "fields": "id,comments.filter(toplevel).order(reverse_chronological).limit(25){id,message,from,created_time}",
        },
        timeout=30,
    )
    if r.status_code != 200:
        print("[poll] failed:", r.status_code, _error_text(r))
        return

    now = datetime.now(timezone.utc)
    for post in r.json().get("data", []):
        for c in (post.get("comments") or {}).get("data", []):
            if str((c.get("from") or {}).get("id")) == str(page_id):
                continue                                  # written by the Page itself
            text = (c.get("message") or "").strip()
            if not text:
                continue                                  # photo-only comment
            created = datetime.strptime(c["created_time"], "%Y-%m-%dT%H:%M:%S%z")
            if now - created > MAX_AGE:
                continue                                  # old comment: do not answer
            if not tools.claim_message(f"comment:{c['id']}"):
                continue                                  # already handled (by the webhook or an earlier poll)
            print(f"[poll] new comment {c['id']}: {text!r}")
            comments.process_comment(c["id"], text)


def _loop() -> None:
    page_id = None
    while not page_id:
        try:
            page_id = httpx.get(f"{G}/me", params={"access_token": TOKEN, "fields": "id"}, timeout=30).json().get("id")
        except Exception as e:
            print("[poll] could not read the Page id:", e)
        if not page_id:
            time.sleep(15)
    print(f"[poll] watching Page {page_id} every {INTERVAL}s")
    while True:
        try:
            poll_once(page_id)
        except Exception as e:
            print("[poll] error:", e)
        time.sleep(INTERVAL)


def start() -> None:
    if os.getenv("POLL_COMMENTS", "").lower() not in ("1", "true", "yes"):
        return
    threading.Thread(target=_loop, daemon=True).start()
