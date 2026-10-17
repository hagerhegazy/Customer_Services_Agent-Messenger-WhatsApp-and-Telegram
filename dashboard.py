from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from summary import collect_stats, write_summary
from tools import supabase

st.set_page_config(page_title="Shop dashboard", layout="wide")
st.title("Shop dashboard")

periods = {"Last 8 hours": 8, "Last 24 hours": 24, "Last 7 days": 168}
hours = periods[st.selectbox("Period", list(periods))]

stats = collect_stats(hours)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Orders", stats["orders_total"])
c2.metric("Revenue (without cancelled)", f"{stats['revenue_excluding_cancelled']} {stats['currency']}")
c3.metric("Cancelled", stats["orders_by_status"].get("cancelled", 0))
c4.metric("Not sent to delivery", len(stats["orders_not_sent_to_delivery"]))

if stats["orders_not_sent_to_delivery"]:
    st.warning(f"Orders not sent to the delivery company yet: {stats['orders_not_sent_to_delivery']}")

left, right = st.columns(2)
with left:
    st.subheader("Top products")
    if stats["top_products"]:
        st.bar_chart(pd.DataFrame(stats["top_products"]).set_index("name"))
    else:
        st.info("No sales in this period.")
with right:
    st.subheader("Low stock (5 or less)")
    if stats["low_stock_products"]:
        st.dataframe(pd.DataFrame(stats["low_stock_products"]))
    else:
        st.success("All products have enough stock.")

st.subheader("Recent orders")
since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
rows = (
    supabase.table("orders")
    .select("id, status, total, created_at, customers(name, phone)")
    .gte("created_at", since)
    .order("created_at", desc=True)
    .limit(50)
    .execute().data
)
table = []
for r in rows:
    customer = r.get("customers") or {}
    table.append({
        "order": r["id"],
        "customer": customer.get("name"),
        "phone": customer.get("phone"),
        "status": r["status"],
        "total": r["total"],
        "time (UTC)": r["created_at"],
    })
if table:
    st.dataframe(pd.DataFrame(table))
else:
    st.info("No orders in this period.")

st.divider()
if st.button("Write AI summary for this period"):
    st.text(write_summary(stats))
