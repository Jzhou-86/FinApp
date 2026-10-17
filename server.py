"""Backend API for the dashboard in frontend/.

The React app calls these endpoints; this server calls Nessie (with the API
key from .env) and returns JSON shaped for the screens.

Run it with:  venv\\Scripts\\python.exe server.py
(or click Run on this file in VS Code). It listens on http://localhost:8000.
Needs:  pip install fastapi uvicorn

Everything except sign up / log in needs "Authorization: Bearer <token>".

Accounts (auth.py):
  POST /api/auth/signup {email, password, display_name} -> {token}
  POST /api/auth/login  {email, password}               -> {token}
  POST /api/auth/logout
  GET  /api/me                                          -> you + your bank link
Bank linking (bank.py):
  GET    /api/bank                                       -> linked profile + accounts
  POST   /api/bank/link   {nessie_customer_id, last_name, zip}
  POST   /api/bank/create {first_name, last_name, street_number, street_name, city, state, zip}
  DELETE /api/bank/link
Dashboard (this file):
  GET /api/me/dashboard?period=this_month   (this_month, last_month, last_3_months)
Friends (friends.py):
  GET    /api/friends?q=name                 -> friends, requests, people to add
  POST   /api/friend-requests {to_user_id}   -> send a request
  POST   /api/friend-requests/{id}/accept
  POST   /api/friend-requests/{id}/decline   (decline, or cancel one you sent)
  DELETE /api/friends/{user_id}              -> remove a friend
Groups and shared goals (groups.py):
  GET    /api/groups                          -> your groups + invites waiting for you
  POST   /api/groups {name, invite_user_ids}  -> create a group (invite friends only)
  GET    /api/groups/{id}                     -> members, invites, goals with progress, activity
  POST   /api/groups/{id}/invites {user_id}   -> invite a friend
  POST   /api/group-invites/{id}/accept | /decline
  POST   /api/groups/{id}/leave
  POST   /api/groups/{id}/goals {title, target_amount, deadline}
  DELETE /api/goals/{id}
  POST   /api/goals/{id}/commit {share_amount}
  POST   /api/goals/{id}/contributions {amount, note}
  DELETE /api/contributions/{id}
"""
from collections import defaultdict
from datetime import date, timedelta

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import auth
import bank
import friends
import groups
import nessie

app = FastAPI(title="finapp backend")

# The Vite dev server (frontend) runs on port 8443.
# Allow the deployed frontend and local development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://finapp-poo.up.railway.app",
        "http://localhost:8443",
        "http://127.0.0.1:8443",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(auth.router)
app.include_router(bank.router)
app.include_router(friends.router)
app.include_router(groups.router)

_merchant_cache = {}


def _merchant(merchant_id):
    """Look up a merchant's name and category once, then remember it."""
    if merchant_id not in _merchant_cache:
        try:
            m = nessie._request("GET", f"/merchants/{merchant_id}")
            category = m.get("category") or "Other"
            if isinstance(category, list):
                category = category[0] if category else "Other"
            _merchant_cache[merchant_id] = {"name": m.get("name", "Unknown merchant"), "category": category}
        except nessie.NessieError:
            _merchant_cache[merchant_id] = {"name": "Unknown merchant", "category": "Other"}
    return _merchant_cache[merchant_id]


def _period_range(period, today=None):
    """Return (start, end, previous_start, previous_end) as dates, end inclusive."""
    today = today or date.today()
    first_this_month = today.replace(day=1)
    if period == "this_month":
        start, end = first_this_month, today
    elif period == "last_month":
        end = first_this_month - timedelta(days=1)
        start = end.replace(day=1)
    elif period == "last_3_months":
        start, end = today - timedelta(days=89), today
    else:
        raise HTTPException(400, "period must be this_month, last_month or last_3_months")
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)
    return start, end, prev_start, prev_end


def _customer_purchases(customer_id):
    purchases = []
    for account in nessie.get_customer_accounts(customer_id):
        for p in nessie._request("GET", f"/accounts/{account['_id']}/purchases") or []:
            merchant = _merchant(p.get("merchant_id"))
            purchases.append({
                "id": p.get("_id"),
                "date": date.fromisoformat(p["purchase_date"][:10]),
                "amount": float(p.get("amount", 0)),
                "merchant": merchant["name"],
                "category": merchant["category"],
                "description": p.get("description", ""),
            })
    return purchases


def _daily_buckets(purchases, start, end):
    """Spending per day; per week when the period is longer than a month."""
    step = 1 if (end - start).days < 31 else 7
    buckets = []
    day = start
    while day <= end:
        bucket_end = min(day + timedelta(days=step - 1), end)
        total = sum(p["amount"] for p in purchases if day <= p["date"] <= bucket_end)
        buckets.append({"date": day.isoformat(), "amount": round(total, 2)})
        day = bucket_end + timedelta(days=1)
    return {"unit": "day" if step == 1 else "week", "points": buckets}


@app.get("/api/me/dashboard")
def dashboard(period: str = "this_month", user: dict = Depends(bank.require_bank_link)):
    """Spending dashboard for the logged-in user's linked bank profile."""
    customer_id = user["bank_link"]["nessie_customer_id"]
    try:
        customer = nessie.get_customer(customer_id)
        start, end, prev_start, prev_end = _period_range(period)
        all_purchases = _customer_purchases(customer_id)
    except nessie.NessieError as e:
        raise HTTPException(502, str(e))

    current = [p for p in all_purchases if start <= p["date"] <= end]
    previous = [p for p in all_purchases if prev_start <= p["date"] <= prev_end]
    total = round(sum(p["amount"] for p in current), 2)
    previous_total = round(sum(p["amount"] for p in previous), 2)

    by_category = defaultdict(lambda: {"amount": 0.0, "count": 0})
    for p in current:
        by_category[p["category"]]["amount"] += p["amount"]
        by_category[p["category"]]["count"] += 1
    categories = sorted(
        (
            {
                "name": name,
                "amount": round(v["amount"], 2),
                "count": v["count"],
                "percent": round(100 * v["amount"] / total) if total else 0,
            }
            for name, v in by_category.items()
        ),
        key=lambda c: c["amount"],
        reverse=True,
    )

    recent = sorted(current, key=lambda p: p["date"], reverse=True)[:8]

    return {
        "customer": {"id": customer_id, "first_name": customer["first_name"], "last_name": customer["last_name"]},
        "period": {"key": period, "start": start.isoformat(), "end": end.isoformat()},
        "total": total,
        "previous_total": previous_total,
        "change_percent": round(100 * (total - previous_total) / previous_total, 1) if previous_total else None,
        "purchase_count": len(current),
        "top_category": categories[0] if categories else None,
        "categories": categories,
        "daily": _daily_buckets(current, start, end),
        "transactions": [
            {**p, "date": p["date"].isoformat()} for p in recent
        ],
    }


if __name__ == "__main__":
    import os
    import uvicorn

    if auth.DEV_MODE:
        print("Demo login shortcut is ON (set FINAPP_DEV=0 to turn it off).")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
    )
