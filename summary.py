import os
import json
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from groq import Groq

load_dotenv()
from tools import supabase  # reuse the Supabase connection

client = Groq(api_key=os.environ["GROQ_API_KEY"])
MODEL = "openai/gpt-oss-120b"
CURRENCY = os.getenv("CURRENCY", "EGP")


def collect_stats(hours: int = 8) -> dict:
    """All numbers are calculated here in Python, never by the AI."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    orders = (
        supabase.table("orders")
        .select("id, status, total, created_at")
        .gte("created_at", since)
        .execute().data
    )
    ids = [o["id"] for o in orders]
    items = []
    if ids:
        items = (
            supabase.table("order_items")
            .select("order_id, quantity, products(name)")
            .in_("order_id", ids)
            .execute().data
        )

    by_status: dict[str, int] = {}
    for o in orders:
        by_status[o["status"]] = by_status.get(o["status"], 0) + 1

    cancelled_ids = {o["id"] for o in orders if o["status"] == "cancelled"}
    revenue = sum(o["total"] for o in orders if o["status"] != "cancelled")

    product_units: dict[str, int] = {}
    for it in items:
        if it["order_id"] in cancelled_ids:
            continue
        name = it["products"]["name"]
        product_units[name] = product_units.get(name, 0) + it["quantity"]
    top = sorted(product_units.items(), key=lambda x: x[1], reverse=True)[:5]

    low_stock = supabase.table("products").select("name, stock").lte("stock", 5).execute().data

    return {
        "period_hours": hours,
        "orders_total": len(orders),
        "orders_by_status": by_status,
        "revenue_excluding_cancelled": revenue,
        "currency": CURRENCY,
        "top_products": [{"name": n, "units": u} for n, u in top],
        "orders_not_sent_to_delivery": [o["id"] for o in orders if o["status"] == "pending"],
        "low_stock_products": low_stock,
    }


def write_summary(stats: dict) -> str:
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You write a short shift report for a small shop owner. "
                    "Use ONLY the numbers in the JSON. Never calculate or invent numbers. "
                    "Plain text only, no markdown, maximum 10 lines. "
                    "End with a line starting 'Needs attention:' listing orders not sent to delivery, "
                    "low stock products, or anything unusual such as many cancellations. "
                    "If nothing needs attention, write 'Needs attention: nothing'."
                ),
            },
            {"role": "user", "content": json.dumps(stats, default=str)},
        ],
    )
    return resp.choices[0].message.content
import httpx

def send_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    chat_id = os.environ["TELEGRAM_CHAT_ID"].strip()
    r = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=30,
    )
    if r.status_code != 200:
        print("Telegram failed:", r.status_code, r.text)

if __name__ == "__main__":
    stats = collect_stats(8)
    print(json.dumps(stats, indent=2, default=str))
    print("\n--- SUMMARY ---\n")
    text = write_summary(stats)
    print(text)
    send_telegram(text)