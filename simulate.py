"""Send fake (but correctly signed) Meta webhooks to your local server, no Facebook needed.

  1. In .env put  DRY_RUN=1  (the server then prints instead of calling Meta/WhatsApp)
  2. uvicorn main:app --port 8000
  3. In another terminal:
       python simulate.py text "how much is the shirt?"
       python simulate.py comment "كام سعر التيشيرت؟"
       python simulate.py voice http://localhost:9000/my_recording.m4a
         (serve the file first:  python -m http.server 9000   in the folder with the recording)
"""
import hashlib
import hmac
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()
APP_SECRET = os.environ["APP_SECRET"].strip()
URL = os.getenv("WEBHOOK_URL", "http://127.0.0.1:8000/webhook")   # 127.0.0.1, not "localhost" (on Windows localhost may mean IPv6)
PAGE_ID, USER_ID = "111111111", "sim_user_1"


def post(payload: dict) -> None:
    print(f"Sending to {URL}  (is uvicorn running on this port? change it with WEBHOOK_URL in .env)")
    raw = json.dumps(payload).encode()
    sig = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    r = httpx.post(URL, content=raw, timeout=30,
                   headers={"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={sig}"})
    print(r.status_code, r.text)


def messaging(message: dict) -> dict:
    message["mid"] = f"sim.{time.time_ns()}"
    return {"object": "page", "entry": [{"id": PAGE_ID, "messaging": [
        {"sender": {"id": USER_ID}, "recipient": {"id": PAGE_ID}, "message": message}]}]}


if __name__ == "__main__":
    kind, arg = (sys.argv[1], sys.argv[2]) if len(sys.argv) >= 3 else ("", "")
    if kind == "text":
        post(messaging({"text": arg}))
    elif kind == "voice":
        post(messaging({"attachments": [{"type": "audio", "payload": {"url": arg}}]}))
    elif kind == "comment":
        cid = f"{PAGE_ID}_{time.time_ns()}"
        post({"object": "page", "entry": [{"id": PAGE_ID, "changes": [{"field": "feed", "value": {
            "item": "comment", "verb": "add", "comment_id": cid, "post_id": f"{PAGE_ID}_post1",
            "parent_id": f"{PAGE_ID}_post1", "message": arg, "from": {"id": USER_ID, "name": "Sim User"}}}]}]})
    else:
        print(__doc__)
