"""Step 10: voice messages in and out, for customers who cannot read or write.

  customer voice note --> download --> speech-to-text (Groq Whisper) --> the normal agent
  agent text reply    --> text-to-speech (edge-tts, free) --> Messenger audio message
We always send the TEXT reply too, so screen-reader users and people who can read lose nothing.

Terminal tests (no Facebook):
  python voice.py stt my_recording.m4a
  python voice.py say "ازيك، عاوز اطلب تيشيرت" reply.mp3
"""
import asyncio
import json
import os
import re
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()
import agent   # reuses its Groq client

PAGE_ACCESS_TOKEN = os.environ["PAGE_ACCESS_TOKEN"].strip()
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v23.0")
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

STT_MODEL = os.getenv("STT_MODEL", "whisper-large-v3")       # most accurate for Arabic dialects
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "").strip()         # "" = auto detect, or "ar" / "en"
VOICE_AR = os.getenv("TTS_VOICE_AR", "ar-EG-SalmaNeural")    # Egyptian female; ar-EG-ShakirNeural = male
VOICE_EN = os.getenv("TTS_VOICE_EN", "en-US-AriaNeural")
MAX_AUDIO_BYTES = 20 * 1024 * 1024
ARABIC = re.compile(r"[؀-ۿ]")
EXTENSIONS = {"mp4", "m4a", "mp3", "aac", "wav", "ogg", "webm", "flac", "mpeg"}
# a sample sentence makes Whisper write phone numbers as digits and spell addresses normally
STT_PROMPT = os.getenv("STT_PROMPT", "01012345678")   # digits only: no Arabic or English words, so it cannot pull the language

_DIGIT_WORDS = {
    "صفر": "0", "زيرو": "0", "zero": "0", "واحد": "1", "one": "1", "اتنين": "2", "إتنين": "2", "اثنين": "2", "two": "2",
    "تلاتة": "3", "تلاته": "3", "ثلاثة": "3", "three": "3", "اربعة": "4", "أربعة": "4", "اربعه": "4", "four": "4",
    "خمسة": "5", "خمسه": "5", "five": "5", "ستة": "6", "سته": "6", "six": "6", "سبعة": "7", "سبعه": "7", "seven": "7",
    "تمانية": "8", "تمانيه": "8", "ثمانية": "8", "تمنية": "8", "eight": "8", "تسعة": "9", "تسعه": "9", "nine": "9",
    "عشرة": "10", "عشره": "10", "ten": "10",
}


def fix_numbers(text: str) -> str:
    """'صفر واحد صفر اتنين ...' or '010 1234 5678' (how speech-to-text often writes a phone number) -> 01021234..."""
    words, out, i = text.split(), [], 0
    while i < len(words):
        run, j = [], i
        while j < len(words):
            w = re.sub(r"[^\w]", "", words[j]).lower()
            if w not in _DIGIT_WORDS and w.startswith("و") and w[1:] in _DIGIT_WORDS:
                w = w[1:]                                   # "وواحد" = "and one"
            if w in _DIGIT_WORDS:
                run.append((_DIGIT_WORDS[w], True))
            elif w.isdigit():
                run.append((w, False))
            else:
                break
            j += 1
        spoken = any(is_word for _, is_word in run)
        total = sum(len(d) for d, _ in run)
        if run and ((spoken and len(run) >= 3) or (not spoken and len(run) >= 2 and total >= 10)):
            out.append("".join(d for d, _ in run))
            i = j
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


# ---------- listen ----------
def download(url: str) -> tuple[bytes, str]:
    r = httpx.get(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    if len(r.content) > MAX_AUDIO_BYTES:
        raise ValueError("audio file too big")
    ext = os.path.splitext(url.split("?")[0])[1].lstrip(".").lower()
    return r.content, f"voice.{ext if ext in EXTENSIONS else 'mp4'}"   # Messenger voice notes are mp4/aac


def transcribe(data: bytes, filename: str = "voice.mp4") -> str:
    kwargs = {"file": (filename, data), "model": STT_MODEL, "temperature": 0}
    if STT_LANGUAGE:
        kwargs["language"] = STT_LANGUAGE
    if STT_PROMPT:
        kwargs["prompt"] = STT_PROMPT
    result = agent.client.audio.transcriptions.create(**kwargs)
    text = (getattr(result, "text", result) or "").strip()
    if "01012345678" in re.sub(r"\D", "", text):     # Whisper echoed our sample sentence (silence / noise): ignore it
        return ""
    return fix_numbers(text)


def transcribe_url(url: str) -> str:
    data, name = download(url)
    return transcribe(data, name)


# ---------- speak ----------
def _speakable(text: str) -> str:
    """Make the text nicer to read aloud."""
    ar = bool(ARABIC.search(text))
    text = re.sub(r"\bEGP\b", "جنيه" if ar else "pounds", text)
    text = re.sub(r"(?<=\d)x\b", "", text)                          # "3x Cap" -> "3 Cap"
    text = text.replace(" x ", " في " if ar else " times ").replace("=", " يساوي " if ar else " equals ")
    text = re.sub(r"[*_#`]", "", text)
    text = re.sub(r"^\s*-\s*", "", text, flags=re.M)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()[:1500]


async def _tts(text: str, voice: str) -> bytes:
    import edge_tts   # pip install edge-tts
    chunks = []
    async for chunk in edge_tts.Communicate(text, voice).stream():
        if chunk["type"] == "audio":
            chunks.append(chunk["data"])
    return b"".join(chunks)


def speak(text: str) -> bytes:
    """Text to mp3 bytes. Call it from a normal (non-async) function, like the webhook background tasks."""
    text = _speakable(text)
    voice = VOICE_AR if ARABIC.search(text) else VOICE_EN
    return asyncio.run(_tts(text, voice))


def send_voice(psid: str, text: str) -> bool:
    try:
        audio = speak(text)
    except Exception as e:
        print("Text-to-speech failed:", e)
        return False
    if not audio:
        return False
    if DRY_RUN:
        print(f"[DRY_RUN] voice reply to {psid}: {len(audio)} bytes of mp3")
        return True
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/me/messages"
    data = {
        "recipient": json.dumps({"id": psid}),
        "messaging_type": "RESPONSE",
        "message": json.dumps({"attachment": {"type": "audio", "payload": {"is_reusable": False}}}),
    }
    files = {"filedata": ("reply.mp3", audio, "audio/mpeg")}
    r = httpx.post(url, params={"access_token": PAGE_ACCESS_TOKEN}, data=data, files=files, timeout=60)
    if r.status_code != 200:
        print("Voice send failed:", r.status_code, r.text)   # if Meta rejects mp3, convert to m4a with ffmpeg
        return False
    print(f"Voice sent OK to {psid}")
    return True


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "stt":
        with open(sys.argv[2], "rb") as f:
            print(transcribe(f.read(), os.path.basename(sys.argv[2])))
    elif len(sys.argv) >= 3 and sys.argv[1] == "say":
        out = sys.argv[3] if len(sys.argv) > 3 else "reply.mp3"
        with open(out, "wb") as f:
            f.write(speak(sys.argv[2]))
        print("saved", out)
    else:
        print(__doc__)
