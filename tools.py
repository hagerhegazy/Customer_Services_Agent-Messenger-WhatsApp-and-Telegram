import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client
import re

load_dotenv()

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

CURRENCY = os.getenv("CURRENCY", "EGP")

ORDER_INSTRUCTION = (
    "Send the customer ALL of order_details in their language, leaving nothing out: order number, "
    "every item with size, quantity and price, the total, name, phone and address. "
    "Copy the numbers exactly. Then ask one short question to check that everything is correct."
)


def money(x) -> str:
    x = float(x)
    return str(int(x)) if x.is_integer() else f"{x:.2f}"


def alert_owner(text: str) -> None:
    """Telegram message to the shop owner. Never raises."""
    try:
        from summary import send_telegram  # imported here to avoid a circular import
        send_telegram(text)
    except Exception as e:
        print("Telegram alert failed:", e)


def get_product_info(name: str) -> list[dict]:
    """Search products by name (partial match). Empty list when nothing matches."""
    return (
        supabase.table("products")
        .select("id, name, description, price, stock, size_chart, colors")
        .ilike("name", f"%{name}%")
        .execute()
        .data
    )
def _norm(s) -> str:
    return str(s or "").strip().lower().translate(
        str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))


def check_color(product: dict, color):
    """Returns (color_to_save, error_for_the_model)."""
    options = [c.strip() for c in re.split(r"[,/،;|]", str(product.get("colors") or "")) if c.strip()]
    if not options:
        return None, None
    if not color or not str(color).strip():
        return None, (f"No color was given for {product['name']}. Available colors: {', '.join(options)}. "
                      f"If the customer already chose a color earlier in this chat (in any language, for example بني means brown), "
                      f"call the tool again with that color written exactly as in the list. Ask the customer only if they never chose one.")
    for c in options:
        if _norm(c) == _norm(color):
            return c, None
    return None, (f"We do not have {product['name']} in '{color}'. Available colors: {', '.join(options)}. "
                  f"Tell the customer and ask which of these they want.")

def list_catalog() -> list[dict]:
    """Everything the shop sells. This is the ONLY source the bot may use to say what we have."""
    try:
        return supabase.table("products").select("id, name, price, stock, category, colors").execute().data
    except Exception:  # no category column in this shop
        return supabase.table("products").select("id, name, price, stock, colors").execute().data

def find_recent_active_order(facebook_id: str, minutes: int = 60) -> int | None:
    customer = supabase.table("customers").select("id").eq("facebook_id", facebook_id).execute().data
    if not customer:
        return None
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = (
        supabase.table("orders").select("id")
        .eq("customer_id", customer[0]["id"]).in_("status", ["pending", "sent_to_delivery"])
        .gte("created_at", since).order("created_at", desc=True).limit(1).execute().data
    )
    return rows[0]["id"] if rows else None

def check_quantities(items) -> str | None:
    if not items or len(items) > 10:
        return "An order needs between 1 and 10 different items."
    for it in items:
        q = it.get("quantity")
        if not isinstance(q, int) or isinstance(q, bool) or not 1 <= q <= 10:
            return "Each quantity must be a whole number from 1 to 10. If the customer wants more, hand off to a human."
    return None

def create_order(facebook_id: str, name: str, phone: str, address: str, items: list[dict], additional_order: bool = False) -> dict:
    """items = [{"product_id": 1, "quantity": 2, "size": "L"}, ...]"""
    phone = phone.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    phone = "".join(ch for ch in phone if ch.isdigit())
    if len(phone) != 11:
        return {"ok": False, "error": "The phone number must be exactly 11 digits. Ask the customer to write it again."}

    # 0. the customer said "yes" twice? The same items from the same customer in the last 10 minutes
    #    are the SAME order: return it instead of creating (and sending to delivery) a second one.
    existing = find_recent_duplicate(facebook_id, items)
    if existing:
        return {"ok": True, "duplicate": True, "order_id": existing, "courier_notified": True,
                "order_details": order_summary(existing), "order_text": format_order_text(existing),
                "instruction": ORDER_INSTRUCTION}
    if not additional_order:
        recent = find_recent_active_order(facebook_id)
        if recent:
            return {"ok": False, "error": (
                f"This customer already has order #{recent}, placed less than an hour ago. If they want to change anything "
                f"(quantity, size, color, address...), call update_order with order_id={recent} and the COMPLETE new items list. "
                f"Only if they clearly asked for an additional, separate order, call create_order again with additional_order=true.")}
    # 1. save or update the customer
    customer = (
        supabase.table("customers")
        .upsert({"facebook_id": facebook_id, "name": name, "phone": phone, "address": address},
                on_conflict="facebook_id")
        .execute()
        .data[0]
    )

    # 2. check stock and calculate total
    total = 0
    lines = []
    for item in items:
        product = (
            supabase.table("products").select("*").eq("id", item["product_id"]).single().execute().data
        )
        if product["stock"] < item["quantity"]:
            return {"ok": False, "error": f"Not enough stock for {product['name']} (left: {product['stock']})"}
        color, bad = check_color(product, item.get("color"))
        if bad:
            return {"ok": False, "error": bad}
        total += product["price"] * item["quantity"]
        lines.append((product, item["quantity"], (item.get("size") or "").strip() or None, color))
    # 3. create the order and its items
    order = (
        supabase.table("orders")
        .insert({"customer_id": customer["id"], "total": total, "status": "pending"})
        .execute()
        .data[0]
    )
    for product, qty, size, color in lines:
        supabase.table("order_items").insert(
            {"order_id": order["id"], "product_id": product["id"], "quantity": qty,
             "unit_price": product["price"], "size": size}
        ).execute()
        supabase.table("products").update({"stock": product["stock"] - qty}).eq("id", product["id"]).execute()

    from whatsapp import send_order_to_delivery  # imported here to avoid a circular import
    sent = send_order_to_delivery(order["id"])
    if not sent:
        alert_owner(f"Order #{order['id']} was NOT sent to the delivery company. "
                    f"Check the server log, then resend it with: python whatsapp.py {order['id']}")

    return {"ok": True, "order_id": order["id"], "total": total, "courier_notified": sent,
            "order_details": order_summary(order["id"]), "order_text": format_order_text(order["id"]),
            "instruction": ORDER_INSTRUCTION}


def _items_key(rows) -> list:
    return sorted((int(r["product_id"]), int(r["quantity"]), (r.get("size") or "").strip().upper(),
                   (r.get("color") or "").strip().lower()) for r in rows)


def find_recent_duplicate(facebook_id: str, items: list[dict], minutes: int = 10) -> int | None:
    """Id of this customer's order with exactly these items, created in the last few minutes (not cancelled)."""
    try:
        customer = supabase.table("customers").select("id").eq("facebook_id", facebook_id).execute().data
        if not customer:
            return None
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = (
            supabase.table("orders")
            .select("id, order_items(product_id, quantity, size, color)")
            .eq("customer_id", customer[0]["id"]).neq("status", "cancelled").gte("created_at", since)
            .order("created_at", desc=True).execute().data
        )
        wanted = _items_key(items)
        for row in rows:
            if _items_key(row["order_items"]) == wanted:
                return row["id"]
    except Exception as e:
        print("duplicate check failed (continuing):", e)
    return None


def cancel_order(order_id: int, facebook_id: str) -> dict:
    """Cancel only if the order belongs to this customer and is not dispatched yet."""
    order = supabase.table("orders").select("*, customers(facebook_id)").eq("id", order_id).single().execute().data
    if order["customers"]["facebook_id"] != facebook_id:
        return {"ok": False, "error": "This order does not belong to you."}
    if order["status"] not in ("pending", "confirmed", "sent_to_delivery"):
        return {"ok": False, "error": f"Order is already {order['status']}, it cannot be cancelled."}

    # give the stock back
    items = supabase.table("order_items").select("product_id, quantity").eq("order_id", order_id).execute().data
    for it in items:
        stock = supabase.table("products").select("stock").eq("id", it["product_id"]).single().execute().data["stock"]
        supabase.table("products").update({"stock": stock + it["quantity"]}).eq("id", it["product_id"]).execute()

    supabase.table("orders").update({"status": "cancelled"}).eq("id", order_id).execute()
    if order["status"] == "sent_to_delivery":
        from whatsapp import send_cancellation_to_delivery
        if not send_cancellation_to_delivery(order_id):
            alert_owner(f"Order #{order_id} was cancelled by the customer but the delivery company was NOT told. "
                        f"Tell them manually.")
    return {"ok": True, "message": f"Order {order_id} cancelled.",
            "order_details": order_summary(order_id), "instruction": ORDER_INSTRUCTION}

def get_my_orders(facebook_id: str) -> list[dict]:
    """Last 5 orders of this customer, with items."""
    customer = supabase.table("customers").select("id").eq("facebook_id", facebook_id).execute().data
    if not customer:
        return []
    return (
        supabase.table("orders")
        .select("id, status, total, created_at, order_items(*, products(name))")
        .eq("customer_id", customer[0]["id"])
        .order("created_at", desc=True)
        .limit(5)
        .execute().data
    )


def update_order(order_id: int, facebook_id: str, name=None, phone=None, address=None, items=None) -> dict:
    order = (
        supabase.table("orders")
        .select("id, status, customers(id, facebook_id)")
        .eq("id", order_id).single().execute().data
    )
    if order["customers"]["facebook_id"] != facebook_id:
        return {"ok": False, "error": "This order does not belong to you."}
    if order["status"] not in ("pending", "sent_to_delivery"):
        return {"ok": False, "error": f"Order is already {order['status']}, it can no longer be changed."}

    if phone:
        phone = phone.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
        phone = "".join(ch for ch in phone if ch.isdigit())
        if len(phone) != 11:
            return {"ok": False, "error": "The phone number must be exactly 11 digits. Ask the customer to write it again."}
    changes = {k: v for k, v in {"name": name, "phone": phone, "address": address}.items() if v}
    if not changes and not items:
        return {"ok": False, "error": "Nothing to change."}

    if items:
        old_items = supabase.table("order_items").select("*").eq("order_id", order_id).execute().data
        held = {}    # units this order already holds, per product
        old_sizes = {}  # sizes this order already has, per product
        old_colors = {}  # colors this order already has, per product
        for it in old_items:
            held[it["product_id"]] = held.get(it["product_id"], 0) + it["quantity"]
            if it.get("size"):
                old_sizes.setdefault(it["product_id"], set()).add(it["size"])
            if it.get("color"):
                old_colors.setdefault(it["product_id"], set()).add(it["color"])

        wanted = {}  # units the new list needs, per product
        for it in items:
            wanted[it["product_id"]] = wanted.get(it["product_id"], 0) + it["quantity"]

        products = {}
        for pid in set(held) | set(wanted):
            products[pid] = supabase.table("products").select("*").eq("id", pid).single().execute().data
        for pid, qty in wanted.items():
            if products[pid]["stock"] + held.get(pid, 0) < qty:
                return {"ok": False, "error": f"{products[pid]['name']} is not available in that quantity"}
        resolved_colors = []
        for it in items:
            p = products[it["product_id"]]
            color = (it.get("color") or "").strip() or None
            if color is None and len(old_colors.get(p["id"], ())) == 1:
                color = next(iter(old_colors[p["id"]]))   # keep the color of an unchanged item
            color, bad = check_color(p, color)
            if bad:
                return {"ok": False, "error": bad}
            resolved_colors.append(color)
        supabase.table("order_items").delete().eq("order_id", order_id).execute()
        total = 0
        for it, color in zip(items, resolved_colors):
            p = products[it["product_id"]]
            size = (it.get("size") or "").strip() or None
            if size is None and len(old_sizes.get(p["id"], ())) == 1:
                size = next(iter(old_sizes[p["id"]]))   # the model forgot the size of an unchanged item: keep it
            row = {"order_id": order_id, "product_id": p["id"], "quantity": it["quantity"], "color": color,
                   "unit_price": p["price"], "size": size}
            supabase.table("order_items").insert(row).execute()
            total += p["price"] * it["quantity"]
        for pid, p in products.items():
            new_stock = p["stock"] + held.get(pid, 0) - wanted.get(pid, 0)
            supabase.table("products").update({"stock": new_stock}).eq("id", pid).execute()
        supabase.table("orders").update({"total": total}).eq("id", order_id).execute()

    if changes:
        supabase.table("customers").update(changes).eq("id", order["customers"]["id"]).execute()

    courier_notified = True
    if order["status"] == "sent_to_delivery":
        from whatsapp import send_update_to_delivery
        courier_notified = send_update_to_delivery(order_id)
        if not courier_notified:
            alert_owner(f"Order #{order_id} was CHANGED but the delivery company was NOT notified. "
                        f"Send them the new details manually:\n\n{format_order_text(order_id)}")
    elif order["status"] == "pending":
        # it never reached the delivery company, so send it now as a new order with the latest details
        from whatsapp import send_order_to_delivery
        courier_notified = send_order_to_delivery(order_id)
        if not courier_notified:
            alert_owner(f"Order #{order_id} was changed and is STILL not sent to the delivery company. "
                        f"Resend it with: python whatsapp.py {order_id}")
    return {"ok": True, "courier_notified": courier_notified, "order_details": order_summary(order_id),
            "order_text": format_order_text(order_id), "instruction": ORDER_INSTRUCTION}

def handoff_to_human(facebook_id: str, reason: str) -> dict:
    supabase.table("handoffs").insert({"facebook_id": facebook_id, "reason": reason}).execute()
    try:
        from summary import send_telegram
        send_telegram(f"A customer needs a human.\nFacebook id: {facebook_id}\nReason: {reason}\nOpen the Page inbox and reply.")
    except Exception as e:
        print("Telegram alert failed:", e)
    return {"ok": True}


def human_has_chat(facebook_id: str) -> bool:
    res = (
        supabase.table("handoffs").select("id")
        .eq("facebook_id", facebook_id).eq("resolved", False).limit(1).execute().data
    )
    return bool(res)

def order_summary(order_id: int) -> dict:
    order = (
        supabase.table("orders")
        .select("id, status, total, customers(name, phone, address), order_items(*, products(name))")
        .eq("id", order_id).single().execute().data
    )
    c = order["customers"]
    return {
        "order_id": order["id"],
        "status": order["status"],
        "name": c["name"], "phone": c["phone"], "address": c["address"],
        "items": [
            {"product": it["products"]["name"], "size": it.get("size"), "color": it.get("color"),
             "quantity": it["quantity"], "unit_price": it["unit_price"],
             "line_total": it["unit_price"] * it["quantity"]}
            for it in order["order_items"]
        ],
        "total": order["total"],
        "currency": CURRENCY,
    }


def format_order_text(order_id: int) -> str:
    """Fixed English block built in code. The bot sends it as a safety net if its own message misses the total."""
    d = order_summary(order_id)
    lines = [f"Order #{d['order_id']}"]
    for it in d["items"]:
        opts = ", ".join(x for x in (it.get("size"), it.get("color")) if x)
        size = f" ({opts})" if opts else ""
        lines.append(f"- {it['quantity']}x {it['product']}{size}: "
                     f"{money(it['unit_price'])} x {it['quantity']} = {money(it['line_total'])} {CURRENCY}")
    lines += [f"Total: {money(d['total'])} {CURRENCY}",
              f"Name: {d['name']}", f"Phone: {d['phone']}", f"Address: {d['address']}"]
    return "\n".join(lines)


# ---- conversation memory (step 8a) ----------------------------------------------------------
def load_history(psid: str, limit: int = 20) -> list[dict]:
    rows = (
        supabase.table("messages").select("role, content")
        .eq("psid", psid).order("id", desc=True).limit(limit).execute().data
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def save_message(psid: str, role: str, content: str) -> None:
    supabase.table("messages").insert({"psid": psid, "role": role, "content": content}).execute()


_seen_in_memory: set[str] = set()


def claim_message(mid: str) -> bool:
    """True the first time we see this Messenger message id, False for a duplicate delivery."""
    try:
        supabase.table("processed_messages").insert({"mid": mid}).execute()
        return True
    except Exception as e:
        if "23505" in str(e) or "duplicate" in str(e).lower():
            return False
        print("processed_messages unavailable, using memory:", e)
        if mid in _seen_in_memory:
            return False
        _seen_in_memory.add(mid)
        return True


if __name__ == "__main__":
    # quick test in the terminal: python tools.py
    print(get_product_info("shirt"))
    result = create_order("test_user_1", "Ali", "01000000000", "Giza, Street 1", [{"product_id": 1, "quantity": 2}])
    print(result)
    if result["ok"]:
        print(cancel_order(result["order_id"], "test_user_1"))
