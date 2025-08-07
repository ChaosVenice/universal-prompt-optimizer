import os
import time
import json
import io
import zipfile
from urllib import request as urlreq
from urllib.error import URLError, HTTPError
import re
from textwrap import dedent
import random
import sqlite3
import datetime
import secrets
import smtplib
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from email.utils import formatdate
from functools import wraps
from flask import Flask, request, jsonify, Response, g, abort

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET")

# --- EMAIL UTILS ---
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "")

def send_email(to_email: str, subject: str, text: str):
    """Try SendGrid first, then SMTP, else no-op (logs to server)."""
    if not to_email or not FROM_EMAIL:
        print("[email] Missing to_email or FROM_EMAIL; skipping email.")
        return False

    # 1) SendGrid API
    if SENDGRID_API_KEY:
        try:
            import urllib.request, urllib.error
            url = "https://api.sendgrid.com/v3/mail/send"
            body = {
                "personalizations": [{"to": [{"email": to_email}], "subject": subject}],
                "from": {"email": FROM_EMAIL},
                "content": [{"type": "text/plain", "value": text}],
            }
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
            req.add_header("Authorization", f"Bearer {SENDGRID_API_KEY}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=15) as resp:
                _ = resp.read()
            print(f"[email] Sent via SendGrid to {to_email}")
            return True
        except Exception as e:
            print(f"[email] SendGrid failed: {e}")

    # 2) SMTP fallback
    if SMTP_HOST and SMTP_USER and SMTP_PASS:
        try:
            msg = MIMEText(text, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = FROM_EMAIL
            msg["To"] = to_email
            msg["Date"] = formatdate(localtime=True)

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(FROM_EMAIL, [to_email], msg.as_string())
            print(f"[email] Sent via SMTP to {to_email}")
            return True
        except Exception as e:
            print(f"[email] SMTP failed: {e}")

    # 3) Nothing configured – log only
    print(f"[email] No email provider configured. Would send to {to_email}: {subject}\n{text}")
    return False

# --- API key storage + helpers ---
DB_PATH = os.getenv("USAGE_DB", "usage.db")

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.execute("""CREATE TABLE IF NOT EXISTS usage (
            key TEXT NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY (key, day)
        )""")
        g.db.execute("""CREATE TABLE IF NOT EXISTS api_keys (
            key TEXT PRIMARY KEY,
            email TEXT,
            plan TEXT,
            daily_limit INTEGER NOT NULL,
            expires_at TEXT,          -- ISO date (YYYY-MM-DD) or NULL
            status TEXT NOT NULL,     -- 'active' | 'revoked'
            notes TEXT,
            created_at TEXT NOT NULL  -- ISO datetime
        )""")
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db: db.close()

def bootstrap_demo_key():
    """Create default demo123 key if it doesn't exist"""
    with app.app_context():
        db = get_db()
        cur = db.execute("SELECT key FROM api_keys WHERE key='demo123'")
        if not cur.fetchone():
            upsert_key("demo123", "demo@example.com", "demo", 50, None, "active", "Bootstrap demo key")

def _init_share_table():
    """Initialize the shares table for shareable links"""
    with app.app_context():
        db = get_db()
        db.execute("""CREATE TABLE IF NOT EXISTS shares(
            token TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            title TEXT,
            meta_json TEXT NOT NULL,      -- JSON blob of images + params
            expires_at TEXT               -- ISO date or NULL
        )""")
        # Lead capture table
        db.execute("""CREATE TABLE IF NOT EXISTS leads(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            share_token TEXT,
            ip_address TEXT,
            created_at TEXT NOT NULL,
            source TEXT DEFAULT 'share_download'
        )""")
        # Analytics tracking table
        db.execute("""CREATE TABLE IF NOT EXISTS share_visits(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            share_token TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            referrer TEXT,
            visited_at TEXT NOT NULL
        )""")
        db.commit()

def _new_token(prefix="sh_"): 
    return prefix + secrets.token_urlsafe(16)

def _get_client_ip():
    """Get client IP address handling proxies"""
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        return forwarded.split(',')[0].strip()
    real_ip = request.headers.get('X-Real-IP')
    if real_ip:
        return real_ip
    return request.remote_addr or 'unknown'

def _track_share_visit(share_token):
    """Track analytics for share page visits"""
    db = get_db()
    db.execute("""INSERT INTO share_visits(share_token, ip_address, user_agent, referrer, visited_at)
                  VALUES(?,?,?,?,?)""",
               (share_token, _get_client_ip(), request.headers.get('User-Agent', ''),
                request.headers.get('Referer', ''), datetime.datetime.utcnow().isoformat()))
    db.commit()

def _capture_lead(email, share_token=None, source='share_download'):
    """Capture lead information"""
    db = get_db()
    db.execute("""INSERT INTO leads(email, share_token, ip_address, created_at, source)
                  VALUES(?,?,?,?,?)""",
               (email, share_token, _get_client_ip(), datetime.datetime.utcnow().isoformat(), source))
    db.commit()

def _today():
    return datetime.date.today().isoformat()

def _get_usage(api_key):
    db = get_db()
    day = _today()
    cur = db.execute("SELECT count FROM usage WHERE key=? AND day=?", (api_key, day))
    row = cur.fetchone()
    return row[0] if row else 0

def _inc_usage(api_key, amt=1):
    db = get_db()
    day = _today()
    cur = db.execute("SELECT count FROM usage WHERE key=? AND day=?", (api_key, day))
    row = cur.fetchone()
    if row:
        newc = row[0] + amt
        db.execute("UPDATE usage SET count=? WHERE key=? AND day=?", (newc, api_key, day))
    else:
        newc = amt
        db.execute("INSERT INTO usage(key,day,count) VALUES(?,?,?)", (api_key, day, newc))
    db.commit()
    return newc

def gen_key(prefix="key_"):
    return prefix + secrets.token_urlsafe(24)

def upsert_key(key, email, plan, daily_limit, expires_at=None, status="active", notes=""):
    db = get_db()
    created_at = datetime.datetime.utcnow().isoformat()
    db.execute("""INSERT OR REPLACE INTO api_keys(key,email,plan,daily_limit,expires_at,status,notes,created_at)
                  VALUES(?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM api_keys WHERE key=?),?))""",
               (key, email, plan, int(daily_limit), expires_at, status, notes, key, created_at))
    db.commit()
    return key

def get_key_row(api_key):
    db = get_db()
    cur = db.execute("SELECT key,email,plan,daily_limit,expires_at,status,notes,created_at FROM api_keys WHERE key=?", (api_key,))
    row = cur.fetchone()
    if not row: return None
    obj = {
        "key": row[0], "email": row[1], "plan": row[2],
        "daily_limit": int(row[3]),
        "expires_at": row[4], "status": row[5],
        "notes": row[6], "created_at": row[7]
    }
    # check expiry/status
    if obj["status"] != "active": return None
    if obj["expires_at"]:
        try:
            if datetime.date.fromisoformat(obj["expires_at"]) < datetime.date.today():
                return None
        except:  # bad date format -> treat as expired
            return None
    return obj

# --- Auth & quota using api_keys table ---
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

def require_api_key(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        key = request.headers.get("X-API-Key","").strip() or (request.get_json(silent=True) or {}).get("api_key","")
        row = get_key_row(key)
        if not row:
            return jsonify({"error":"Unauthorized: missing/invalid/expired API key"}), 401
        g.api_key = row["key"]
        g.daily_limit = row["daily_limit"]
        return func(*args, **kwargs)
    return wrapper

def require_admin(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-Admin-Token","")
        if not ADMIN_TOKEN or token != ADMIN_TOKEN:
            return jsonify({"error":"Forbidden"}), 403
        return func(*args, **kwargs)
    return wrapper

@app.get("/auth/check")
@require_api_key
def auth_check():
    used = _get_usage(g.api_key)
    return jsonify({"ok": True, "limit": g.daily_limit, "used": used, "remaining": max(0, g.daily_limit - used)})

@app.get("/usage")
@require_api_key
def usage_get():
    used = _get_usage(g.api_key)
    return jsonify({"limit": g.daily_limit, "used": used, "remaining": max(0, g.daily_limit - used)})

@app.post("/usage/charge")
@require_api_key
def usage_charge():
    data = request.get_json(force=True)
    amt = max(1, int(data.get("amount", 1)))
    used = _get_usage(g.api_key)
    if used + amt > g.daily_limit:
        return jsonify({"error":"Daily limit reached", "used": used, "limit": g.daily_limit}), 429
    newc = _inc_usage(g.api_key, amt)
    return jsonify({"ok": True, "used": newc, "limit": g.daily_limit, "remaining": max(0, g.daily_limit - newc)})

# --- STRIPE WEBHOOK (with email notifications) ---
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:5000")

# Map Stripe Price IDs to key configuration
# REPLACE THESE with your actual Stripe Price IDs from your Stripe Dashboard
PLAN_MAP = {
    # Replace these example Price IDs with your actual ones:
    "price_1ABC123EXAMPLE": {"plan":"basic","daily_limit":50,"days_valid":30},
    "price_1XYZ789EXAMPLE": {"plan":"pro","daily_limit":200,"days_valid":30},
}

# Map human plan names -> Stripe Price IDs (update these to match your actual products)
PRICE_MAP = {
    # Update these to match your actual Stripe Price IDs:
    "basic": "price_1ABC123EXAMPLE",
    "pro": "price_1XYZ789EXAMPLE",
}

STRIPE_API_KEY = os.getenv("STRIPE_API_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

def _expire_date(days):
    return (datetime.date.today() + datetime.timedelta(days=int(days))).isoformat()

def _issue_key_from_price(email, price_id):
    if not price_id or price_id not in PLAN_MAP:
        return jsonify({"ok": True, "note": "Unknown price_id; no key issued"}), 200

    plan = PLAN_MAP[price_id]
    key = gen_key("live_")
    expires_at = _expire_date(plan["days_valid"])
    upsert_key(
        key=key, email=email or "", plan=plan["plan"],
        daily_limit=plan["daily_limit"], expires_at=expires_at,
        status="active", notes=f"issued via webhook price={price_id}"
    )

    # Email the key
    subject = "Your Universal Prompt Optimizer API Key"
    text = f"""Thanks for your purchase!

Plan: {plan['plan']}
Daily limit: {plan['daily_limit']} generations/day
Expires: {expires_at}

Your API Key:
{key}

How to use:
1) Open the app, click Settings.
2) Paste the key in "API Key".
3) Click Save. You'll see your remaining quota.

Keep this key secret. If you need help, just reply to this email.

— UPO Team
"""
    ok = send_email(email, subject, text)
    return jsonify({"ok": True, "api_key": key, "emailed": ok}), 200

@app.post("/stripe/webhook")
def stripe_webhook():
    # Minimal parse (SDK verification optional)
    try:
        event = json.loads(request.get_data(as_text=True))
    except Exception:
        return jsonify({"error":"Invalid payload"}), 400

    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    if etype == "checkout.session.completed":
        email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email") or ""
        # We recommend passing price_id via Checkout Session metadata for reliability:
        price_id = (obj.get("metadata") or {}).get("price_id")
        return _issue_key_from_price(email, price_id)

    elif etype in ("invoice.paid", "customer.subscription.created"):
        lines = (obj.get("lines") or {}).get("data") or []
        price_id = None
        if lines:
            try: price_id = lines[0]["price"]["id"]
            except: pass
        email = obj.get("customer_email") or ""
        return _issue_key_from_price(email, price_id)

    return jsonify({"ok": True})

# -------- Admin endpoints (manual ops) --------
@app.post("/admin/issue")
@require_admin
def admin_issue():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip()
    plan  = (data.get("plan") or "manual").strip()
    daily_limit = int(data.get("daily_limit", 50))
    days_valid  = int(data.get("days_valid", 30))
    key = gen_key("live_")
    expires_at = _expire_date(days_valid)
    upsert_key(key, email, plan, daily_limit, expires_at, "active", "issued manually")
    return jsonify({"ok": True, "api_key": key, "expires_at": expires_at})

@app.post("/admin/revoke")
@require_admin
def admin_revoke():
    data = request.get_json(force=True)
    key = (data.get("key") or "").strip()
    db = get_db()
    db.execute("UPDATE api_keys SET status='revoked' WHERE key=?", (key,))
    db.commit()
    return jsonify({"ok": True})

@app.post("/admin/update_limit")
@require_admin
def admin_update_limit():
    data = request.get_json(force=True)
    key = (data.get("key") or "").strip()
    daily_limit = int(data.get("daily_limit", 50))
    db = get_db()
    db.execute("UPDATE api_keys SET daily_limit=? WHERE key=?", (daily_limit, key))
    db.commit()
    return jsonify({"ok": True})

@app.get("/admin/keys")
@require_admin
def admin_keys():
    db = get_db()
    cur = db.execute("SELECT key,email,plan,daily_limit,expires_at,status,created_at FROM api_keys ORDER BY created_at DESC")
    rows = cur.fetchall()
    items = []
    for r in rows:
        items.append({
            "key": r[0], "email": r[1], "plan": r[2], "daily_limit": r[3],
            "expires_at": r[4], "status": r[5], "created_at": r[6]
        })
    return jsonify({"keys": items})

@app.get("/admin/leads")
@require_admin
def admin_leads():
    """View captured leads from share pages"""
    db = get_db()
    cur = db.execute("""SELECT id, email, share_token, ip_address, created_at, source 
                        FROM leads ORDER BY created_at DESC LIMIT 500""")
    rows = cur.fetchall()
    leads = []
    for r in rows:
        leads.append({
            "id": r[0], "email": r[1], "share_token": r[2], 
            "ip_address": r[3], "created_at": r[4], "source": r[5]
        })
    return jsonify({"leads": leads, "total": len(leads)})

@app.get("/admin/analytics")
@require_admin
def admin_analytics():
    """View share page analytics"""
    db = get_db()
    
    # Recent visits
    cur = db.execute("""SELECT share_token, ip_address, user_agent, referrer, visited_at 
                        FROM share_visits ORDER BY visited_at DESC LIMIT 200""")
    visits = []
    for r in cur.fetchall():
        visits.append({
            "share_token": r[0], "ip_address": r[1], "user_agent": r[2],
            "referrer": r[3], "visited_at": r[4]
        })
    
    # Visit counts by token
    cur = db.execute("""SELECT share_token, COUNT(*) as visit_count 
                        FROM share_visits GROUP BY share_token ORDER BY visit_count DESC LIMIT 50""")
    popular_shares = []
    for r in cur.fetchall():
        popular_shares.append({"share_token": r[0], "visits": r[1]})
    
    # Daily visit stats
    cur = db.execute("""SELECT DATE(visited_at) as visit_date, COUNT(*) as daily_visits 
                        FROM share_visits GROUP BY DATE(visited_at) ORDER BY visit_date DESC LIMIT 30""")
    daily_stats = []
    for r in cur.fetchall():
        daily_stats.append({"date": r[0], "visits": r[1]})
    
    return jsonify({
        "recent_visits": visits,
        "popular_shares": popular_shares,
        "daily_stats": daily_stats
    })

# --- CHECKOUT + BUY PAGE ---
@app.post("/checkout/create")
def checkout_create():
    """
    Body: { "plan": "basic", "email": "buyer@example.com" }
    Returns: { "url": "https://checkout.stripe.com/..." }
    """
    data = request.get_json(force=True)
    plan = (data.get("plan") or "").strip()
    email = (data.get("email") or "").strip()
    price_id = PRICE_MAP.get(plan)
    if not price_id:
        return jsonify({"error":"Unknown plan"}), 400

    # Create Checkout Session via Stripe API (no SDK)
    # Also pass price_id in metadata so webhook can read it reliably
    form = {
        "mode": "payment",
        "success_url": f"{PUBLIC_BASE_URL}/buy?success=1",
        "cancel_url": f"{PUBLIC_BASE_URL}/buy?canceled=1",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "metadata[price_id]": price_id,
    }
    if email:
        form["customer_email"] = email

    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        "https://api.stripe.com/v1/checkout/sessions",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {os.getenv('STRIPE_API_KEY','')}",
                 "Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return jsonify({"error": f"Stripe error: {e}"}), 502

    return jsonify({"url": out.get("url")})

# Minimal "Buy" page (client-side)
BUY_HTML = """
<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Buy API Access – Universal Prompt Optimizer</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#0b0f14;color:#e7edf5;margin:0}
.wrap{max-width:720px;margin:40px auto;padding:0 16px}
.card{background:#111826;border:1px solid #1f2a3a;border-radius:14px;padding:16px;box-shadow:0 10px 30px rgba(0,0,0,.25);margin-bottom:16px}
label{display:block;margin:8px 0 4px;color:#9fb2c7}
input,select,button{width:100%;padding:10px 12px;border-radius:10px;border:1px solid #243447;background:#0f141c;color:#e7edf5}
button{background:#2f6df6;border:none;font-weight:700;cursor:pointer;margin-top:10px}
small{color:#9fb2c7}
.notice{padding:10px;border-radius:10px;margin-bottom:12px}
.ok{background:#10261a;border:1px solid #1f6d3a;color:#a8e1bd}
.err{background:#2a0f13;border:1px solid #7a2230;color:#f2b6c0}
</style></head>
<body>
<div class="wrap">
  <h1>Buy API Access</h1>
  <p>Pay once, receive an email with your API key instantly. Paste it in the app under <b>Settings → API Key</b>.</p>

  <div id="msg"></div>

  <div class="card">
    <label>Email (receive your key)</label>
    <input id="email" type="email" placeholder="you@example.com">

    <label>Plan</label>
    <select id="plan">
      <option value="basic">Basic – 50/day, 30 days</option>
      <option value="pro">Pro – 200/day, 30 days</option>
    </select>

    <button onclick="checkout()">Proceed to Payment</button>
    <small>Payments handled by Stripe. You'll be redirected.</small>
  </div>

  <div class="card">
    <h3>After payment</h3>
    <p>Check your inbox for <b>"Your Universal Prompt Optimizer API Key"</b>. If it doesn't arrive in 2 minutes, check spam or contact support.</p>
  </div>
</div>
<script>
function qs(k){ return new URLSearchParams(location.search).get(k); }
(function init(){
  const m=document.getElementById('msg');
  if(qs('success')) m.innerHTML='<div class="notice ok">Payment received! Your API key is being emailed to you. Please check your inbox.</div>';
  if(qs('canceled')) m.innerHTML='<div class="notice err">Payment canceled. You can try again anytime.</div>';
})();
async function checkout(){
  const email=document.getElementById('email').value.trim();
  const plan=document.getElementById('plan').value;
  if(!email){ alert('Enter your email'); return; }
  const res=await fetch('/checkout/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({plan,email})});
  const out=await res.json();
  if(!res.ok || !out.url){ alert(out.error||'Failed to create checkout'); return; }
  location.href = out.url;
}
</script>
</body></html>
"""

@app.get("/buy")
def buy_page():
    return BUY_HTML

# --- SHARE ENDPOINTS ---
@app.post("/share/create")
@require_api_key
def share_create():
    """
    Body: { title?, images: [urls], params: {...}, days_valid?: 30 }
    Returns: { url, token }
    """
    data = request.get_json(force=True)
    images = data.get("images") or []
    params = data.get("params") or {}
    if not images: return jsonify({"error":"No images"}), 400
    days = int(data.get("days_valid", 30))
    token = _new_token()
    payload = {
        "images": images,
        "params": params,
    }
    db = get_db()
    db.execute(
        "INSERT INTO shares(token,created_at,title,meta_json,expires_at) VALUES(?,?,?,?,?)",
        (
            token,
            datetime.datetime.utcnow().isoformat(),
            (data.get("title") or "").strip(),
            json.dumps(payload),
            (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
        )
    )
    db.commit()
    base = os.getenv("PUBLIC_BASE_URL", "")
    url = f"{base}/s/{token}" if base else f"/s/{token}"
    return jsonify({ "ok": True, "url": url, "token": token })

@app.get("/s/<token>")
def share_view(token):
    # Track analytics
    _track_share_visit(token)
    
    db = get_db()
    cur = db.execute("SELECT title, meta_json, expires_at, created_at FROM shares WHERE token=?", (token,))
    row = cur.fetchone()
    if not row: return Response("Not found", 404)
    title, meta_json, expires_at, created_at = row
    if expires_at:
        try:
            if datetime.date.fromisoformat(expires_at) < datetime.date.today():
                return Response("Link expired", 410)
        except:
            pass
    try:
        meta = json.loads(meta_json)
    except:
        return Response("Corrupt share data", 500)

    # Build branded marketing funnel page
    imgs = "".join([f'<img src="{u}" style="max-width:100%;border-radius:10px;border:2px solid #1f2a3a;margin:8px 0;box-shadow:0 4px 20px rgba(0,255,255,0.1)">' for u in meta.get("images",[])])
    params = json.dumps(meta.get("params",{}), indent=2)
    ttl = (title or "Shared Generation")
    
    # Create email body for order request (URL encoded)
    import urllib.parse
    order_params = "\n".join([f"{k}: {v}" for k,v in meta.get("params",{}).items() if v])
    order_subject = urllib.parse.quote(f"Order Similar Image Pack - {ttl}")
    order_body = urllib.parse.quote(f"""Hi Chaos Venice Productions,

I'd like to order a similar image pack based on this generation:

Title: {ttl}
Parameters:
{order_params}

Please let me know pricing and delivery time.

Best regards""")

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{ttl} - Chaos Venice Productions</title>
<style>
body{{font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:linear-gradient(135deg,#0a0e1a 0%,#0f1419 50%,#1a1f2e 100%);color:#e7edf5;margin:0;min-height:100vh}}
.wrap{{max-width:1000px;margin:0 auto;padding:20px}}
.header{{text-align:center;margin-bottom:40px;padding:30px 20px;background:linear-gradient(45deg,rgba(0,255,255,0.1),rgba(255,0,255,0.1));border-radius:20px;border:1px solid #1f2a3a}}
.logo{{font-size:2.5rem;font-weight:800;background:linear-gradient(45deg,#00ffff,#ff00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:10px}}
.tagline{{font-size:1.1rem;color:#9fb2c7;font-style:italic}}
.card{{background:rgba(17,24,38,0.8);border:1px solid #1f2a3a;border-radius:16px;padding:24px;box-shadow:0 10px 40px rgba(0,0,0,.4);margin-bottom:24px;backdrop-filter:blur(10px)}}
.images-section img{{transition:transform 0.3s ease;cursor:pointer}}
.images-section img:hover{{transform:scale(1.02)}}
.cta-section{{background:linear-gradient(135deg,rgba(0,255,255,0.1),rgba(255,0,255,0.1));border:2px solid transparent;background-clip:padding-box}}
.cta-grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px}}
@media(max-width:768px){{.cta-grid{{grid-template-columns:1fr}}}}
.btn{{display:inline-block;padding:16px 24px;background:linear-gradient(45deg,#00ffff,#ff00ff);border-radius:12px;color:#000;text-decoration:none;font-weight:600;text-align:center;transition:all 0.3s ease;box-shadow:0 4px 20px rgba(0,255,255,0.3)}}
.btn:hover{{transform:translateY(-2px);box-shadow:0 8px 30px rgba(0,255,255,0.5)}}
.btn-secondary{{background:linear-gradient(45deg,#1a1f2e,#2a2f3e);color:#e7edf5;box-shadow:0 4px 20px rgba(255,255,255,0.1)}}
.btn-secondary:hover{{box-shadow:0 8px 30px rgba(255,255,255,0.2)}}
.lead-form{{background:rgba(0,0,0,0.3);padding:20px;border-radius:12px;margin-top:20px}}
.form-group{{margin-bottom:15px}}
.form-group input{{width:100%;padding:12px;background:rgba(255,255,255,0.1);border:1px solid #1f2a3a;border-radius:8px;color:#e7edf5;font-size:16px}}
.form-group input::placeholder{{color:#9fb2c7}}
pre{{white-space:pre-wrap;background:#0f141c;border:1px solid #1f2a3a;padding:16px;border-radius:12px;overflow:auto;font-family:'JetBrains Mono',monospace}}
small{{color:#9fb2c7}}
.download-section{{text-align:center;margin-top:20px}}
.success-message{{background:rgba(0,255,0,0.1);border:1px solid rgba(0,255,0,0.3);padding:15px;border-radius:8px;margin-top:15px;display:none}}
.footer{{text-align:center;margin-top:40px;padding:20px;border-top:1px solid #1f2a3a;color:#9fb2c7}}
</style></head><body>
<div class="wrap">
  <!-- Header/Branding -->
  <div class="header">
    <div class="logo">CHAOS VENICE PRODUCTIONS</div>
    <div class="tagline">Where Imagination Meets Precision</div>
  </div>

  <!-- Title -->
  <div class="card">
    <h1 style="margin:0;font-size:2rem;background:linear-gradient(45deg,#00ffff,#ff00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent">{ttl}</h1>
    <small>Created: {created_at} UTC</small>
  </div>

  <!-- Images -->
  <div class="card images-section">{imgs}</div>

  <!-- CTA Section -->
  <div class="card cta-section">
    <h3 style="text-align:center;margin-bottom:20px;color:#00ffff">Love This Style? Get More!</h3>
    <div class="cta-grid">
      <a href="mailto:orders@chaosvenice.com?subject={order_subject}&body={order_body}" class="btn">📦 Order Similar Image Pack</a>
      <a href="/contact" class="btn btn-secondary">🎨 Hire Us to Create More</a>
    </div>
    <div style="text-align:center;color:#9fb2c7;font-size:0.9rem">
      Professional AI art generation • Custom workflows • Enterprise solutions
    </div>

    <!-- Lead Capture for Downloads -->
    <div class="lead-form" id="leadForm">
      <h4 style="margin-top:0;color:#ff00ff">Download High-Quality Images</h4>
      <p style="margin-bottom:15px;color:#9fb2c7">Enter your email to download these images as a ZIP file:</p>
      <div class="form-group">
        <input type="email" id="emailInput" placeholder="your@email.com" required>
      </div>
      <button onclick="submitLead()" class="btn" style="width:100%">Get Download Link</button>
      <div class="success-message" id="successMessage">
        <p>✅ Thank you! Your download will start automatically.</p>
        <p>We'll also send you updates about new AI art techniques and exclusive offers.</p>
      </div>
    </div>
  </div>

  <!-- Parameters -->
  <div class="card">
    <h3 style="color:#00ffff">Generation Parameters</h3>
    <pre>{params}</pre>
  </div>

  <!-- Footer -->
  <div class="footer">
    <p>© 2025 Chaos Venice Productions. Pushing the boundaries of AI creativity.</p>
    <p style="font-size:0.8rem">This content was generated using our Universal Prompt Optimizer platform.</p>
  </div>
</div>

<script>
async function submitLead() {{
  const email = document.getElementById('emailInput').value;
  if (!email || !email.includes('@')) {{
    alert('Please enter a valid email address');
    return;
  }}

  try {{
    const response = await fetch('/share/capture-lead', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        email: email,
        share_token: '{token}'
      }})
    }});

    if (response.ok) {{
      document.getElementById('leadForm').style.display = 'none';
      document.getElementById('successMessage').style.display = 'block';
      
      // Trigger download
      const images = {meta.get("images",[])};
      if (images.length > 0) {{
        const zipResponse = await fetch('/zip', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{urls: images}})
        }});
        
        if (zipResponse.ok) {{
          const blob = await zipResponse.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = 'chaos_venice_images.zip';
          a.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        }}
      }}
    }} else {{
      alert('Error capturing email. Please try again.');
    }}
  }} catch (error) {{
    alert('Network error. Please try again.');
  }}
}}
</script>
</body></html>"""
    return Response(html, 200, mimetype="text/html")

@app.post("/share/delete")
@require_api_key
def share_delete():
    data = request.get_json(force=True)
    token = (data.get("token") or "").strip()
    if not token: return jsonify({"error":"Missing token"}), 400
    db = get_db()
    db.execute("DELETE FROM shares WHERE token=?", (token,))
    db.commit()
    return jsonify({"ok": True})

@app.post("/share/capture-lead")
def share_capture_lead():
    """Capture lead email from share page downloads"""
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip()
    share_token = (data.get("share_token") or "").strip()
    
    if not email or "@" not in email:
        return jsonify({"error": "Valid email required"}), 400
    
    _capture_lead(email, share_token, "share_download")
    return jsonify({"ok": True, "message": "Email captured successfully"})

@app.get("/contact")
def contact_page():
    """Contact/booking page for Chaos Venice Productions"""
    html = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Contact - Chaos Venice Productions</title>
<style>
body{font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:linear-gradient(135deg,#0a0e1a 0%,#0f1419 50%,#1a1f2e 100%);color:#e7edf5;margin:0;min-height:100vh}
.wrap{max-width:800px;margin:0 auto;padding:20px}
.header{text-align:center;margin-bottom:40px;padding:30px 20px;background:linear-gradient(45deg,rgba(0,255,255,0.1),rgba(255,0,255,0.1));border-radius:20px;border:1px solid #1f2a3a}
.logo{font-size:2.5rem;font-weight:800;background:linear-gradient(45deg,#00ffff,#ff00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:10px}
.tagline{font-size:1.1rem;color:#9fb2c7;font-style:italic}
.card{background:rgba(17,24,38,0.8);border:1px solid #1f2a3a;border-radius:16px;padding:24px;box-shadow:0 10px 40px rgba(0,0,0,.4);margin-bottom:24px;backdrop-filter:blur(10px)}
.contact-grid{display:grid;grid-template-columns:1fr 1fr;gap:30px;margin-top:30px}
@media(max-width:768px){.contact-grid{grid-template-columns:1fr}}
.contact-method{text-align:center;padding:20px;background:rgba(0,0,0,0.3);border-radius:12px;border:1px solid #1f2a3a}
.contact-method h3{color:#00ffff;margin-bottom:10px}
.contact-method a{color:#ff00ff;text-decoration:none;font-weight:600}
.contact-method a:hover{text-decoration:underline}
.services{margin-top:30px}
.service-item{padding:15px;margin-bottom:15px;background:rgba(0,255,255,0.05);border-left:3px solid #00ffff;border-radius:8px}
</style></head><body>
<div class="wrap">
  <div class="header">
    <div class="logo">CHAOS VENICE PRODUCTIONS</div>
    <div class="tagline">Where Imagination Meets Precision</div>
  </div>
  
  <div class="card">
    <h1 style="text-align:center;background:linear-gradient(45deg,#00ffff,#ff00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent">Let's Create Something Amazing</h1>
    <p style="text-align:center;color:#9fb2c7;font-size:1.1rem">Ready to bring your creative vision to life? We specialize in cutting-edge AI art generation and custom creative solutions.</p>
    
    <div class="contact-grid">
      <div class="contact-method">
        <h3>📧 Email Us</h3>
        <p><a href="mailto:orders@chaosvenice.com">orders@chaosvenice.com</a></p>
        <p style="color:#9fb2c7;font-size:0.9rem">For project inquiries and custom orders</p>
      </div>
      
      <div class="contact-method">
        <h3>💼 Business Inquiries</h3>
        <p><a href="mailto:business@chaosvenice.com">business@chaosvenice.com</a></p>
        <p style="color:#9fb2c7;font-size:0.9rem">Enterprise solutions and partnerships</p>
      </div>
    </div>
  </div>
  
  <div class="card services">
    <h2 style="color:#00ffff;text-align:center">Our Services</h2>
    <div class="service-item">
      <h3 style="color:#ff00ff">Custom AI Art Generation</h3>
      <p>Bespoke image creation using advanced AI models and custom workflows</p>
    </div>
    <div class="service-item">
      <h3 style="color:#ff00ff">Brand Asset Creation</h3>
      <p>Logo design, marketing materials, and complete brand identity packages</p>
    </div>
    <div class="service-item">
      <h3 style="color:#ff00ff">Enterprise Solutions</h3>
      <p>Large-scale content generation, API integration, and custom platform development</p>
    </div>
    <div class="service-item">
      <h3 style="color:#ff00ff">Creative Consulting</h3>
      <p>Guidance on AI art workflows, prompt engineering, and creative strategy</p>
    </div>
  </div>
  
  <div class="card" style="text-align:center">
    <h3 style="color:#00ffff">Ready to Start?</h3>
    <p style="margin-bottom:20px">Tell us about your project and we'll get back to you within 24 hours.</p>
    <a href="mailto:orders@chaosvenice.com?subject=New Project Inquiry" style="display:inline-block;padding:16px 32px;background:linear-gradient(45deg,#00ffff,#ff00ff);border-radius:12px;color:#000;text-decoration:none;font-weight:600">Start a Project</a>
  </div>
</div>
</body></html>"""
    return Response(html, 200, mimetype="text/html")

# ---------------------------
# Prompt Enhancement Engine
# ---------------------------

# Quality, style, mood terms for consistent categorization
STYLE_KEYWORDS = {
    "quality": ["masterpiece", "best quality", "ultra high detail", "8k", "4k", "highres", "ultra detailed", "extremely detailed", "intricate", "sharp focus", "professional"],
    "art_styles": ["photorealistic", "hyperrealistic", "cinematic", "digital art", "oil painting", "watercolor", "anime", "manga", "concept art", "impressionist", "baroque", "renaissance", "art nouveau", "cyberpunk", "steampunk", "minimalist"],
    "photography": ["bokeh", "depth of field", "macro", "wide angle", "telephoto", "portrait", "landscape", "street photography", "documentary", "fashion photography"],
    "lighting": ["soft lighting", "hard lighting", "natural lighting", "studio lighting", "golden hour", "blue hour", "backlighting", "rim lighting", "volumetric lighting", "chiaroscuro"],
    "composition": ["rule of thirds", "centered", "symmetrical", "leading lines", "framing", "negative space", "close-up", "medium shot", "wide shot", "bird's eye view", "worm's eye view"],
    "mood": ["moody", "dramatic", "serene", "melancholic", "uplifting", "mysterious", "romantic", "energetic", "peaceful", "tense", "nostalgic", "futuristic"],
    "color_grades": ["vibrant", "desaturated", "monochrome", "sepia", "teal and orange", "warm tones", "cool tones", "high contrast", "low contrast", "film grain"]
}

# Comprehensive negative defaults
NEGATIVE_DEFAULT = ["lowres", "bad anatomy", "bad hands", "text", "error", "missing fingers", "extra digit", "fewer digits", "cropped", "worst quality", "low quality", "normal quality", "jpeg artifacts", "signature", "watermark", "username", "blurry", "bad feet", "cropped", "poorly drawn hands", "poorly drawn face", "mutation", "deformed", "worst quality", "low quality", "normal quality", "jpeg artifacts", "signature", "watermark", "extra fingers", "fewer digits", "extra limbs", "extra arms", "extra legs", "malformed limbs", "fused fingers", "too many fingers", "long neck", "cross-eyed", "mutated hands", "polar lowres", "bad body", "bad proportions", "gross proportions", "text", "error", "missing fingers", "missing arms", "missing legs", "extra digit", "extra arms", "extra leg", "extra foot"]

# Aspect ratio mappings
AR_TO_RES = {"1:1": (1024, 1024), "16:9": (1344, 768), "9:16": (768, 1344), "2:3": (832, 1216), "3:2": (1216, 832)}

# Common stopwords to filter out
STOPWORDS = set("""
a an the of and to in on at by for with from into over under between
is are was were be been being do does did have has had can will would should
this that these those as if then than so such very really just it its it's
""".split())

# ---------------------------
# Core Processing Functions
# ---------------------------

def tokenize(text):
    """Extract alphanumeric tokens from text."""
    return re.findall(r"[A-Za-z0-9\-+/#']+", text.lower())

def dedup_preserve(seq):
    """Remove duplicates while preserving order."""
    seen = set()
    out = []
    for x in seq:
        if x not in seen and x:
            seen.add(x)
            out.append(x)
    return out

def clamp_length(s, max_chars=850):
    """Prevent overly long prompts that degrade quality."""
    return (s[:max_chars] + "…") if len(s) > max_chars else s

def extract_categories(user_text: str):
    """Extract style keywords and subject terms from user input."""
    words = tokenize(user_text)
    text = user_text.lower()

    found = {k: [] for k in STYLE_KEYWORDS}
    for k, arr in STYLE_KEYWORDS.items():
        for term in arr:
            if term in text:
                found[k].append(term)

    # Subject terms are remaining meaningful words after removing style terms and stopwords
    used_style_words = set()
    for style_list in found.values():
        for term in style_list:
            used_style_words.update(tokenize(term))

    subject_terms = [w for w in words 
                    if w not in STOPWORDS 
                    and w not in used_style_words 
                    and len(w) > 2]
    
    return found, subject_terms

def build_positive(subject_terms, found_styles, extras):
    """Construct optimized positive prompt with strict order: quality → subject → style → lighting → composition → mood → color grade → extra tags."""
    parts = []
    
    # 1. Quality descriptors (always first)
    if found_styles["quality"]:
        parts.extend(found_styles["quality"][:2])  # Limit to avoid redundancy
    
    # 2. Core subject
    parts.extend(subject_terms[:8])  # Main nouns/descriptors from user input
    
    # 3. Art style and photography techniques
    parts.extend(found_styles["art_styles"][:2])
    parts.extend(found_styles["photography"][:2])
    
    # 4. Lighting (priority for user-specified or detected)
    lighting_terms = []
    if extras.get("lighting"):
        lighting_terms.extend(tokenize(extras["lighting"]))
    lighting_terms.extend(found_styles["lighting"][:2])
    parts.extend(lighting_terms[:2])
    
    # 5. Composition
    parts.extend(found_styles["composition"][:2])
    
    # 6. Mood
    parts.extend(found_styles["mood"][:2])
    
    # 7. Color grade (priority for user-specified)
    color_terms = []
    if extras.get("color_grade"):
        color_terms.extend(tokenize(extras["color_grade"]))
    color_terms.extend(found_styles["color_grades"][:2])
    parts.extend(color_terms[:2])
    
    # 8. Extra tags (user-specified)
    if extras.get("extra_tags"):
        parts.extend(tokenize(extras["extra_tags"])[:3])
    
    # Deduplicate and join
    final_parts = dedup_preserve(parts)
    result = ", ".join(final_parts)
    return clamp_length(result)

def build_negative(user_negative):
    """Construct enhanced negative prompt covering anatomy/structure, artifacts/quality, branding/text, style pitfalls."""
    parts = list(NEGATIVE_DEFAULT)  # Start with comprehensive defaults
    
    if user_negative:
        user_terms = [term.strip() for term in user_negative.split(",") if term.strip()]
        parts.extend(user_terms)
    
    final_parts = dedup_preserve(parts)
    result = ", ".join(final_parts)
    return clamp_length(result)

def sdxl_prompt(positive, negative, aspect_ratio):
    """Generate SDXL-optimized prompt configuration."""
    w, h = AR_TO_RES.get(aspect_ratio, (1024, 1024))
    return {
        "positive": positive,
        "negative": negative,
        "settings": {
            "width": w, "height": h, "steps": 30, "cfg_scale": 6.5, 
            "sampler": "DPM++ 2M Karras", "scheduler": "karras", "model": "SDXL"
        }
    }

def comfyui_recipe(positive, negative, aspect_ratio):
    """Generate ComfyUI workflow configuration with execution tips."""
    w, h = AR_TO_RES.get(aspect_ratio, (1024, 1024))
    return {
        "positive": positive,
        "negative": negative,
        "settings": {"width": w, "height": h, "steps": 30, "cfg_scale": 6.5, "sampler": "dpmpp_2m"},
        "execution_tips": [
            "Use SDXL base model for best results",
            "Enable 'Tiled VAE' if getting VRAM errors", 
            "Consider refiner model for final 20% of steps"
        ]
    }

def midjourney_prompt(positive, aspect_ratio):
    """Generate Midjourney v6 prompt with proper flags."""
    ar_map = {"1:1": "1:1", "16:9": "16:9", "9:16": "9:16", "2:3": "2:3", "3:2": "3:2"}
    ar = ar_map.get(aspect_ratio, "1:1")
    ar_flag = f"--ar {ar}"
    # Replace "8k" with "ultra high detail" for MJ compatibility
    clean_positive = positive.replace("8k", "ultra high detail")
    return f"{clean_positive} --v 6 {ar_flag} --stylize 200 --chaos 5"

def pika_prompt(positive):
    """Generate Pika Labs video prompt configuration."""
    return {
        "prompt": f"{positive}, animated micro-details, smooth motion, temporal consistency",
        "motion": "subtle camera push-in, natural parallax, no warping",
        "duration_sec": 6,
        "guidance": 7.0
    }

def runway_prompt(positive):
    """Generate Runway ML video prompt configuration."""
    return {
        "text_prompt": f"{positive}",
        "camera_motion": "push_in",
        "motion_strength": "medium",
        "duration_sec": 5,
        "notes": "Keep subject centrally framed to reduce morphing."
    }

# ---------------------------
# Routes
# ---------------------------
INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Universal Prompt Optimizer</title>
<style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#0b0f14;color:#e7edf5}
.container{max-width:980px;margin:36px auto;padding:0 16px}
.card{background:#111826;border:1px solid #1f2a3a;border-radius:14px;padding:16px;box-shadow:0 10px 30px rgba(0,0,0,.25);margin-bottom:16px}
h1{margin:0 0 6px;font-size:28px}.sub{margin:0 0 18px;color:#9fb2c7}
label{display:block;font-size:14px;color:#a9bdd4;margin-bottom:6px}
textarea,input,select{width:100%;padding:10px 12px;border-radius:10px;border:1px solid #243447;background:#0f141c;color:#e7edf5}
textarea::placeholder,input::placeholder{color:#627a91}.row{display:flex;gap:12px;margin-top:12px;flex-wrap:wrap}.col{flex:1;min-width:240px}
button{margin-top:10px;padding:10px 14px;background:#2f6df6;border:none;border-radius:10px;color:#fff;font-weight:600;cursor:pointer}
button.secondary{background:#1e293b}button.danger{background:#9b1c1c}
button:hover{filter:brightness(1.05)}.results{margin-top:22px}.hidden{display:none}
.box{background:#0f141c;border:1px solid #1f2a3a;padding:12px;border-radius:10px;white-space:pre-wrap}
.section{margin-top:14px}.rowbtns{display:flex;gap:8px;flex-wrap:wrap}
.kv{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:12px;color:#bcd0e3}
small,.small{font-size:12px;color:#7f93a7}.badge{display:inline-block;padding:4px 8px;border:1px solid #2d3b4e;border-radius:999px;margin-right:6px;color:#a9bdd4}
footer{margin-top:24px;text-align:center;color:#6f859c;font-size:12px}
h3{margin:8px 0}select, input[type="text"] {height:40px}textarea {min-height:110px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;padding:16px}
.modal .inner{background:#0f141c;border:1px solid #223045;border-radius:14px;max-width:720px;width:100%;padding:16px}
.modal .actions{display:flex;gap:10px;justify-content:flex-end;margin-top:10px}
.imgwrap{margin-top:10px;display:flex;gap:12px;flex-wrap:wrap}
.imgwrap img{max-width:320px;border-radius:10px;border:1px solid #1f2a3a}
.thumb{display:flex;flex-direction:column;gap:6px}
.adv{background:#0e1420;border:1px dashed #27405e;border-radius:12px;padding:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.meta{font-family:ui-monospace,monospace;font-size:11px;color:#9fb2c7}
.progress{margin-top:10px;background:#0f141c;border:1px solid #27405e;border-radius:10px;overflow:hidden;height:14px}
.progress .bar{height:100%;width:0%}
.badkey{color:#ff6b6b}
</style>
</head>
<body>
<div class="container">
  <h1>Universal Prompt Optimizer</h1>
  <p class="sub">Turn rough ideas into model-ready prompts for SDXL, ComfyUI, Midjourney, Pika, and Runway. Built for speed + consistency.</p>

  <!-- INPUTS -->
  <div class="card">
    <div class="row">
      <div class="col">
        <label>Your idea</label>
        <textarea id="idea" placeholder="e.g., A cozy coffee shop at golden hour, rain outside, cinematic lighting, moody vibe, shallow depth of field, candid couple"></textarea>
      </div>
    </div>

    <div class="row">
      <div class="col">
        <label>Negative prompt (optional)</label>
        <input id="negative" placeholder="e.g., lowres, watermark, bad anatomy">
      </div>
      <div class="col">
        <label>Aspect ratio</label>
        <select id="ar">
          <option selected>16:9</option>
          <option>1:1</option>
          <option>9:16</option>
          <option>2:3</option>
          <option>3:2</option>
        </select>
      </div>
    </div>

    <div class="row">
      <div class="col">
        <label>Lighting (optional)</label>
        <input id="lighting" placeholder="e.g., soft studio lighting, volumetric light">
      </div>
      <div class="col">
        <label>Color grade (optional)</label>
        <input id="color_grade" placeholder="e.g., teal and orange, Kodak Portra 400, moody grade">
      </div>
      <div class="col">
        <label>Extra tags (optional)</label>
        <input id="extra_tags" placeholder="e.g., film grain, depth of field, subsurface scattering">
      </div>
    </div>

    <div class="row">
      <div class="col small">
        <span class="badge">Tip</span> Keep nouns concrete. Add one clear mood + one lighting cue for best control.
      </div>
    </div>

    <!-- ADVANCED CONTROLS -->
    <div class="adv" style="margin-top:10px">
      <h3 style="margin-top:0">Advanced (Optional)</h3>
      <div class="row">
        <div class="col">
          <label>Steps</label>
          <input id="steps" type="text" placeholder="30">
        </div>
        <div class="col">
          <label>CFG Scale</label>
          <input id="cfg" type="text" placeholder="6.5">
        </div>
        <div class="col">
          <label>Sampler</label>
          <select id="sampler">
            <option selected>DPM++ 2M Karras</option>
            <option>DPM++ SDE Karras</option>
            <option>Euler a</option>
          </select>
        </div>
      </div>
      <div class="row">
        <div class="col">
          <label>Seed (leave blank or type "random")</label>
          <input id="seed" type="text" placeholder="random">
        </div>
        <div class="col">
          <label>Batch size (1–8)</label>
          <input id="batch" type="text" placeholder="1">
        </div>
      </div>
      <small>Advanced values override defaults during generation. Leave blank to use optimizer defaults.</small>
    </div>

    <div class="rowbtns" style="margin-top:10px">
      <button id="run">Optimize</button>
      <button class="secondary" onclick="openSettings()">Settings</button>
    </div>
  </div>

  <!-- PRESETS -->
  <div class="card">
    <h3>Presets</h3>
    <div class="row">
      <div class="col">
        <label>Preset name</label>
        <input id="presetName" placeholder="e.g., Neon Alley Cinematic">
      </div>
      <div class="col">
        <label>Load preset</label>
        <select id="presetSelect"></select>
      </div>
    </div>
    <div class="rowbtns" style="margin-top:10px">
      <button class="secondary" onclick="savePreset()">Save Preset</button>
      <button class="secondary" onclick="loadPreset()">Load</button>
      <button class="danger" onclick="deletePreset()">Delete</button>
      <button class="secondary" onclick="exportPresets()">Export JSON</button>
      <button class="secondary" onclick="importPresets()">Import JSON</button>
    </div>
    <small>Presets store: idea, negatives, AR, lighting, color grade, extra tags, and advanced settings (steps, cfg, sampler, seed, batch).</small>
  </div>

  <!-- RESULTS -->
  <div id="results" class="results hidden">
    <div class="card">
      <h3>Unified Prompt</h3>
      <div class="rowbtns">
        <button onclick="copyText('unifiedPos')">Copy Positive</button>
        <button onclick="copyText('unifiedNeg')">Copy Negative</button>
        <button onclick="downloadText('unified_positive.txt','unifiedPos')">Download .txt (Positive)</button>
        <button onclick="downloadText('unified_negative.txt','unifiedNeg')">Download .txt (Negative)</button>
      </div>
      <div id="unifiedPos" class="box section"></div>
      <div id="unifiedNeg" class="box section"></div>

      <h3>SDXL</h3>
      <div class="rowbtns">
        <button onclick="copyText('sdxlBox')">Copy JSON</button>
        <button onclick="downloadText('sdxl.json','sdxlBox')">Download JSON</button>
        <button class="secondary" id="genComfyBtn" onclick="generateComfyAsync()">Generate (ComfyUI)</button>
        <button class="secondary" id="zipBtn" style="display:none" onclick="downloadZip(LAST_IMAGES)">Download ZIP</button>
        <button class="secondary" id="shareBtn" style="display:none" onclick="shareCurrent()">Share Link</button>
      </div>
      <div id="sdxlBox" class="box section kv"></div>

      <div id="progressWrap" class="hidden">
        <div class="rowbtns">
          <button class="danger" id="cancelBtn" onclick="cancelGeneration()">Cancel</button>
          <span id="progressText" class="small"></span>
        </div>
        <div class="progress"><div id="progressBar" class="bar"></div></div>
      </div>

      <div class="imgwrap" id="genImages"></div>

      <h3>ComfyUI</h3>
      <div class="rowbtns">
        <button onclick="copyText('comfyBox')">Copy JSON</button>
        <button onclick="downloadText('comfyui.json','comfyBox')">Download JSON</button>
      </div>
      <div id="comfyBox" class="box section kv"></div>

      <h3>Midjourney</h3>
      <div class="rowbtns">
        <button onclick="copyText('mjBox')">Copy Prompt</button>
        <button onclick="downloadText('midjourney.txt','mjBox')">Download .txt</button>
      </div>
      <div id="mjBox" class="box section kv"></div>

      <h3>Pika</h3>
      <div class="rowbtns">
        <button onclick="copyText('pikaBox')">Copy JSON</button>
        <button onclick="downloadText('pika.json','pikaBox')">Download JSON</button>
      </div>
      <div id="pikaBox" class="box section kv"></div>

      <h3>Runway</h3>
      <div class="rowbtns">
        <button onclick="copyText('runwayBox')">Copy JSON</button>
        <button onclick="downloadText('runway.json','runwayBox')">Download JSON</button>
      </div>
      <div id="runwayBox" class="box section kv"></div>

      <h3>Hints</h3>
      <div id="hintsBox" class="box section small"></div>
    </div>
  </div>

  <!-- HISTORY -->
  <div class="card">
    <h3>History</h3>
    <div class="rowbtns">
      <button class="secondary" onclick="exportHistory()">Export History JSON</button>
      <button class="danger" onclick="clearHistory()">Clear History</button>
    </div>
    <div id="historyGrid" class="grid" style="margin-top:10px"></div>
    <small class="small">History is saved locally in your browser (upo_history_v1). Click "Re-run" to regenerate with the same seed and settings.</small>
  </div>

  <footer>Seed tip: lock a seed for reproducibility; vary only seed to explore variants without wrecking the look.</footer>
</div>

<!-- SETTINGS MODAL -->
<div class="modal" id="settingsModal">
  <div class="inner">
    <h3>ComfyUI Settings</h3>
    <div class="row">
      <div class="col">
        <label>API Key (required for generation)</label>
        <input id="apiKey" placeholder="enter your key or use demo123">
        <small id="quotaLine" class="small"></small>
        <small style="color:#9fb2c7;margin-top:4px;display:block;">Need a key? <a href="/buy" target="_blank" style="color:#4f9eff;">Buy API access</a> or use demo123 for testing</small>
      </div>
    </div>
    <div class="row">
      <div class="col">
        <label>ComfyUI Host (e.g., http://127.0.0.1:8188)</label>
        <input id="comfyHost" placeholder="http://127.0.0.1:8188">
      </div>
    </div>
    <div class="row">
      <div class="col">
        <label>Custom Workflow JSON (optional)</label>
        <textarea id="workflowJson" placeholder='Paste a ComfyUI "workflow_api" JSON here to override defaults'></textarea>
      </div>
    </div>
    <div class="actions">
      <button class="secondary" onclick="closeSettings()">Close</button>
      <button onclick="saveSettings()">Save</button>
    </div>
  </div>
</div>

<script>
const LS_KEY = 'upo_presets_v1';
const LS_SETTINGS = 'upo_settings_v1';
const LS_HISTORY = 'upo_history_v1';
let LAST_IMAGES = [];
let POLL = null;
let CURRENT = {host:'', pid:'', expected:1, started:0};

function getForm(){
  return {
    idea: document.getElementById('idea').value.trim(),
    negative: document.getElementById('negative').value.trim(),
    aspect_ratio: document.getElementById('ar').value,
    lighting: document.getElementById('lighting').value.trim(),
    color_grade: document.getElementById('color_grade').value.trim(),
    extra_tags: document.getElementById('extra_tags').value.trim(),
    steps: document.getElementById('steps').value.trim(),
    cfg_scale: document.getElementById('cfg').value.trim(),
    sampler: document.getElementById('sampler').value,
    seed: document.getElementById('seed').value.trim(),
    batch: document.getElementById('batch').value.trim()
  };
}
function setForm(v){
  if(!v) return;
  document.getElementById('idea').value = v.idea || '';
  document.getElementById('negative').value = v.negative || '';
  document.getElementById('ar').value = v.aspect_ratio || '16:9';
  document.getElementById('lighting').value = v.lighting || '';
  document.getElementById('color_grade').value = v.color_grade || '';
  document.getElementById('extra_tags').value = v.extra_tags || '';
  document.getElementById('steps').value = v.steps || '';
  document.getElementById('cfg').value = v.cfg_scale || '';
  document.getElementById('sampler').value = v.sampler || 'DPM++ 2M Karras';
  document.getElementById('seed').value = v.seed || '';
  document.getElementById('batch').value = v.batch || '';
}

// Presets/history helpers (unchanged from your current file) …
function loadAllPresets(){ try{ const raw=localStorage.getItem(LS_KEY); const map=raw?JSON.parse(raw):{}; const sel=document.getElementById('presetSelect'); sel.innerHTML=''; Object.keys(map).sort((a,b)=>a.localeCompare(b)).forEach(name=>{ const opt=document.createElement('option'); opt.value=name; opt.textContent=name; sel.appendChild(opt); }); }catch(e){} }
function savePreset(){ const name=(document.getElementById('presetName').value||'').trim(); if(!name){alert('Name your preset first.');return;} try{ const raw=localStorage.getItem(LS_KEY); const map=raw?JSON.parse(raw):{}; map[name]=getForm(); localStorage.setItem(LS_KEY, JSON.stringify(map)); loadAllPresets(); document.getElementById('presetSelect').value=name; }catch(e){ alert('Could not save preset.'); } }
function loadPreset(){ const sel=document.getElementById('presetSelect'); const name=sel.value; if(!name){alert('No preset selected.');return;} try{ const map=JSON.parse(localStorage.getItem(LS_KEY)||'{}'); setForm(map[name]); }catch(e){ alert('Could not load preset.'); } }
function deletePreset(){ const sel=document.getElementById('presetSelect'); const name=sel.value; if(!name){alert('No preset selected.');return;} if(!confirm(`Delete preset "${name}"?`)) return; try{ const map=JSON.parse(localStorage.getItem(LS_KEY)||'{}'); delete map[name]; localStorage.setItem(LS_KEY, JSON.stringify(map)); loadAllPresets(); }catch(e){ alert('Could not delete preset.'); } }
function exportPresets(){ try{ const raw=localStorage.getItem(LS_KEY)||'{}'; const blob=new Blob([raw],{type:'application/json'}); const url=URL.createObjectURL(blob); const a=document.createElement('a'); a.href=url; a.download='upo_presets.json'; a.click(); setTimeout(()=>URL.revokeObjectURL(url), 1000); }catch(e){ alert('Could not export presets.'); } }
function importPresets(){ const input=document.createElement('input'); input.type='file'; input.accept='application/json'; input.onchange=(e)=>{ const file=e.target.files[0]; if(!file) return; const reader=new FileReader(); reader.onload=()=>{ try{ const incoming=JSON.parse(reader.result); const current=JSON.parse(localStorage.getItem(LS_KEY)||'{}'); const merged=Object.assign(current,incoming); localStorage.setItem(LS_KEY, JSON.stringify(merged)); loadAllPresets(); alert('Presets imported.'); }catch(err){ alert('Invalid JSON.'); } }; reader.readAsText(file); }; input.click(); }

function loadHistory(){ try{ return JSON.parse(localStorage.getItem('upo_history_v1')||'[]'); }catch(e){ return []; } }
function saveHistory(arr){ localStorage.setItem('upo_history_v1', JSON.stringify(arr)); }
function addHistory(rec){ const arr=loadHistory(); arr.unshift(rec); if(arr.length>200) arr.length=200; saveHistory(arr); renderHistory(); }
function clearHistory(){ if(!confirm('Clear all history?')) return; saveHistory([]); renderHistory(); }
function exportHistory(){ const raw=localStorage.getItem('upo_history_v1')||'[]'; const blob=new Blob([raw],{type:'application/json'}); const url=URL.createObjectURL(blob); const a=document.createElement('a'); a.href=url; a.download='upo_history.json'; a.click(); setTimeout(()=>URL.revokeObjectURL(url), 1000); }
function renderHistory(){
  const grid=document.getElementById('historyGrid'); const arr=loadHistory(); grid.innerHTML='';
  arr.forEach((h)=>{
    const div=document.createElement('div'); div.className='thumb';
    const img=document.createElement('img'); img.src=(h.images&&h.images[0])||''; img.alt='generation'; img.loading='lazy'; img.style.maxWidth='100%';
    const meta=document.createElement('div'); meta.className='meta'; meta.textContent=`[${new Date(h.ts).toLocaleString()}] seed=${h.seed} steps=${h.steps} cfg=${h.cfg_scale} ${h.sampler} ${h.width}x${h.height}`;
    const btns=document.createElement('div'); btns.className='rowbtns';
    const rerun=document.createElement('button'); rerun.className='secondary'; rerun.textContent='Re-run'; rerun.onclick=()=>reRun(h);
    const dl=document.createElement('button'); dl.className='secondary'; dl.textContent='Download All'; dl.onclick=()=>downloadAll(h.images||[]);
    const zip=document.createElement('button'); zip.className='secondary'; zip.textContent='ZIP'; zip.onclick=()=>downloadZip(h.images||[]);
    const shareBtn = document.createElement('button'); shareBtn.className = 'secondary'; shareBtn.textContent = 'Share'; shareBtn.onclick = ()=>shareHistory(h);
    btns.appendChild(rerun); btns.appendChild(dl); btns.appendChild(zip); btns.appendChild(shareBtn);
    div.appendChild(img); div.appendChild(meta); div.appendChild(btns); grid.appendChild(div);
  });
}
function downloadAll(urls){ urls.forEach((u,i)=>{ const a=document.createElement('a'); a.href=u; a.download=`image_${i+1}.png`; a.click(); }); }

function openSettings(){
  const s = JSON.parse(localStorage.getItem(LS_SETTINGS) || '{}');
  document.getElementById('apiKey').value = s.apiKey || '';
  document.getElementById('comfyHost').value = s.host || '';
  document.getElementById('workflowJson').value = s.workflow || '';
  document.getElementById('quotaLine').textContent = s.limit ? `Quota: ${s.remaining}/${s.limit} remaining today` : '';
  document.getElementById('settingsModal').style.display='flex';
}
function closeSettings(){ document.getElementById('settingsModal').style.display='none'; }
function saveSettings(){
  const s = JSON.parse(localStorage.getItem(LS_SETTINGS) || '{}');
  s.apiKey = document.getElementById('apiKey').value.trim();
  s.host   = document.getElementById('comfyHost').value.trim();
  s.workflow = document.getElementById('workflowJson').value.trim();
  localStorage.setItem(LS_SETTINGS, JSON.stringify(s));
  refreshQuota();
  closeSettings();
}

async function refreshQuota(){
  const s = JSON.parse(localStorage.getItem(LS_SETTINGS) || '{}');
  if(!s.apiKey){ setQuotaLine(''); return; }
  try{
    const res = await fetch('/usage', { headers: {'X-API-Key': s.apiKey }});
    const out = await res.json();
    if(res.ok){
      s.limit = out.limit; s.remaining = out.remaining; localStorage.setItem(LS_SETTINGS, JSON.stringify(s));
      setQuotaLine(`Quota: ${out.remaining}/${out.limit} remaining today`);
    } else {
      setQuotaLine('Invalid API key', true);
    }
  }catch(e){ setQuotaLine(''); }
}
function setQuotaLine(text, bad=false){
  const el = document.getElementById('quotaLine');
  el.textContent = text || '';
  el.className = 'small' + (bad ? ' badkey' : '');
}

function copyText(id){ const el=document.getElementById(id); const txt=el?.innerText||el?.textContent||''; navigator.clipboard.writeText(txt); }
function downloadText(filename,id){ const el=document.getElementById(id); const txt=el?.innerText||el?.textContent||''; const blob=new Blob([txt],{type:'text/plain'}); const url=URL.createObjectURL(blob); const a=document.createElement('a'); a.href=url; a.download=filename; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000); }
async function downloadZip(urls){ if(!urls||!urls.length){alert('No images to zip.');return;} const res=await fetch('/zip',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({urls})}); if(!res.ok){alert('ZIP failed.');return;} const blob=await res.blob(); const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='upo_outputs.zip'; a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),1000); }

// Share functions
function copy(text){ navigator.clipboard.writeText(text); alert('Link copied to clipboard'); }

async function createShare(meta){
  const s = JSON.parse(localStorage.getItem('upo_settings_v1') || '{}');
  if(!s.apiKey){ alert('Enter your API key in Settings.'); return null; }
  const res = await fetch('/share/create', {
    method:'POST',
    headers:{'Content-Type':'application/json','X-API-Key': s.apiKey},
    body: JSON.stringify(meta)
  });
  const out = await res.json();
  if(!res.ok){ alert(out.error || 'Share failed'); return null; }
  return out.url;
}

async function shareCurrent(){
  if(!LAST_IMAGES || !LAST_IMAGES.length){ alert('No images to share'); return; }
  const f = getForm();
  const meta = {
    title: f.idea || 'Shared Generation',
    images: LAST_IMAGES,
    params: {
      steps: f.steps || undefined,
      cfg_scale: f.cfg_scale || undefined,
      sampler: f.sampler || undefined,
      seed: f.seed || undefined,
      batch: f.batch || undefined,
      aspect_ratio: f.aspect_ratio || undefined,
      lighting: f.lighting || undefined,
      color_grade: f.color_grade || undefined,
      extra_tags: f.extra_tags || undefined
    }
  };
  const url = await createShare(meta);
  if(url){ copy(url); }
}

async function shareHistory(h){
  const meta = {
    title: h.idea || 'Shared Generation',
    images: h.images || [],
    params: {
      steps: h.steps, cfg_scale: h.cfg_scale, sampler: h.sampler,
      seed: h.seed, batch: h.batch, width: h.width, height: h.height,
      aspect_ratio: h.aspect_ratio, lighting: h.lighting,
      color_grade: h.color_grade, extra_tags: h.extra_tags
    }
  };
  const url = await createShare(meta);
  if(url){ copy(url); }
}

async function optimize(){
  const payload=getForm();
  const res=await fetch('/optimize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const out=await res.json();
  document.getElementById('results').classList.remove('hidden');
  document.getElementById('unifiedPos').textContent=out.unified?.positive||'';
  document.getElementById('unifiedNeg').textContent=out.unified?.negative||'';
  document.getElementById('sdxlBox').textContent=JSON.stringify(out.sdxl,null,2);
  document.getElementById('comfyBox').textContent=JSON.stringify(out.comfyui,null,2);
  document.getElementById('mjBox').textContent=out.midjourney||'';
  document.getElementById('pikaBox').textContent=JSON.stringify(out.pika,null,2);
  document.getElementById('runwayBox').textContent=JSON.stringify(out.runway,null,2);
  document.getElementById('hintsBox').textContent=JSON.stringify(out.hints,null,2);
  const s=JSON.parse(localStorage.getItem(LS_SETTINGS)||'{}');
  document.getElementById('genComfyBtn').style.display=s.host?'inline-block':'none';
  document.getElementById('genImages').innerHTML=''; document.getElementById('zipBtn').style.display='none'; document.getElementById('shareBtn').style.display='none';
  hideProgress();
}
document.getElementById('run').addEventListener('click', optimize);

function showProgress(){ document.getElementById('progressWrap').classList.remove('hidden'); setProgress(0,'Queued...'); }
function hideProgress(){ document.getElementById('progressWrap').classList.add('hidden'); setProgress(0,''); }
function setProgress(pct, text){ const bar=document.getElementById('progressBar'); bar.style.width=(pct||0)+'%'; bar.style.background = 'linear-gradient(90deg,#2f6df6,#36c3ff)'; document.getElementById('progressText').textContent=text||''; }

async function generateComfyAsync(){
  const s=JSON.parse(localStorage.getItem(LS_SETTINGS)||'{}');
  if(!s.host){ alert('Set your ComfyUI host in Settings.'); return; }
  if(!s.apiKey){ alert('Enter your API key in Settings.'); return; }
  let sdxlJson={}; try{ sdxlJson=JSON.parse(document.getElementById('sdxlBox').textContent||'{}'); }catch(e){ alert('Invalid SDXL JSON—click Optimize again.'); return; }
  const f=getForm();
  const advanced={ steps:f.steps, cfg_scale:f.cfg_scale, sampler:f.sampler, seed:f.seed, batch:f.batch };

  // queue job
  const q=await fetch('/generate/comfy_async',{
    method:'POST',
    headers:{'Content-Type':'application/json', 'X-API-Key': s.apiKey},
    body:JSON.stringify({ host:s.host, workflow_override:s.workflow||'', sdxl:sdxlJson, advanced })
  });
  const qo=await q.json();
  if(!q.ok){ alert(qo.error||'Queue failed.'); refreshQuota(); return; }

  CURRENT={ host:s.host, pid:qo.prompt_id, expected:Number(qo.batch||1), started:Date.now() };
  showProgress(); document.getElementById('genImages').innerHTML=''; LAST_IMAGES=[];

  if(POLL) clearInterval(POLL);
  POLL=setInterval(pollStatus,1500);
}

async function pollStatus(){
  if(!CURRENT.pid) return;
  const s=JSON.parse(localStorage.getItem(LS_SETTINGS)||'{}');
  try{
    const url=`/generate/comfy_status?host=${encodeURIComponent(CURRENT.host)}&pid=${encodeURIComponent(CURRENT.pid)}`;
    const res=await fetch(url, { headers: {'X-API-Key': s.apiKey }});
    const out=await res.json();
    if(!res.ok||out.error){ clearInterval(POLL); hideProgress(); alert(`Status error: ${out.error||'Unknown'}`); return; }
    const found=out.images||[]; const elapsed=((Date.now()-CURRENT.started)/1000).toFixed(0);
    if(found.length>=CURRENT.expected){
      clearInterval(POLL); POLL=null; hideProgress();
      LAST_IMAGES=found; const wrap=document.getElementById('genImages'); wrap.innerHTML='';
      found.forEach(url=>{const img=document.createElement('img'); img.src=url; img.alt='Generated image'; img.loading='lazy'; wrap.appendChild(img);});
      document.getElementById('zipBtn').style.display='inline-block'; document.getElementById('shareBtn').style.display='inline-block';
      
      // Charge usage
      try{
        await fetch('/usage/charge',{
          method:'POST',
          headers:{'Content-Type':'application/json', 'X-API-Key': s.apiKey},
          body:JSON.stringify({amount: CURRENT.expected})
        });
        refreshQuota();
      }catch(e){}
      
      const form=getForm(); const historyRecord={ts:Date.now(),idea:form.idea,negative:form.negative,aspect_ratio:form.aspect_ratio,lighting:form.lighting,color_grade:form.color_grade,extra_tags:form.extra_tags,steps:form.steps||'30',cfg_scale:form.cfg_scale||'6.5',sampler:form.sampler||'DPM++ 2M Karras',seed:form.seed||'random',batch:form.batch||'1',width:1024,height:1024,images:LAST_IMAGES}; addHistory(historyRecord);
    }else{
      const pct=Math.min(95,10+((found.length/CURRENT.expected)*85));
      setProgress(pct,`Generating ${found.length}/${CURRENT.expected} (${elapsed}s)`);
    }
  }catch(err){ clearInterval(POLL); hideProgress(); alert(`Poll error: ${err.message}`); }
}

function cancelGeneration(){ if(POLL){ clearInterval(POLL); POLL=null; } hideProgress(); fetch('/generate/comfy_cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt_id:CURRENT.pid})}); CURRENT={host:'',pid:'',expected:1,started:0}; }

function reRun(h){ if(!h) return; setForm({idea:h.idea||'',negative:h.negative||'',aspect_ratio:h.aspect_ratio||'16:9',lighting:h.lighting||'',color_grade:h.color_grade||'',extra_tags:h.extra_tags||'',steps:h.steps||'',cfg_scale:h.cfg_scale||'',sampler:h.sampler||'DPM++ 2M Karras',seed:h.seed||'',batch:h.batch||''}); optimize(); }

document.addEventListener('DOMContentLoaded',()=>{ loadAllPresets(); renderHistory(); refreshQuota(); });
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")

def _optimize_core(data):
    user_text = (data.get("idea") or "").strip()
    user_negative = (data.get("negative") or "").strip()
    aspect_ratio = (data.get("aspect_ratio") or "1:1").strip()
    extras = {
        "lighting": (data.get("lighting") or "").strip(),
        "color_grade": (data.get("color_grade") or "").strip(),
        "extra_tags": (data.get("extra_tags") or "").strip(),
    }
    if not user_text:
        return {"error": "Please describe your idea."}, 400

    found, subject_terms = extract_categories(user_text)
    positive = build_positive(subject_terms, found, extras)
    negative = build_negative(user_negative)

    out = {
        "unified": {"positive": positive, "negative": negative},
        "sdxl": sdxl_prompt(positive, negative, aspect_ratio),
        "comfyui": comfyui_recipe(positive, negative, aspect_ratio),
        "midjourney": midjourney_prompt(positive, aspect_ratio),
        "pika": pika_prompt(positive),
        "runway": runway_prompt(positive),
        "hints": {
            "faces": "For better faces: Add 'portrait, detailed face, sharp focus' and avoid 'bad anatomy' in negative",
            "motion": "For video: Use motion cues like 'gentle camera movement' but avoid 'warping, morphing'",
            "busy": "If output is too busy: Reduce adjectives and focus on 1-2 key elements"
        }
    }
    return out, 200

@app.route("/optimize", methods=["POST"])
def optimize():
    try:
        data = request.get_json() or {}
        result, status_code = _optimize_core(data)
        return jsonify(result), status_code
    except Exception as e:
        return jsonify({"error": f"Server error: {e}"}), 500

@app.route("/api/optimize", methods=["POST"])
def api_optimize():
    return optimize()

# Map pretty sampler names to ComfyUI short names
_SAMPLER_MAP = {
    "DPM++ 2M Karras": "dpmpp_2m", "DPM++ 2M": "dpmpp_2m",
    "DPM++ SDE Karras": "dpmpp_sde", "Euler a": "euler_ancestral"
}

@app.route("/generate/comfy_async", methods=["POST"])
@require_api_key
def generate_comfy_async():
    """
    Queues a ComfyUI job and returns prompt_id immediately for polling.
    Body: {
      "host": "http://127.0.0.1:8188",
      "workflow_override": "<optional JSON string>",
      "sdxl": {positive, negative, settings:{width,height,steps,cfg_scale,sampler}},
      "advanced": {steps,cfg_scale,sampler,seed,batch}
    }
    """
    data = request.get_json(force=True)
    host = (data.get("host") or "").rstrip("/")
    if not host:
        return jsonify({"error":"Missing ComfyUI host"}), 400

    sdxl = data.get("sdxl") or {}
    pos = sdxl.get("positive") or ""
    neg = sdxl.get("negative") or ""
    st  = sdxl.get("settings") or {}
    w = int(st.get("width", 1024)); h = int(st.get("height", 1024))
    steps_default = int(st.get("steps", 30))
    cfg_default   = float(st.get("cfg_scale", 6.5))
    sampler_name  = (st.get("sampler") or "DPM++ 2M Karras").strip()

    adv = data.get("advanced") or {}
    def _i(x, d): 
        try: return int(str(x).strip())
        except: return d
    def _f(x, d): 
        try: return float(str(x).strip())
        except: return d

    steps = _i(adv.get("steps",""), steps_default)
    cfg   = _f(adv.get("cfg_scale",""), cfg_default)
    sampler = _SAMPLER_MAP.get(adv.get("sampler", sampler_name), "dpmpp_2m")
    seed_raw = str(adv.get("seed","")).lower().strip()
    seed = random.randint(1, 2**31 - 1) if seed_raw in ("", "random", "rnd") else _i(seed_raw, random.randint(1, 2**31 - 1))
    batch = max(1, min(8, _i(adv.get("batch",""), 1)))

    wf_override_raw = data.get("workflow_override") or ""
    if wf_override_raw.strip():
        try:
            g = json.loads(wf_override_raw)
            for _, node in g.items():
                c = (node.get("class_type") or "").lower()
                if c.startswith("cliptextencode"):
                    for key in ("text","text_g","text_l"):
                        if key in node.get("inputs", {}):
                            node["inputs"][key] = pos
                if c.startswith("emptylatentimage"):
                    node["inputs"]["width"] = w
                    node["inputs"]["height"] = h
                    node["inputs"]["batch_size"] = int(batch)
                if c == "ksampler":
                    node["inputs"]["seed"] = int(seed)
                    node["inputs"]["steps"] = int(steps)
                    node["inputs"]["cfg"] = float(cfg)
                    node["inputs"]["sampler_name"] = sampler
                    node["inputs"]["scheduler"] = "karras"
            graph = g
        except Exception as e:
            return jsonify({"error": f"Invalid workflow_override JSON: {e}"}), 400
    else:
        graph = _build_default_sdxl_workflow(pos, neg, w, h, steps=steps, cfg=cfg, sampler=sampler, seed=seed, batch_size=batch)

    try:
        out = _http_post_json(f"{host}/prompt", {"prompt": graph})
        pid = out.get("prompt_id")
        if not pid:
            return jsonify({"error":"No prompt_id from ComfyUI"}), 502
        return jsonify({"ok": True, "prompt_id": pid, "seed": seed, "batch": batch, "width": w, "height": h}), 200
    except Exception as e:
        return jsonify({"error": f"Queue error: {e}"}), 502

@app.route("/generate/comfy_status", methods=["GET"])
@require_api_key
def generate_comfy_status():
    """
    Query: host=http://127.0.0.1:8188&pid=<prompt_id>
    Returns images found so far (if any).
    """
    host = (request.args.get("host") or "").rstrip("/")
    pid  = request.args.get("pid") or ""
    if not host or not pid:
        return jsonify({"error":"Missing host or pid"}), 400
    try:
        hist = _http_get_json(f"{host}/history/{pid}")
        images = []
        entry = hist.get(pid) or {}
        for _, node_out in (entry.get("outputs") or {}).items():
            if "images" in node_out:
                for img in node_out["images"]:
                    fname = img.get("filename")
                    subf  = img.get("subfolder","")
                    if fname:
                        images.append(f"{host}/view?filename={fname}&subfolder={subf}&type=output")
        return jsonify({"images": images, "done": bool(images)}), 200
    except Exception as e:
        return jsonify({"error": f"Status error: {e}"}), 502

@app.route("/generate/comfy_cancel", methods=["POST"])
def generate_comfy_cancel():
    """
    Soft-cancel: client stops polling. We return OK immediately.
    (Some ComfyUI builds support queue management via API, but it's not guaranteed.)
    Body: { "prompt_id": "..." }
    """
    return jsonify({"ok": True}), 200

def _build_default_sdxl_workflow(pos, neg, w, h, steps=30, cfg=6.5, sampler="dpmpp_2m", seed=123456789, batch_size=1):
    """
    Minimal SDXL text2img workflow_api graph. Adjust model/vae names to match your install.
    """
    return {
      "3": {"class_type": "KSampler", "inputs": {
        "seed": int(seed), "steps": int(steps), "cfg": float(cfg),
        "sampler_name": sampler, "scheduler": "karras", "denoise": 1.0,
        "model": ["21", 0], "positive": ["12", 0], "negative": ["13", 0], "latent_image": ["5", 0]
      }},
      "5": {"class_type": "EmptyLatentImage", "inputs": {"width": int(w), "height": int(h), "batch_size": int(batch_size)}},
      "7": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["22", 0]}},
      "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0]}},
      "12": {"class_type": "CLIPTextEncodeSDXL", "inputs": {"text_g": pos, "text_l": pos, "clip": ["20", 0]}},
      "13": {"class_type": "CLIPTextEncodeSDXL", "inputs": {"text_g": neg, "text_l": neg, "clip": ["20", 0]}},
      "20": {"class_type": "CLIPSetLastLayer", "inputs": {"stop_at_clip_layer": -1, "clip": ["19", 0]}},
      "21": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
      "22": {"class_type": "VAELoader", "inputs": {"vae_name": "sdxl_vae.safetensors"}},
      "19": {"class_type": "CLIPLoader", "inputs": {"clip_name": "sdxl_clip.safetensors"}}
    }

def _http_post_json(url, data):
    """Helper for POST requests with JSON."""
    req = urlreq.Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    with urlreq.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode())

def _http_get_json(url):
    """Helper for GET requests returning JSON."""
    req = urlreq.Request(url)
    with urlreq.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode())

@app.route("/generate/comfy", methods=["POST"])
@require_api_key
def generate_comfy():
    """
    Body: {
      "host": "http://127.0.0.1:8188",
      "workflow_override": "<optional workflow_api JSON string>",
      "sdxl": { "positive": "...", "negative": "...", "settings": { "width": 1344, "height": 768, "steps": 30, "cfg_scale": 6.5, "sampler": "DPM++ 2M Karras" } },
      "advanced": { "steps": "36", "cfg_scale": "6.0", "sampler": "DPM++ SDE Karras", "seed": "random", "batch": "2" }
    }
    """
    data = request.get_json(force=True)
    host = (data.get("host") or "").rstrip("/")
    if not host:
        return jsonify({"error":"Missing ComfyUI host"}), 400

    sdxl = data.get("sdxl") or {}
    pos = sdxl.get("positive") or ""
    neg = sdxl.get("negative") or ""
    st  = sdxl.get("settings") or {}
    w = int(st.get("width", 1024))
    h = int(st.get("height", 1024))
    steps_default = int(st.get("steps", 30))
    cfg_default   = float(st.get("cfg_scale", 6.5))
    sampler_name  = (st.get("sampler") or "DPM++ 2M Karras").strip()

    # Advanced overrides
    adv = data.get("advanced") or {}
    def _i(x, d): 
        try: return int(str(x).strip())
        except: return d
    def _f(x, d): 
        try: return float(str(x).strip())
        except: return d

    steps = _i(adv.get("steps",""), steps_default)
    cfg   = _f(adv.get("cfg_scale",""), cfg_default)
    sampler = _SAMPLER_MAP.get(adv.get("sampler", sampler_name), "dpmpp_2m")
    
    seed_raw = str(adv.get("seed","")).lower().strip()
    seed = random.randint(1, 2**31 - 1) if seed_raw in ("", "random", "rnd") else _i(seed_raw, random.randint(1, 2**31 - 1))
    
    batch = max(1, min(8, _i(adv.get("batch",""), 1)))

    # Custom workflow check
    wf_override_raw = data.get("workflow_override") or ""
    if wf_override_raw.strip():
        try:
            g = json.loads(wf_override_raw)
            # Basic text replacements
            for _, node in g.items():
                c = (node.get("class_type") or "").lower()
                if c.startswith("cliptextencode"):
                    for key in ("text","text_g","text_l"):
                        if key in node.get("inputs", {}):
                            node["inputs"][key] = pos
                if c.startswith("emptylatentimage"):
                    node["inputs"]["width"] = w
                    node["inputs"]["height"] = h
                    node["inputs"]["batch_size"] = int(batch)
                if c == "ksampler":
                    node["inputs"]["seed"] = int(seed)
                    node["inputs"]["steps"] = int(steps)
                    node["inputs"]["cfg"] = float(cfg)
                    node["inputs"]["sampler_name"] = sampler
                    node["inputs"]["scheduler"] = "karras"
            graph = g
        except Exception as e:
            return jsonify({"error": f"Invalid workflow_override JSON: {e}"}), 400
    else:
        graph = _build_default_sdxl_workflow(pos, neg, w, h, steps=steps, cfg=cfg, sampler=sampler, seed=seed, batch_size=batch)

    try:
        out = _http_post_json(f"{host}/prompt", {"prompt": graph})
        pid = out.get("prompt_id")
        if not pid:
            return jsonify({"error":"No prompt_id from ComfyUI"}), 502

        # Poll for results (blocking)
        start_time = time.time()
        max_wait = 120  # 2 minutes timeout
        images = []
        
        while time.time() - start_time < max_wait:
            try:
                hist = _http_get_json(f"{host}/history/{pid}")
                entry = hist.get(pid) or {}
                for _, node_out in (entry.get("outputs") or {}).items():
                    if "images" in node_out:
                        for img in node_out["images"]:
                            fname = img.get("filename")
                            subf  = img.get("subfolder","")
                            if fname:
                                images.append(f"{host}/view?filename={fname}&subfolder={subf}&type=output")
                
                if images:
                    return jsonify({
                        "images": images,
                        "seed": seed,
                        "steps": steps,
                        "cfg_scale": cfg,
                        "sampler": sampler,
                        "batch": batch,
                        "width": w,
                        "height": h
                    }), 200
                
                time.sleep(1)
            except Exception:
                time.sleep(1)
                continue
        
        return jsonify({"error": "Generation timed out. Check ComfyUI console."}), 408
    
    except Exception as e:
        return jsonify({"error": f"ComfyUI error: {e}"}), 502

@app.route("/zip", methods=["POST"])
def create_zip():
    """Create ZIP archive from image URLs"""
    try:
        data = request.get_json() or {}
        urls = data.get("urls", [])
        
        if not urls:
            return jsonify({"error": "No URLs provided"}), 400
        
        # Create in-memory ZIP
        zip_buffer = io.BytesIO()
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for i, url in enumerate(urls):
                try:
                    # Fetch image
                    req = urlreq.Request(url)
                    with urlreq.urlopen(req, timeout=10) as response:
                        image_data = response.read()
                    
                    # Add to ZIP with filename
                    filename = f"image_{i+1:02d}.png"
                    zip_file.writestr(filename, image_data)
                    
                except Exception as e:
                    # Skip failed images but continue with others
                    print(f"Failed to fetch {url}: {e}")
                    continue
        
        zip_buffer.seek(0)
        
        return Response(
            zip_buffer.getvalue(),
            mimetype='application/zip',
            headers={
                'Content-Disposition': 'attachment; filename=upo_outputs.zip',
                'Content-Length': str(len(zip_buffer.getvalue()))
            }
        )
        
    except Exception as e:
        return jsonify({"error": f"ZIP creation failed: {e}"}), 500

# Create demo key on startup
bootstrap_demo_key()
_init_share_table()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)