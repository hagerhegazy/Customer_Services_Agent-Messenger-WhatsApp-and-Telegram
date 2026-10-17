import os
import sys
import time
from datetime import datetime, timedelta, timezone
import httpx
from dotenv import load_dotenv

load_dotenv()
from tools import supabase  # reuse the Supabase connection from tools.py

TOKEN = os.environ["WHATSAPP_TOKEN"].strip()
PHONE_ID = os.environ["WHATSAPP_PHONE_ID"].strip()            # the "Phone number ID", not the phone number
DELIVERY_NUMBER = os.environ["DELIVERY_WHATSAPP"].strip()     # digits only: country code + number, no + and no leading 0
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v23.0")
TEMPLATE_LANG = os.getenv("WHATSAPP_TEMPLATE_LANG", "en")
NEW_ORDER_TEMPLATE = os.getenv("WHATSAPP_TEMPLATE_NEW", "new_order")
CANCEL_TEMPLATE = os.getenv("WHATSAPP_TEMPLATE_CANCEL", "order_cancelled")
CURRENCY = os.getenv("CURRENCY", "EGP")
WABA_ID = os.getenv("WHATSAPP_WABA_ID", "").strip()
UPDATE_TEMPLATE = os.getenv("WHATSAPP_TEMPLATE_UPDATE", "order_updated")

def clean(value) -> str:
    """Template variables cannot contain new lines, tabs or long runs of spaces."""
    return " ".join(str(value if value not in (None, "") else "-").split())


def _send_template(name: str, params: list, lang: str | None = None) -> bool:
    if os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes"):
        print(f"[DRY_RUN] WhatsApp '{name}' ->", [clean(p) for p in params]); return True
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_ID}/messages"
    template = {"name": name, "language": {"code": lang or TEMPLATE_LANG}}
    if params:
        template["components"] = [
            {"type": "body", "parameters": [{"type": "text", "text": clean(p)} for p in params]}
        ]
    payload = {
        "messaging_product": "whatsapp",
        "to": DELIVERY_NUMBER,
        "type": "template",
        "template": template,
    }
    r = None
    for attempt in range(3):
        try:
            r = httpx.post(url, headers={"Authorization": f"Bearer {TOKEN}"}, json=payload, timeout=30)
            break
        except (httpx.ConnectTimeout, httpx.ConnectError) as e:  # never connected, so retrying is safe
            print(f"WhatsApp connection problem (try {attempt + 1}/3):", e)
            time.sleep(3)
        except httpx.HTTPError as e:
            print("WhatsApp network error:", e)
            return False
    if r is None:
        return False
    if r.status_code != 200:
        print("WhatsApp send failed:", r.status_code, r.text)
        return False
    print(f"WhatsApp template '{name}' sent OK")
    return True


def send_order_to_delivery(order_id: int, template: str | None = None, mark_sent: bool = True, tag: str = "") -> bool:
    """Builds the order message from Supabase and sends it to the delivery company."""
    try:
        order = (
            supabase.table("orders")
            .select("*, customers(name, phone, address)")
            .eq("id", order_id).single().execute().data
        )
        items = (
            supabase.table("order_items")
            .select("*, products(name)")
            .eq("order_id", order_id).execute().data
        )
    except Exception as e:
        print("Could not load order for delivery:", e)
        return False

    customer = order["customers"]
    lines = []
    for it in items:
        opts = ", ".join(x for x in (it.get("size"), it.get("color")) if x)
        size = f" ({opts})" if opts else ""
        lines.append(f"{it['quantity']}x {it['products']['name']}{size}")

    ok = _send_template(
        template or NEW_ORDER_TEMPLATE,
        [f"{order_id} {tag}".strip(), customer["name"], customer["phone"], customer["address"],
         ", ".join(lines), f"{order['total']} {CURRENCY}"],
    )
    if ok and mark_sent:
        supabase.table("orders").update({"status": "sent_to_delivery"}).eq("id", order_id).execute()
    return ok


def send_cancellation_to_delivery(order_id: int) -> bool:
    if _send_template(CANCEL_TEMPLATE, [order_id]):
        return True
    # the cancel template is missing or not approved: use the approved new-order template with a clear tag
    print("Cancel template failed, using the new-order template with a CANCELLED tag")
    return send_order_to_delivery(order_id, template=NEW_ORDER_TEMPLATE, mark_sent=False,
                                  tag="- CANCELLED, DO NOT DELIVER -")

def list_templates() -> None:
    r = httpx.get(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{WABA_ID}/message_templates",
        params={"fields": "name,language,status", "limit": 50},
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=30,
    )
    if r.status_code != 200:
        print("Failed:", r.status_code, r.text)
        return
    for t in r.json().get("data", []):
        print(t["name"], "|", t["language"], "|", t["status"])

def send_update_to_delivery(order_id: int) -> bool:
    if send_order_to_delivery(order_id, template=UPDATE_TEMPLATE, mark_sent=False):
        return True
    # the update template is missing or not approved: use the approved new-order template with a clear tag
    print("Update template failed, using the new-order template with an UPDATED tag")
    return send_order_to_delivery(order_id, template=NEW_ORDER_TEMPLATE, mark_sent=False,
                                  tag="- UPDATED, USE THESE NEW DETAILS -")

def resend_pending(min_age_minutes: int = 2) -> None:
    """Orders the delivery company never received (status still 'pending'). Run it on a schedule."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = supabase.table("orders").select("id").eq("status", "pending").lte("created_at", cutoff).execute().data
    print(f"{len(rows)} pending order(s) to resend")
    for r in rows:
        print(f"order {r['id']}:", "sent" if send_order_to_delivery(r["id"]) else "failed")


if __name__ == "__main__":
    # python whatsapp.py resend  -> resends orders stuck in 'pending'
    # python whatsapp.py hello   -> sends Meta's built-in test template (checks your setup)
    # python whatsapp.py 5       -> sends order number 5 to the delivery number
    arg = sys.argv[1] if len(sys.argv) > 1 else "hello"
    if arg == "hello":
        _send_template("hello_world", [], lang="en_US")
    elif arg == "templates":
        list_templates()
    elif arg == "resend":
        resend_pending()
    else:
        print("sent" if send_order_to_delivery(int(arg)) else "failed")
