import os
import hmac
import hashlib
from tools import human_has_chat, claim_message
import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

load_dotenv()
import agent      # your agent.py
import comments   # step 9: Facebook comments
import voice      # step 10: voice in / voice out

router = APIRouter()

VERIFY_TOKEN = os.environ["VERIFY_TOKEN"].strip()            # any secret word YOU choose
PAGE_ACCESS_TOKEN = os.environ["PAGE_ACCESS_TOKEN"].strip()  # from the Meta dashboard
APP_SECRET = os.environ["APP_SECRET"].strip()                # from Meta app Settings > Basic
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v23.0")          # check the dashboard for the current version
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")   # print instead of calling Meta
# what a customer who sent a VOICE note gets back: voice_first (voice, then text) | text_first | voice_only
VOICE_REPLY = os.getenv("VOICE_REPLY", "voice_first").strip().lower()

print(f"[startup] APP_SECRET length = {len(APP_SECRET)} (should be 32)")
print(f"[startup] PAGE_ACCESS_TOKEN length = {len(PAGE_ACCESS_TOKEN)}, ECHO_TEST = {bool(os.getenv('ECHO_TEST'))}, DRY_RUN = {DRY_RUN}")


def valid_signature(raw_body: bytes, header: str | None) -> bool:
    """Meta signs every webhook call. This proves the call really came from Meta."""
    if not header or not header.startswith("sha256="):
        print("Webhook rejected: no signature header")
        return False
    expected = hmac.new(APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    ok = hmac.compare_digest(expected, header.removeprefix("sha256="))
    if not ok:
        print(f"Webhook rejected: signature mismatch (APP_SECRET length = {len(APP_SECRET)}, should be 32)")
    return ok


def send_message(psid: str, text: str) -> None:
    if DRY_RUN:
        print(f"[DRY_RUN] text to {psid}: {text!r}")
        return
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/me/messages"
    payload = {
        "recipient": {"id": psid},
        "messaging_type": "RESPONSE",
        "message": {"text": (text or "...")[:1900]},  # Messenger limit is 2000 characters
    }
    r = httpx.post(url, params={"access_token": PAGE_ACCESS_TOKEN}, json=payload, timeout=20)
    if r.status_code != 200:
        print("Send failed:", r.status_code, r.text)
        try:
            err = r.json().get("error", {})
        except Exception:
            err = {}
        if err.get("code") == 10 and err.get("error_subcode") == 1893063:
            print("   -> Meta messaging restriction on this page/conversation. Retrying will not help.")
    else:
        print(f"Sent OK to {psid}")


def handle_message(psid: str, text: str, by_voice: bool = False) -> None:
    print(f"Received from {psid}: {text!r}" + (" (voice)" if by_voice else ""))
    if human_has_chat(psid):
        print(f"Skipping {psid}: a human is handling this chat")
        return
    if os.getenv("ECHO_TEST"):
        send_message(psid, f"echo: {text}")
        return
    try:
        # "[voice]" tells the agent this is a transcript that may contain mistakes (see the SYSTEM_PROMPT rule)
        reply = agent.reply_to(psid, f"[voice] {text}" if by_voice else text)
    except Exception as e:
        print("Agent error:", e)
        reply = "Sorry, something went wrong. A team member will contact you."
    if not by_voice:
        send_message(psid, reply)
    elif VOICE_REPLY == "text_first":
        send_message(psid, reply)
        voice.send_voice(psid, reply)
    else:
        sent_voice = voice.send_voice(psid, reply)           # the voice note comes first
        if not (VOICE_REPLY == "voice_only" and sent_voice):  # the text follows (screen readers) unless voice_only
            send_message(psid, reply)                         # also the fallback when the voice could not be made


def handle_voice(psid: str, audio_url: str) -> None:
    """Customer sent a voice note: turn it into text, then handle it like any other message."""
    if human_has_chat(psid):
        print(f"Skipping {psid}: a human is handling this chat")
        return
    try:
        text = voice.transcribe_url(audio_url)
    except Exception as e:
        print("Voice transcription failed:", e)
        text = ""
    if not text:
        send_message(psid, "Sorry, I could not hear that clearly. Could you record it again or write it? / "
                           "معلش مسمعتش كويس، ممكن تبعتها تاني؟")
        return
    handle_message(psid, text, by_voice=True)


def handle_unsupported(psid: str) -> None:
    send_message(psid, "I can read text and voice messages. Please write or record what you need. / "
                       "ابعتلي رسالة مكتوبة أو صوتية وهساعدك.")


# 1) Meta calls this ONCE when you press "Verify and save" in the dashboard
@router.get("/webhook")
def verify(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge"),
):
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403)


# 2) Meta calls this for every new customer message and every new comment
@router.post("/webhook")
async def receive(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not valid_signature(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=403)

    data = await request.json()
    if data.get("object") == "page":
        for entry in data.get("entry", []):
            print(f"[webhook] page {entry.get('id')}: fields={[c.get('field') for c in entry.get('changes', [])]} "
                  f"messaging_events={len(entry.get('messaging', []))}")
            # step 9: comments on the Page's posts
            for change in entry.get("changes", []):
                if change.get("field") == "feed":
                    comments.handle_feed_change(entry.get("id"), change.get("value") or {}, background)

            # Messenger messages
            for event in entry.get("messaging", []):
                msg = event.get("message")
                if not msg or msg.get("is_echo"):
                    continue  # ignore our own echoes
                # the "like" thumb and stickers arrive as images with a sticker_id: ignore those
                atts = [a for a in (msg.get("attachments") or []) if not (a.get("payload") or {}).get("sticker_id")]
                audio = next((a for a in atts if a.get("type") == "audio" and (a.get("payload") or {}).get("url")), None)
                if "text" not in msg and not atts:
                    continue
                if not claim_message(msg.get("mid")):   # Meta sometimes sends the same message twice
                    continue
                sender = event["sender"]["id"]
                # Answer Meta immediately; the slow AI work runs in the background
                if "text" in msg:
                    background.add_task(handle_message, sender, msg["text"])
                elif audio:
                    background.add_task(handle_voice, sender, audio["payload"]["url"])
                elif any(a.get("type") in ("image", "video", "file") for a in atts):
                    background.add_task(handle_unsupported, sender)

    return PlainTextResponse("EVENT_RECEIVED")
