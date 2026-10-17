"""Checks (and fixes) why Facebook comments never reach your bot.

  python check_page.py             -> shows what your PAGE_ACCESS_TOKEN is allowed to do + what the Page is subscribed to
  python check_page.py subscribe   -> subscribes the Page to  messages + feed  (feed = comments on posts)
  python check_page.py comments    -> shows the Page's latest posts and the comments Facebook itself sees on them
  python check_page.py token       -> your .env holds a USER token? this lists your Pages and their PAGE tokens
  python check_page.py token PAGE_ID  -> same, for one Page (use this if the list is empty)

Needs in .env:  PAGE_ACCESS_TOKEN, APP_SECRET and APP_ID (Meta app > Settings > Basic > App ID)
"""
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["PAGE_ACCESS_TOKEN"].strip()
APP_ID = os.getenv("APP_ID", "").strip()
APP_SECRET = os.environ["APP_SECRET"].strip()
V = os.getenv("GRAPH_VERSION", "v23.0")
G = f"https://graph.facebook.com/{V}"
NEEDED = ["pages_messaging", "pages_manage_metadata", "pages_read_engagement", "pages_manage_engagement"]


def call(method: str, path: str, token: str = TOKEN, **params):
    r = httpx.request(method, f"{G}/{path}", params={"access_token": token, **params}, timeout=20)
    text = re.sub(r'(access_token(?:=|":"))[^&"]+', r'\1HIDDEN', r.text)
    print(f"{method} /{path} -> {r.status_code}  {text}")
    return r


if len(sys.argv) > 1 and sys.argv[1] == "token":
    if len(sys.argv) > 2:
        pages = [call("GET", sys.argv[2], fields="id,name,access_token").json()]
    else:
        pages = call("GET", "me/accounts", fields="id,name,access_token").json().get("data", [])
    print()
    for pg in pages:
        if pg.get("access_token"):
            print(f"PAGE: {pg.get('name')}  (id {pg['id']})\nPAGE_ACCESS_TOKEN={pg['access_token']}\n")
    if not any(pg.get("access_token") for pg in pages):
        print("No Page token came back. Try:  python check_page.py token YOUR_PAGE_ID")
    else:
        print("Copy the PAGE_ACCESS_TOKEN line of YOUR page into .env (keep it secret), restart uvicorn, then run: python check_page.py subscribe")
    sys.exit()

page_id = None
if APP_ID:
    print("1) What is this token allowed to do?")
    info = call("GET", "debug_token", token=f"{APP_ID}|{APP_SECRET}", input_token=TOKEN).json().get("data", {})
    scopes = info.get("scopes", [])
    page_id = info.get("profile_id")
    print(f"\n   token type : {info.get('type')}   valid: {info.get('is_valid')}   expires: {info.get('expires_at') or 'never'}")
    if info.get("type") == "USER":
        sys.exit("\n   This is a USER token. The bot needs the PAGE token.\n   Run:  python check_page.py token   and put the PAGE_ACCESS_TOKEN it prints into .env")
    print(f"   page id    : {page_id}")
    print(f"   permissions: {', '.join(scopes) or '(none)'}")
    missing = [s for s in NEEDED if s not in scopes]
    print(("\n   MISSING permissions: " + ", ".join(missing) + "\n   -> make a new Page token that includes them (see the steps I sent)")
          if missing else "\n   All needed permissions are on this token.")
else:
    print("Add  APP_ID=<your app id>  to .env to see the token's permissions.")

if not page_id:
    page_id = call("GET", "me", fields="id").json().get("id")
if not page_id:
    sys.exit("Could not find the Page id. Fix PAGE_ACCESS_TOKEN / APP_ID first.")

if len(sys.argv) > 1 and sys.argv[1] == "comments":
    print(f"\nLatest posts of Page {page_id} and their comments:")
    data = call("GET", f"{page_id}/feed", fields="id,message,created_time,comments.limit(5){from,message,created_time}", limit=3)
    sys.exit("\nIs the post you commented on in this list? If not, you are commenting on a different Page than the one the bot is connected to.")

if len(sys.argv) > 1 and sys.argv[1] == "subscribe":
    print("\n2) Subscribing the Page to messages + feed")
    call("POST", f"{page_id}/subscribed_apps", subscribed_fields="messages,feed")
print("\n3) What is the Page subscribed to now?")
call("GET", f"{page_id}/subscribed_apps")
print('\nLook for "feed" inside subscribed_fields. If it is missing, run: python check_page.py subscribe')
