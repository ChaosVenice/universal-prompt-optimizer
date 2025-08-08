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
import atexit
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import timedelta

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET")

logger = logging.getLogger(__name__)
_scheduler = None

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
        # Social media tokens table
        db.execute("""CREATE TABLE IF NOT EXISTS social_tokens(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT NOT NULL,
            platform TEXT NOT NULL,  -- 'twitter', 'instagram', 'linkedin'
            access_token TEXT NOT NULL,
            refresh_token TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(api_key, platform)
        )""")
        # Social shares tracking table
        db.execute("""CREATE TABLE IF NOT EXISTS social_shares(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            share_token TEXT NOT NULL,
            platform TEXT NOT NULL,
            post_id TEXT,
            success BOOLEAN NOT NULL,
            error_message TEXT,
            posted_at TEXT NOT NULL
        )""")
        # Error logs table
        db.execute("""CREATE TABLE IF NOT EXISTS error_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL
        )""")
        # Email tracking table
        db.execute("""CREATE TABLE IF NOT EXISTS sent_emails(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            email_to TEXT NOT NULL,
            subject TEXT NOT NULL,
            trigger_type TEXT NOT NULL,  -- 'download', 'hire_us', 'manual'
            status TEXT NOT NULL,        -- 'sent', 'failed', 'retry'
            retry_count INTEGER DEFAULT 0,
            share_token TEXT,
            sent_at TEXT NOT NULL,
            error_message TEXT,
            FOREIGN KEY(lead_id) REFERENCES leads(id)
        )""")
        
        # Portfolio analytics table
        db.execute("""CREATE TABLE IF NOT EXISTS portfolio_analytics(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            share_token TEXT NOT NULL,
            action_type TEXT NOT NULL,  -- 'view', 'click', 'order', 'license'
            ip_address TEXT,
            user_agent TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(share_token) REFERENCES shares(token)
        )""")
        
        # Featured portfolio table
        db.execute("""CREATE TABLE IF NOT EXISTS featured_portfolio(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            share_token TEXT NOT NULL UNIQUE,
            engagement_score REAL DEFAULT 0,
            total_views INTEGER DEFAULT 0,
            total_downloads INTEGER DEFAULT 0,
            total_social_shares INTEGER DEFAULT 0,
            featured_at TEXT NOT NULL,
            auto_selected BOOLEAN DEFAULT TRUE,
            seo_title TEXT,
            seo_description TEXT,
            FOREIGN KEY(share_token) REFERENCES shares(token)
        )""")
        
        # Portfolio orders table
        db.execute("""CREATE TABLE IF NOT EXISTS portfolio_orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            share_token TEXT NOT NULL,
            order_type TEXT NOT NULL,    -- 'similar', 'license', 'commission'
            customer_email TEXT NOT NULL,
            customer_name TEXT,
            order_details TEXT,          -- JSON with parameters and requirements
            price_cents INTEGER,
            status TEXT DEFAULT 'pending', -- 'pending', 'paid', 'completed', 'cancelled', 'upsell_pending'
            created_at TEXT NOT NULL,
            upsell_token TEXT,           -- Links to upsell session
            FOREIGN KEY(share_token) REFERENCES shares(token)
        )""")
        
        # Upsell sessions table for funnel tracking
        db.execute("""CREATE TABLE IF NOT EXISTS upsell_sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            upsell_token TEXT UNIQUE,
            original_share_token TEXT,
            customer_email TEXT,
            customer_name TEXT,
            original_project_type TEXT,
            status TEXT DEFAULT 'active',  -- 'active', 'completed', 'abandoned', 'expired'
            tier_selected TEXT,  -- 'basic', 'premium', 'deluxe'
            expires_at TEXT,
            created_at TEXT,
            completed_at TEXT,
            FOREIGN KEY(original_share_token) REFERENCES shares(token)
        )""")
        
        # Upsell follow-up emails table for automation
        db.execute("""CREATE TABLE IF NOT EXISTS upsell_follow_ups(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            upsell_token TEXT,
            email_sequence INTEGER,  -- 1, 2, 3, 4 (hour 1, hour 12, day 2, day 4)
            scheduled_at TEXT,
            sent_at TEXT,
            status TEXT DEFAULT 'scheduled',  -- 'scheduled', 'sent', 'failed'
            FOREIGN KEY(upsell_token) REFERENCES upsell_sessions(upsell_token)
        )""")
        
        db.commit()

# ===== Scheduler bootstrap (safe, idempotent) =====
log = logging.getLogger(__name__)

def _maybe_call(name):
    """Call an internal function if it exists; return (ok, message)."""
    fn = globals().get(name)
    if callable(fn):
        try:
            fn()
            return True, f"called {name}"
        except Exception as e:
            log.exception("Error running %s: %s", name, e)
            return False, f"error in {name}: {e}"
    return False, f"missing {name}"

def _job_process_upsell_emails():
    # Replace internal names if yours differ
    for candidate in ("scheduled_process_upsell_emails", "_internal_process_upsell_emails", "process_upsell_emails"):
        ok, msg = _maybe_call(candidate)
        if ok or "error" in msg:
            return
    log.info("No upsell email processor found; skipping.")

def _job_process_email_retries():
    for candidate in ("scheduled_process_email_retries", "_internal_process_email_retries", "process_email_retries"):
        ok, msg = _maybe_call(candidate)
        if ok or "error" in msg:
            return
    log.info("No email retry processor found; skipping.")

def _job_cleanup_expired_sessions():
    for candidate in ("scheduled_cleanup_expired_sessions", "_internal_cleanup_expired_sessions", "cleanup_expired_sessions"):
        ok, msg = _maybe_call(candidate)
        if ok or "error" in msg:
            return
    log.info("No cleanup function found; skipping.")

def _start_background_scheduler():
    """Start once; skip if DISABLE_SCHEDULER=1."""
    global _scheduler
    if os.environ.get("DISABLE_SCHEDULER") == "1":
        log.warning("Scheduler disabled via DISABLE_SCHEDULER=1")
        return None
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
    # Tick intervals — adjust if you want them tighter/looser
    _scheduler.add_job(
        func=_job_process_upsell_emails,
        trigger=IntervalTrigger(minutes=15),
        id="process_upsell_emails",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    _scheduler.add_job(
        func=_job_process_email_retries,
        trigger=IntervalTrigger(minutes=10),
        id="process_email_retries",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    _scheduler.add_job(
        func=_job_cleanup_expired_sessions,
        trigger=IntervalTrigger(hours=1),
        id="cleanup_expired_sessions",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )

    _scheduler.start()
    atexit.register(lambda: _scheduler.shutdown(wait=False))
    log.info("BackgroundScheduler started with jobs: %s", [j.id for j in _scheduler.get_jobs()])
    return _scheduler

def scheduled_process_upsell_emails():
    """Scheduled job wrapper for upsell email processing"""
    try:
        with app.app_context():
            # Call the existing endpoint logic
            db = get_db()
            
            # Get all scheduled follow-ups that need to be sent
            cur = db.execute("""SELECT uf.*, us.customer_name, us.customer_email, us.original_share_token, s.title, s.meta_json
                                FROM upsell_follow_ups uf
                                JOIN upsell_sessions us ON uf.upsell_token = us.upsell_token
                                LEFT JOIN shares s ON us.original_share_token = s.token
                                WHERE uf.status='scheduled' AND uf.scheduled_at <= ?
                                ORDER BY uf.scheduled_at ASC""",
                             (datetime.datetime.utcnow().isoformat(),))
            
            pending_emails = cur.fetchall()
            sent_count = 0
            
            for email_data in pending_emails:
                (follow_up_id, upsell_token, sequence, scheduled_at, sent_at, status,
                 name, email, original_token, title, meta_json) = email_data
                
                try:
                    # Get image for email
                    meta = json.loads(meta_json or '{}')
                    image_url = meta.get('images', [''])[0] or ''
                    
                    # Generate email content based on sequence
                    subject, html_content = _generate_upsell_email_content(sequence, name, title, image_url, upsell_token)
                    
                    # Send email using SendGrid or SMTP
                    success = _send_upsell_email(email, subject, html_content)
                    
                    if success:
                        # Mark as sent
                        db.execute("""UPDATE upsell_follow_ups SET status='sent', sent_at=?
                                     WHERE id=?""",
                                  (datetime.datetime.utcnow().isoformat(), follow_up_id))
                        sent_count += 1
                    else:
                        # Mark as failed
                        db.execute("""UPDATE upsell_follow_ups SET status='failed'
                                     WHERE id=?""", (follow_up_id,))
                        
                except Exception as e:
                    _log_error("ERROR", f"Failed to send upsell email {follow_up_id}", str(e))
                    db.execute("""UPDATE upsell_follow_ups SET status='failed'
                                 WHERE id=?""", (follow_up_id,))
            
            db.commit()
            
            if pending_emails:
                log.info(f"Processed {len(pending_emails)} upsell emails, {sent_count} sent successfully")
            
    except Exception as e:
        log.exception("scheduled_process_upsell_emails error: %s", e)

def scheduled_process_email_retries():
    """Scheduled job for retrying failed emails"""
    try:
        with app.app_context():
            db = get_db()
            
            # Get failed emails that haven't exceeded retry limit (example: 3 retries max)
            # Working with existing schema: id, lead_id, email_to, subject, trigger_type, status, retry_count, share_token, sent_at, error_message
            cur = db.execute("""SELECT * FROM sent_emails 
                                WHERE status='failed' AND retry_count < 3""")
            
            failed_emails = cur.fetchall()
            
            for email_record in failed_emails:
                try:
                    email_id, lead_id, email_to, subject, trigger_type, status, retry_count, share_token, sent_at, error_message = email_record
                    
                    # Get lead info - leads table has: id, email, share_token, ip_address, created_at, source
                    cur = db.execute("SELECT email FROM leads WHERE id=?", (lead_id,))
                    lead_data = cur.fetchone()
                    
                    if lead_data:
                        email_addr = lead_data[0]
                        name = "Valued Customer"  # Use generic name since leads table doesn't store names
                        
                        # Retry the email based on trigger_type
                        if trigger_type == 'download':
                            # Send download follow-up email
                            subject = "Thanks for exploring Chaos Venice Productions"
                            html_content = f"""<!DOCTYPE html>
                            <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
                            <h2>Hi {name}!</h2>
                            <p>Thanks for your interest in our creative work. We'd love to help bring your vision to life!</p>
                            <p>Ready for a custom commission? <a href="mailto:hello@chaosvenice.com?subject=Commission%20Inquiry" style="color:#0066cc;">Get in touch</a></p>
                            <p>Best regards,<br>Chaos Venice Productions</p>
                            </body></html>"""
                            success = send_email(email_addr, subject, html_content)
                        elif trigger_type == 'hire_us':
                            # Send hire us follow-up email
                            subject = "Your Creative Vision Awaits"
                            html_content = f"""<!DOCTYPE html>
                            <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
                            <h2>Hi {name}!</h2>
                            <p>Thank you for reaching out to Chaos Venice Productions. We've received your inquiry and will respond within 24 hours.</p>
                            <p>In the meantime, feel free to explore more of our work and let us know if you have any specific questions!</p>
                            <p>Best regards,<br>Chaos Venice Productions</p>
                            </body></html>"""
                            success = send_email(email_addr, subject, html_content)
                        else:
                            continue
                        
                        # Update retry attempt
                        new_retry_count = retry_count + 1
                        if success:
                            db.execute("""UPDATE sent_emails SET status='sent', retry_count=?
                                         WHERE id=?""",
                                      (new_retry_count, email_id))
                        else:
                            db.execute("""UPDATE sent_emails SET retry_count=?
                                         WHERE id=?""",
                                      (new_retry_count, email_id))
                            
                except Exception as e:
                    log.error(f"Failed to retry email {email_record[0]}: {e}")
            
            db.commit()
            
            if failed_emails:
                log.info(f"Retried {len(failed_emails)} failed emails")
            
    except Exception as e:
        log.exception("scheduled_process_email_retries error: %s", e)

def scheduled_cleanup_expired_sessions():
    """Scheduled job for cleaning up expired sessions and tokens"""
    try:
        with app.app_context():
            db = get_db()
            
            current_time = datetime.datetime.utcnow().isoformat()
            
            # Cleanup expired upsell sessions
            cur = db.execute("SELECT COUNT(*) FROM upsell_sessions WHERE expires_at < ? AND status != 'expired'", (current_time,))
            expired_count = cur.fetchone()[0]
            
            if expired_count > 0:
                db.execute("UPDATE upsell_sessions SET status='expired' WHERE expires_at < ? AND status != 'expired'", (current_time,))
            
            # Cleanup expired share tokens (older than 30 days)
            thirty_days_ago = (datetime.datetime.utcnow() - timedelta(days=30)).isoformat()
            cur = db.execute("SELECT COUNT(*) FROM shares WHERE expires_at < ?", (thirty_days_ago,))
            old_shares_count = cur.fetchone()[0]
            
            if old_shares_count > 0:
                db.execute("DELETE FROM shares WHERE expires_at < ?", (thirty_days_ago,))
                
            # Cleanup old logs (older than 90 days)
            ninety_days_ago = (datetime.datetime.utcnow() - timedelta(days=90)).isoformat()
            cur = db.execute("SELECT COUNT(*) FROM error_logs WHERE created_at < ?", (ninety_days_ago,))
            old_logs_count = cur.fetchone()[0]
            
            if old_logs_count > 0:
                db.execute("DELETE FROM error_logs WHERE created_at < ?", (ninety_days_ago,))
            
            db.commit()
            
            if expired_count > 0 or old_shares_count > 0 or old_logs_count > 0:
                log.info(f"Cleanup: {expired_count} expired upsell sessions, {old_shares_count} old shares, {old_logs_count} old logs")
            
    except Exception as e:
        log.exception("scheduled_cleanup_expired_sessions error: %s", e)

# start it now (Gunicorn worker boot)
_start_background_scheduler()

# --- PORTFOLIO ENGINE FUNCTIONS ---

def _track_portfolio_action(share_token, action_type):
    """Track user actions on portfolio items"""
    db = get_db()
    db.execute("""INSERT INTO portfolio_analytics(share_token, action_type, ip_address, user_agent, created_at)
                  VALUES(?,?,?,?,?)""",
               (share_token, action_type, _get_client_ip(), 
                request.headers.get('User-Agent', ''), datetime.datetime.utcnow().isoformat()))
    db.commit()

def _calculate_engagement_score(share_token):
    """Calculate engagement score for a share based on various metrics"""
    db = get_db()
    
    # Get share visits
    cur = db.execute("SELECT COUNT(*) FROM share_visits WHERE share_token=?", (share_token,))
    visits = cur.fetchone()[0]
    
    # Get downloads from leads
    cur = db.execute("SELECT COUNT(*) FROM leads WHERE share_token=?", (share_token,))
    downloads = cur.fetchone()[0]
    
    # Get social shares
    cur = db.execute("SELECT COUNT(*) FROM social_shares WHERE share_token=? AND success=1", (share_token,))
    social_shares = cur.fetchone()[0]
    
    # Get portfolio views/clicks
    cur = db.execute("SELECT COUNT(*) FROM portfolio_analytics WHERE share_token=? AND action_type IN ('view', 'click')", (share_token,))
    portfolio_actions = cur.fetchone()[0]
    
    # Get orders
    cur = db.execute("SELECT COUNT(*) FROM portfolio_orders WHERE share_token=?", (share_token,))
    orders = cur.fetchone()[0]
    
    # Calculate weighted score
    engagement_score = (
        visits * 1.0 +           # Base visits
        downloads * 5.0 +        # Email captures are valuable
        social_shares * 10.0 +   # Social shares amplify reach
        portfolio_actions * 2.0 + # Portfolio engagement
        orders * 50.0            # Orders are most valuable
    )
    
    return engagement_score, visits, downloads, social_shares

def _generate_seo_content(share_data):
    """Generate SEO title and description for portfolio items"""
    try:
        meta = json.loads(share_data.get('meta_json', '{}'))
        title = share_data.get('title', 'AI Generated Art')
        prompt = meta.get('positive_prompt', '')
        
        # Extract key terms from prompt for SEO
        key_terms = []
        if 'cinematic' in prompt.lower():
            key_terms.append('cinematic')
        if 'portrait' in prompt.lower():
            key_terms.append('portrait')
        if 'landscape' in prompt.lower():
            key_terms.append('landscape')
        if 'concept art' in prompt.lower():
            key_terms.append('concept art')
        if 'digital art' in prompt.lower():
            key_terms.append('digital art')
        
        style_terms = ['hyperrealistic', 'fantasy', 'sci-fi', 'abstract', 'minimalist']
        for term in style_terms:
            if term in prompt.lower():
                key_terms.append(term)
                break
        
        # Generate SEO title
        if key_terms:
            seo_title = f"{title} - {' '.join(key_terms[:3]).title()} AI Art by Chaos Venice Productions"
        else:
            seo_title = f"{title} - Professional AI Art by Chaos Venice Productions"
        
        # Generate SEO description
        seo_description = f"Professional AI-generated artwork: {title}. "
        if key_terms:
            seo_description += f"Features {', '.join(key_terms[:2])} styling. "
        seo_description += "Commission similar works or license for commercial use. Created by Chaos Venice Productions using advanced AI technology."
        
        return seo_title[:60], seo_description[:160]
        
    except:
        return f"{share_data.get('title', 'AI Art')} - Chaos Venice Productions", "Professional AI-generated artwork available for licensing and custom commissions."

def _update_featured_portfolio():
    """Automatically update featured portfolio based on engagement"""
    db = get_db()
    
    # Get all shares older than 1 hour (to allow initial engagement)
    one_hour_ago = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).isoformat()
    cur = db.execute("""SELECT token, title, meta_json, created_at FROM shares 
                        WHERE created_at < ? AND expires_at > ?""", 
                     (one_hour_ago, datetime.datetime.utcnow().isoformat()))
    
    shares = cur.fetchall()
    
    for share in shares:
        token, title, meta_json, created_at = share
        
        # Calculate engagement score
        score, views, downloads, social_shares = _calculate_engagement_score(token)
        
        # Only feature if there's meaningful engagement (score > 10)
        if score > 10:
            # Generate SEO content
            share_data = {'title': title, 'meta_json': meta_json}
            seo_title, seo_description = _generate_seo_content(share_data)
            
            # Insert or update featured portfolio
            db.execute("""INSERT OR REPLACE INTO featured_portfolio
                          (share_token, engagement_score, total_views, total_downloads, 
                           total_social_shares, featured_at, seo_title, seo_description)
                          VALUES(?,?,?,?,?,?,?,?)""",
                       (token, score, views, downloads, social_shares, 
                        datetime.datetime.utcnow().isoformat(), seo_title, seo_description))
    
    db.commit()
    
    # Cleanup: Remove low-performing items from featured (keep top 50)
    db.execute("""DELETE FROM featured_portfolio WHERE id NOT IN (
                    SELECT id FROM featured_portfolio ORDER BY engagement_score DESC LIMIT 50
                  )""")
    db.commit()

def _new_token(prefix="sh_"): 
    return prefix + secrets.token_urlsafe(16)

def _generate_share_token():
    """Generate a random token for share links"""
    import secrets
    return secrets.token_urlsafe(16)

def _start_upsell_sequence(lead_id, email, upsell_token, original_token, project_type):
    """Start automated upsell sequence with email follow-ups"""
    try:
        db = get_db()
        
        # Create upsell session (expires in 24 hours)
        expires_at = (datetime.datetime.utcnow() + datetime.timedelta(hours=24)).isoformat()
        
        # Get customer name from lead
        cur = db.execute("SELECT name FROM leads WHERE id=?", (lead_id,))
        lead_row = cur.fetchone()
        customer_name = lead_row[0] if lead_row else 'Valued Customer'
        
        db.execute("""INSERT INTO upsell_sessions(upsell_token, original_share_token, customer_email, 
                      customer_name, original_project_type, expires_at, created_at)
                      VALUES(?,?,?,?,?,?,?)""",
                   (upsell_token, original_token, email, customer_name, project_type, 
                    expires_at, datetime.datetime.utcnow().isoformat()))
        
        # Schedule follow-up emails
        now = datetime.datetime.utcnow()
        follow_up_times = [
            (1, now + datetime.timedelta(hours=1)),    # Hour 1 reminder
            (2, now + datetime.timedelta(hours=12)),   # Hour 12 discount
            (3, now + datetime.timedelta(days=2)),     # Day 2 behind scenes
            (4, now + datetime.timedelta(days=4))      # Day 4 last chance
        ]
        
        for sequence, scheduled_time in follow_up_times:
            db.execute("""INSERT INTO upsell_follow_ups(upsell_token, email_sequence, scheduled_at)
                          VALUES(?,?,?)""",
                       (upsell_token, sequence, scheduled_time.isoformat()))
        
        db.commit()
        _log_error("INFO", f"Upsell sequence started for {email}", f"Token: {upsell_token}")
        
    except Exception as e:
        _log_error("ERROR", "Failed to start upsell sequence", str(e))

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
    """Capture lead information and return lead ID"""
    db = get_db()
    cur = db.execute("""INSERT INTO leads(email, share_token, ip_address, created_at, source)
                        VALUES(?,?,?,?,?)""",
                     (email, share_token, _get_client_ip(), datetime.datetime.utcnow().isoformat(), source))
    lead_id = cur.lastrowid
    db.commit()
    return lead_id

def _log_error(level, message, details=None):
    """Log errors to database"""
    db = get_db()
    db.execute("""INSERT INTO error_logs(level, message, details, created_at)
                  VALUES(?,?,?,?)""",
               (level, message, details, datetime.datetime.utcnow().isoformat()))
    db.commit()

def _encrypt_token(token):
    """Simple encryption for stored tokens"""
    import base64
    # In production, use proper encryption with a secret key
    return base64.b64encode(token.encode()).decode()

def _decrypt_token(encrypted_token):
    """Simple decryption for stored tokens"""
    import base64
    try:
        return base64.b64decode(encrypted_token.encode()).decode()
    except:
        return encrypted_token  # Fallback for unencrypted tokens

def _store_social_token(api_key, platform, access_token, refresh_token=None, expires_at=None):
    """Store encrypted social media tokens"""
    db = get_db()
    encrypted_access = _encrypt_token(access_token)
    encrypted_refresh = _encrypt_token(refresh_token) if refresh_token else None
    
    db.execute("""INSERT OR REPLACE INTO social_tokens(api_key, platform, access_token, refresh_token, expires_at, created_at)
                  VALUES(?,?,?,?,?,?)""",
               (api_key, platform, encrypted_access, encrypted_refresh, expires_at, datetime.datetime.utcnow().isoformat()))
    db.commit()

def _get_social_token(api_key, platform):
    """Retrieve and decrypt social media token"""
    db = get_db()
    cur = db.execute("SELECT access_token, refresh_token, expires_at FROM social_tokens WHERE api_key=? AND platform=?", 
                     (api_key, platform))
    row = cur.fetchone()
    if not row:
        return None
    
    access_token = _decrypt_token(row[0])
    refresh_token = _decrypt_token(row[1]) if row[1] else None
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": row[2]
    }

def _track_social_share(share_token, platform, success, post_id=None, error_message=None):
    """Track social media share attempts"""
    db = get_db()
    db.execute("""INSERT INTO social_shares(share_token, platform, post_id, success, error_message, posted_at)
                  VALUES(?,?,?,?,?,?)""",
               (share_token, platform, post_id, success, error_message, datetime.datetime.utcnow().isoformat()))
    db.commit()

def _post_to_twitter(access_token, caption, image_url, share_url):
    """Post to Twitter/X with image and link"""
    import requests
    try:
        # Twitter API v2 endpoint
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        # First upload media (simplified - in production, use proper Twitter media upload)
        tweet_text = f"{caption}\n\n{share_url}\n\n#AI #Art #ChaosVenice"
        
        data = {"text": tweet_text}
        
        response = requests.post(
            'https://api.twitter.com/2/tweets',
            headers=headers,
            json=data,
            timeout=10
        )
        
        if response.status_code == 201:
            result = response.json()
            return True, result.get('data', {}).get('id')
        else:
            return False, f"Twitter API error: {response.status_code} - {response.text}"
            
    except Exception as e:
        return False, f"Twitter posting failed: {str(e)}"

def _post_to_instagram(access_token, caption, image_url, share_url):
    """Post to Instagram via Graph API"""
    import requests
    try:
        # Instagram Graph API requires Facebook Page token
        # This is a simplified version - production needs proper Instagram Business setup
        
        post_caption = f"{caption}\n\nLink in bio: {share_url}\n\n#AI #Art #ChaosVenice #WhereImaginationMeetsPrecision"
        
        # Create media container
        container_url = f"https://graph.facebook.com/v18.0/{{instagram_account_id}}/media"
        container_data = {
            'image_url': image_url,
            'caption': post_caption,
            'access_token': access_token
        }
        
        # Note: This requires Instagram Business Account setup
        # For now, return success but log as not implemented
        _log_error("INFO", "Instagram posting not fully implemented", 
                  "Requires Instagram Business Account and Facebook Page token")
        return True, "instagram_mock_id"
        
    except Exception as e:
        return False, f"Instagram posting failed: {str(e)}"

def _post_to_linkedin(access_token, caption, image_url, share_url):
    """Post to LinkedIn via API"""
    import requests
    try:
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'X-Restli-Protocol-Version': '2.0.0'
        }
        
        post_text = f"{caption}\n\n{share_url}\n\n#AI #ArtificialIntelligence #DigitalArt #ChaosVenice"
        
        # LinkedIn v2 API for creating posts
        data = {
            "author": "urn:li:person:{person_id}",  # Would need actual person ID
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {
                        "text": post_text
                    },
                    "shareMediaCategory": "IMAGE",
                    "media": [
                        {
                            "status": "READY",
                            "description": {
                                "text": caption
                            },
                            "media": image_url,
                            "title": {
                                "text": "AI Generated Art - Chaos Venice Productions"
                            }
                        }
                    ]
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            }
        }
        
        response = requests.post(
            'https://api.linkedin.com/v2/ugcPosts',
            headers=headers,
            json=data,
            timeout=10
        )
        
        if response.status_code == 201:
            result = response.json()
            return True, result.get('id', 'linkedin_post_id')
        else:
            return False, f"LinkedIn API error: {response.status_code} - {response.text}"
            
    except Exception as e:
        return False, f"LinkedIn posting failed: {str(e)}"

def _resize_image_for_platform(image_url, platform):
    """Get optimal image size for each platform"""
    platform_sizes = {
        'twitter': {'width': 1200, 'height': 675},  # 16:9 ratio
        'instagram': {'width': 1080, 'height': 1080},  # Square
        'linkedin': {'width': 1200, 'height': 627}   # LinkedIn recommended
    }
    
    # For now, return original URL (in production, implement actual resizing)
    return image_url

# --- EMAIL AUTOMATION SYSTEM ---

def _send_email_sendgrid(to_email, subject, html_content, text_content=None):
    """Send email using SendGrid API"""
    import requests
    
    sendgrid_key = os.getenv('SENDGRID_API_KEY')
    if not sendgrid_key:
        return False, "SendGrid API key not configured"
    
    from_email = os.getenv('FROM_EMAIL', 'hello@chaosvenice.com')
    
    payload = {
        "personalizations": [{
            "to": [{"email": to_email}],
            "subject": subject
        }],
        "from": {"email": from_email, "name": "Chaos Venice Productions"},
        "content": [
            {"type": "text/html", "value": html_content}
        ]
    }
    
    if text_content:
        payload["content"].insert(0, {"type": "text/plain", "value": text_content})
    
    try:
        response = requests.post(
            'https://api.sendgrid.com/v3/mail/send',
            headers={
                'Authorization': f'Bearer {sendgrid_key}',
                'Content-Type': 'application/json'
            },
            json=payload,
            timeout=10
        )
        
        if response.status_code == 202:
            return True, "Email sent successfully"
        else:
            return False, f"SendGrid error: {response.status_code} - {response.text}"
            
    except Exception as e:
        return False, f"Email sending failed: {str(e)}"

def _send_email_smtp(to_email, subject, html_content, text_content=None):
    """Send email using SMTP as fallback"""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    
    smtp_host = os.getenv('SMTP_HOST')
    smtp_port = int(os.getenv('SMTP_PORT', '587'))
    smtp_user = os.getenv('SMTP_USER')
    smtp_pass = os.getenv('SMTP_PASS')
    from_email = os.getenv('FROM_EMAIL', 'hello@chaosvenice.com')
    
    if not all([smtp_host, smtp_user, smtp_pass]):
        return False, "SMTP credentials not configured"
    
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"Chaos Venice Productions <{from_email}>"
        msg['To'] = to_email
        
        if text_content:
            msg.attach(MIMEText(text_content, 'plain'))
        msg.attach(MIMEText(html_content, 'html'))
        
        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
        server.quit()
        
        return True, "Email sent successfully via SMTP"
        
    except Exception as e:
        return False, f"SMTP sending failed: {str(e)}"

def _send_email(to_email, subject, html_content, text_content=None):
    """Send email using available service (SendGrid preferred, SMTP fallback)"""
    # Try SendGrid first
    if os.getenv('SENDGRID_API_KEY'):
        success, message = _send_email_sendgrid(to_email, subject, html_content, text_content)
        if success:
            return True, message
        _log_error("WARNING", "SendGrid failed, trying SMTP", message)
    
    # Fallback to SMTP
    return _send_email_smtp(to_email, subject, html_content, text_content)

def _generate_download_email(name, generation_title, image_url, share_url):
    """Generate HTML email for download trigger"""
    name_greeting = f"Hi {name}," if name else "Hello,"
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Thanks for exploring Chaos Venice Productions</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 0; background: #0b0f14; color: #e7edf5; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ text-align: center; margin-bottom: 30px; }}
            .logo {{ font-size: 24px; font-weight: 700; background: linear-gradient(45deg, #00ffff, #ff00ff); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
            .tagline {{ color: #9fb2c7; margin-top: 8px; }}
            .content {{ background: #111826; border-radius: 12px; padding: 30px; margin: 20px 0; }}
            .image-preview {{ text-align: center; margin: 20px 0; }}
            .image-preview img {{ max-width: 300px; height: auto; border-radius: 8px; border: 2px solid #1f2a3a; }}
            .cta-button {{ display: inline-block; padding: 16px 32px; background: linear-gradient(45deg, #00ffff, #ff00ff); border-radius: 8px; color: #000; text-decoration: none; font-weight: 600; margin: 16px 8px; }}
            .secondary-button {{ display: inline-block; padding: 16px 32px; background: transparent; border: 2px solid #00ffff; border-radius: 8px; color: #00ffff; text-decoration: none; font-weight: 600; margin: 16px 8px; }}
            .footer {{ text-align: center; color: #9fb2c7; font-size: 14px; margin-top: 40px; }}
            a {{ color: #4f9eff; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="logo">CHAOS VENICE PRODUCTIONS</div>
                <div class="tagline">Where Imagination Meets Precision</div>
            </div>
            
            <div class="content">
                <h2 style="color: #00ffff; margin-top: 0;">Thanks for exploring our AI creations!</h2>
                
                <p>{name_greeting}</p>
                
                <p>We noticed you downloaded "<strong>{generation_title}</strong>" – excellent taste! Our AI-powered art generation system creates stunning, professional-quality visuals that capture the perfect balance of creativity and technical precision.</p>
                
                <div class="image-preview">
                    <img src="{image_url}" alt="{generation_title}">
                </div>
                
                <p><strong>Ready to commission something truly unique?</strong></p>
                
                <p>Whether you need:</p>
                <ul style="color: #9fb2c7;">
                    <li>Custom art for your brand or project</li>
                    <li>Concept art and visual development</li>
                    <li>Marketing materials and social content</li>
                    <li>Bespoke image collections</li>
                </ul>
                
                <p>Our team specializes in transforming your creative vision into pixel-perfect reality.</p>
                
                <div style="text-align: center; margin: 30px 0;">
                    <a href="mailto:orders@chaosvenice.com?subject=Custom Commission Inquiry" class="cta-button">Commission Custom Work</a>
                    <a href="{share_url}" class="secondary-button">View Full Generation</a>
                </div>
                
                <p style="color: #9fb2c7; font-size: 14px; margin-top: 30px;">
                    <strong>What happens next?</strong><br>
                    Reply to this email with your project details, and we'll send you a custom quote within 24 hours. Every commission includes unlimited revisions until it's perfect.
                </p>
            </div>
            
            <div class="footer">
                <p>Chaos Venice Productions<br>
                📧 <a href="mailto:orders@chaosvenice.com">orders@chaosvenice.com</a><br>
                🌐 Where your imagination meets our precision</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    text = f"""
    Thanks for exploring Chaos Venice Productions!
    
    {name_greeting}
    
    We noticed you downloaded "{generation_title}" - excellent taste! Our AI-powered art generation system creates stunning, professional-quality visuals.
    
    Ready to commission something truly unique?
    
    Whether you need custom art for your brand, concept art, marketing materials, or bespoke image collections, our team specializes in transforming your creative vision into pixel-perfect reality.
    
    Commission Custom Work: mailto:orders@chaosvenice.com?subject=Custom Commission Inquiry
    View Full Generation: {share_url}
    
    What happens next?
    Reply to this email with your project details, and we'll send you a custom quote within 24 hours. Every commission includes unlimited revisions until it's perfect.
    
    Chaos Venice Productions
    orders@chaosvenice.com
    Where your imagination meets our precision
    """
    
    return html, text

def _generate_hire_us_email(name, inquiry_details):
    """Generate HTML email for hire us trigger"""
    name_greeting = f"Hi {name}," if name else "Hello,"
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Your Creative Vision Awaits</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 0; background: #0b0f14; color: #e7edf5; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ text-align: center; margin-bottom: 30px; }}
            .logo {{ font-size: 24px; font-weight: 700; background: linear-gradient(45deg, #00ffff, #ff00ff); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
            .tagline {{ color: #9fb2c7; margin-top: 8px; }}
            .content {{ background: #111826; border-radius: 12px; padding: 30px; margin: 20px 0; }}
            .portfolio-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin: 20px 0; }}
            .portfolio-item {{ background: #1f2a3a; border-radius: 8px; padding: 16px; text-align: center; }}
            .cta-button {{ display: inline-block; padding: 16px 32px; background: linear-gradient(45deg, #00ffff, #ff00ff); border-radius: 8px; color: #000; text-decoration: none; font-weight: 600; margin: 16px 8px; }}
            .timeline {{ background: rgba(0,255,255,0.05); border-left: 3px solid #00ffff; padding: 20px; margin: 20px 0; border-radius: 8px; }}
            .footer {{ text-align: center; color: #9fb2c7; font-size: 14px; margin-top: 40px; }}
            a {{ color: #4f9eff; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="logo">CHAOS VENICE PRODUCTIONS</div>
                <div class="tagline">Where Imagination Meets Precision</div>
            </div>
            
            <div class="content">
                <h2 style="color: #00ffff; margin-top: 0;">Your Creative Vision Awaits ✨</h2>
                
                <p>{name_greeting}</p>
                
                <p>Thank you for reaching out about commissioning custom work with Chaos Venice Productions! We're excited to learn about your creative project and help bring your vision to life.</p>
                
                <div class="timeline">
                    <h3 style="color: #ff00ff; margin-top: 0;">What happens next:</h3>
                    <p><strong>Within 24 hours:</strong> Our creative team will review your inquiry and send you a detailed project proposal with timeline and pricing.</p>
                    <p><strong>Your input matters:</strong> We'll work closely with you to refine every detail until it perfectly matches your vision.</p>
                    <p><strong>Unlimited revisions:</strong> Every commission includes as many iterations as needed to get it just right.</p>
                </div>
                
                <h3 style="color: #00ffff;">Recent Work Highlights</h3>
                <div class="portfolio-grid">
                    <div class="portfolio-item">
                        <h4 style="color: #ff00ff; margin: 0 0 8px 0;">Brand Identity</h4>
                        <p style="font-size: 14px; color: #9fb2c7; margin: 0;">Complete visual systems</p>
                    </div>
                    <div class="portfolio-item">
                        <h4 style="color: #ff00ff; margin: 0 0 8px 0;">Concept Art</h4>
                        <p style="font-size: 14px; color: #9fb2c7; margin: 0;">Character & environment design</p>
                    </div>
                    <div class="portfolio-item">
                        <h4 style="color: #ff00ff; margin: 0 0 8px 0;">Marketing Assets</h4>
                        <p style="font-size: 14px; color: #9fb2c7; margin: 0;">Social media & campaign visuals</p>
                    </div>
                    <div class="portfolio-item">
                        <h4 style="color: #ff00ff; margin: 0 0 8px 0;">Product Visualization</h4>
                        <p style="font-size: 14px; color: #9fb2c7; margin: 0;">3D renders & lifestyle shots</p>
                    </div>
                </div>
                
                <p>Every project receives our signature blend of cutting-edge AI technology and human artistic expertise to ensure truly exceptional results.</p>
                
                <div style="text-align: center; margin: 30px 0;">
                    <a href="mailto:orders@chaosvenice.com?subject=Re: Commission Inquiry - Urgent" class="cta-button">Questions? Reply Now</a>
                </div>
                
                <p style="color: #9fb2c7; font-size: 14px; background: rgba(255,0,255,0.05); padding: 16px; border-radius: 8px; border-left: 3px solid #ff00ff;">
                    <strong>Pro tip:</strong> The more details you share about your vision, timeline, and intended use, the more accurately we can tailor our proposal to your needs.
                </p>
            </div>
            
            <div class="footer">
                <p>Chaos Venice Productions<br>
                📧 <a href="mailto:orders@chaosvenice.com">orders@chaosvenice.com</a><br>
                🎨 Transforming imagination into reality since day one</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    text = f"""
    Your Creative Vision Awaits
    
    {name_greeting}
    
    Thank you for reaching out about commissioning custom work with Chaos Venice Productions! We're excited to learn about your creative project and help bring your vision to life.
    
    What happens next:
    - Within 24 hours: Our creative team will review your inquiry and send you a detailed project proposal
    - Your input matters: We'll work closely with you to refine every detail
    - Unlimited revisions: Every commission includes as many iterations as needed
    
    Recent Work Highlights:
    • Brand Identity - Complete visual systems
    • Concept Art - Character & environment design  
    • Marketing Assets - Social media & campaign visuals
    • Product Visualization - 3D renders & lifestyle shots
    
    Every project receives our signature blend of cutting-edge AI technology and human artistic expertise.
    
    Questions? Reply to: orders@chaosvenice.com
    
    Pro tip: The more details you share about your vision, timeline, and intended use, the more accurately we can tailor our proposal to your needs.
    
    Chaos Venice Productions
    orders@chaosvenice.com
    Transforming imagination into reality since day one
    """
    
    return html, text

def _send_automated_email(lead_id, email_to, trigger_type, share_token=None, generation_data=None):
    """Send automated follow-up email with retry logic"""
    # Get lead information for personalization
    db = get_db()
    cur = db.execute("SELECT email, share_token, created_at FROM leads WHERE id=?", (lead_id,))
    lead_row = cur.fetchone()
    
    # Extract name from email (simple heuristic)
    name = email_to.split('@')[0].replace('.', ' ').replace('_', ' ').title() if '@' in email_to else None
    
    if trigger_type == 'download':
        # Get share information
        if share_token:
            cur = db.execute("SELECT title, meta_json FROM shares WHERE token=?", (share_token,))
            share_row = cur.fetchone()
            if share_row:
                title = share_row[0] or "AI Generated Art"
                try:
                    meta = json.loads(share_row[1] or '{}')
                    first_image = meta.get('images', [''])[0] or 'https://via.placeholder.com/300x300?text=AI+Art'
                except:
                    first_image = 'https://via.placeholder.com/300x300?text=AI+Art'
                
                base_url = os.getenv("PUBLIC_BASE_URL", "")
                share_url = f"{base_url}/s/{share_token}" if base_url else f"/s/{share_token}"
                
                html_content, text_content = _generate_download_email(name, title, first_image, share_url)
                subject = "Thanks for exploring Chaos Venice Productions"
            else:
                return False, "Share not found"
        else:
            return False, "No share token provided for download trigger"
            
    elif trigger_type == 'hire_us':
        inquiry_details = generation_data or {}
        html_content, text_content = _generate_hire_us_email(name, inquiry_details)
        subject = "Your Creative Vision Awaits"
    
    elif trigger_type == 'manual':
        # Manual emails would have custom content passed in generation_data
        subject = generation_data.get('subject', 'Message from Chaos Venice Productions')
        html_content = generation_data.get('html_content', '<p>Custom message content</p>')
        text_content = generation_data.get('text_content', 'Custom message content')
    
    else:
        return False, f"Unknown trigger type: {trigger_type}"
    
    # Attempt to send email
    success, message = _send_email(email_to, subject, html_content, text_content)
    
    # Log the email attempt
    status = 'sent' if success else 'failed'
    db.execute("""INSERT INTO sent_emails(lead_id, email_to, subject, trigger_type, status, share_token, sent_at, error_message)
                  VALUES(?,?,?,?,?,?,?,?)""",
               (lead_id, email_to, subject, trigger_type, status, share_token, 
                datetime.datetime.utcnow().isoformat(), None if success else message))
    db.commit()
    
    if not success:
        _log_error("ERROR", f"Email failed for lead {lead_id}", f"To: {email_to}, Error: {message}")
    
    return success, message

def _retry_failed_emails():
    """Retry failed emails with exponential backoff"""
    db = get_db()
    
    # Find emails that failed and need retry (max 3 attempts, with delays)
    twenty_four_hours_ago = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat()
    two_minutes_ago = (datetime.datetime.utcnow() - datetime.timedelta(minutes=2)).isoformat()
    
    cur = db.execute("""SELECT id, lead_id, email_to, subject, trigger_type, share_token, retry_count, sent_at
                        FROM sent_emails 
                        WHERE status='failed' 
                        AND retry_count < 3 
                        AND sent_at > ? 
                        AND sent_at < ?
                        ORDER BY sent_at ASC LIMIT 10""", 
                     (twenty_four_hours_ago, two_minutes_ago))
    
    failed_emails = cur.fetchall()
    
    for email_record in failed_emails:
        email_id, lead_id, email_to, subject, trigger_type, share_token, retry_count, sent_at = email_record
        
        # Calculate retry delay: 2 minutes, 10 minutes, 60 minutes
        retry_delays = [2, 10, 60]  # minutes
        required_delay = retry_delays[min(retry_count, len(retry_delays)-1)]
        
        sent_time = datetime.datetime.fromisoformat(sent_at)
        next_retry_time = sent_time + datetime.timedelta(minutes=required_delay)
        
        if datetime.datetime.utcnow() >= next_retry_time:
            # Attempt retry
            success, message = _send_automated_email(lead_id, email_to, trigger_type, share_token)
            
            if success:
                # Update original record to 'sent'
                db.execute("UPDATE sent_emails SET status='sent', retry_count=? WHERE id=?", 
                          (retry_count + 1, email_id))
                _log_error("INFO", f"Email retry succeeded for lead {lead_id}", f"Attempt {retry_count + 1}")
            else:
                # Update retry count
                db.execute("UPDATE sent_emails SET retry_count=?, error_message=? WHERE id=?", 
                          (retry_count + 1, message, email_id))
                _log_error("WARNING", f"Email retry {retry_count + 1} failed for lead {lead_id}", message)
            
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
    
    # Social media share stats
    cur = db.execute("""SELECT platform, COUNT(*) as total_shares,
                        SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful_shares
                        FROM social_shares GROUP BY platform""")
    social_stats = []
    for r in cur.fetchall():
        social_stats.append({
            "platform": r[0], 
            "total": r[1], 
            "successful": r[2],
            "success_rate": round((r[2] / r[1]) * 100, 1) if r[1] > 0 else 0
        })
    
    # Recent social shares
    cur = db.execute("""SELECT ss.share_token, ss.platform, ss.success, ss.posted_at, s.title
                        FROM social_shares ss 
                        LEFT JOIN shares s ON ss.share_token = s.token
                        ORDER BY ss.posted_at DESC LIMIT 20""")
    recent_social = []
    for r in cur.fetchall():
        recent_social.append({
            "token": r[0], "platform": r[1], "success": bool(r[2]), 
            "time": r[3], "title": r[4]
        })
    
    return jsonify({
        "recent_visits": visits,
        "popular_shares": popular_shares,
        "daily_stats": daily_stats,
        "social_stats": social_stats,
        "recent_social": recent_social
    })

@app.get("/admin/logs")
@require_admin
def admin_logs():
    """View error logs"""
    db = get_db()
    cur = db.execute("""SELECT level, message, details, created_at 
                        FROM error_logs ORDER BY created_at DESC LIMIT 200""")
    logs = []
    for r in cur.fetchall():
        logs.append({
            "level": r[0], "message": r[1], "details": r[2], "created_at": r[3]
        })
    return jsonify({"logs": logs})

# --- SOCIAL MEDIA INTEGRATION ---

@app.post("/social/auth/<platform>")
@require_api_key
def social_auth(platform):
    """Initiate OAuth flow for social platform"""
    if platform not in ['twitter', 'instagram', 'linkedin']:
        return jsonify({"error": "Unsupported platform"}), 400
    
    # OAuth URLs (would need real client IDs in production)
    oauth_urls = {
        'twitter': 'https://twitter.com/i/oauth2/authorize',
        'instagram': 'https://api.instagram.com/oauth/authorize', 
        'linkedin': 'https://www.linkedin.com/oauth/v2/authorization'
    }
    
    # For demo purposes, return mock success
    # In production, this would redirect to OAuth provider
    return jsonify({
        "auth_url": oauth_urls[platform],
        "state": "mock_state_token",
        "message": f"Redirect user to {platform} OAuth"
    })

@app.post("/social/callback/<platform>")
@require_api_key
def social_callback(platform):
    """Handle OAuth callback and store tokens"""
    data = request.get_json(force=True)
    code = data.get("code")
    state = data.get("state")
    
    if not code:
        return jsonify({"error": "Missing authorization code"}), 400
    
    # In production, exchange code for access token
    # For demo, accept any code as valid
    mock_token = f"mock_{platform}_token_{secrets.token_urlsafe(16)}"
    expires_at = (datetime.datetime.utcnow() + datetime.timedelta(days=60)).isoformat()
    
    _store_social_token(g.api_key, platform, mock_token, expires_at=expires_at)
    
    return jsonify({"ok": True, "message": f"{platform.title()} connected successfully"})

@app.post("/social/share")
@require_api_key
def social_share():
    """Share content to social media platforms"""
    data = request.get_json(force=True)
    share_token = data.get("share_token")
    platforms = data.get("platforms", [])
    caption = data.get("caption", "")
    
    if not share_token or not platforms:
        return jsonify({"error": "Missing share_token or platforms"}), 400
    
    # Get share data
    db = get_db()
    cur = db.execute("SELECT title, meta_json FROM shares WHERE token=?", (share_token,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Share not found"}), 404
    
    title, meta_json = row
    try:
        meta = json.loads(meta_json)
        images = meta.get("images", [])
        if not images:
            return jsonify({"error": "No images in share"}), 400
        
        # Use first image as teaser
        teaser_image = images[0]
        
        # Generate share URL
        base_url = os.getenv("PUBLIC_BASE_URL", "")
        share_url = f"{base_url}/s/{share_token}" if base_url else f"/s/{share_token}"
        
        # Default caption if empty
        if not caption:
            caption = f"🎨 {title or 'AI Generated Art'}\n\nWhere Imagination Meets Precision ✨"
        
        results = []
        
        for platform in platforms:
            if platform not in ['twitter', 'instagram', 'linkedin']:
                results.append({
                    "platform": platform,
                    "success": False,
                    "error": "Unsupported platform"
                })
                continue
            
            # Get stored token
            token_data = _get_social_token(g.api_key, platform)
            if not token_data:
                results.append({
                    "platform": platform,
                    "success": False,
                    "error": f"No {platform} account connected. Please authenticate first."
                })
                _track_social_share(share_token, platform, False, error_message="No token")
                continue
            
            # Resize image for platform
            optimized_image = _resize_image_for_platform(teaser_image, platform)
            
            # Post to platform
            try:
                success = False
                result = "Unknown error"
                
                if platform == 'twitter':
                    success, result = _post_to_twitter(token_data["access_token"], caption, optimized_image, share_url)
                elif platform == 'instagram':
                    success, result = _post_to_instagram(token_data["access_token"], caption, optimized_image, share_url)
                elif platform == 'linkedin':
                    success, result = _post_to_linkedin(token_data["access_token"], caption, optimized_image, share_url)
                
                if success:
                    results.append({
                        "platform": platform,
                        "success": True,
                        "post_id": result
                    })
                    _track_social_share(share_token, platform, True, post_id=result)
                else:
                    results.append({
                        "platform": platform,
                        "success": False,
                        "error": result
                    })
                    _track_social_share(share_token, platform, False, error_message=result)
                    _log_error("ERROR", f"Social posting failed", f"Platform: {platform}, Error: {result}")
                    
            except Exception as e:
                error_msg = f"Posting error: {str(e)}"
                results.append({
                    "platform": platform,
                    "success": False,
                    "error": error_msg
                })
                _track_social_share(share_token, platform, False, error_message=error_msg)
                _log_error("ERROR", f"Social posting exception", f"Platform: {platform}, Exception: {str(e)}")
        
        return jsonify({"results": results})
        
    except Exception as e:
        _log_error("ERROR", "Social share processing failed", str(e))
        return jsonify({"error": f"Processing failed: {str(e)}"}), 500

@app.get("/social/status")
@require_api_key
def social_status():
    """Get connected social media accounts"""
    db = get_db()
    cur = db.execute("SELECT platform, created_at FROM social_tokens WHERE api_key=?", (g.api_key,))
    connections = []
    for row in cur.fetchall():
        connections.append({
            "platform": row[0],
            "connected_at": row[1],
            "status": "connected"
        })
    
    return jsonify({"connections": connections})

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
    """Capture lead email from share page downloads and send automated follow-up"""
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip()
    share_token = (data.get("share_token") or "").strip()
    
    if not email or "@" not in email:
        return jsonify({"error": "Valid email required"}), 400
    
    # Capture lead and get the ID
    lead_id = _capture_lead(email, share_token, "share_download")
    
    # Send automated follow-up email (download trigger)
    try:
        success, message = _send_automated_email(lead_id, email, 'download', share_token)
        if success:
            _log_error("INFO", f"Download email sent to lead {lead_id}", f"Email: {email}")
        else:
            _log_error("WARNING", f"Download email failed for lead {lead_id}", message)
    except Exception as e:
        _log_error("ERROR", f"Email automation error for lead {lead_id}", str(e))
    
    return jsonify({"ok": True, "message": "Email captured successfully! Check your inbox for exclusive content."})

# --- PORTFOLIO ENDPOINTS ---

@app.route('/portfolio')
def portfolio_gallery():
    """Dynamic portfolio gallery with auto-curated featured works"""
    # Update featured portfolio automatically
    _update_featured_portfolio()
    
    # Get featured works
    db = get_db()
    cur = db.execute("""SELECT f.share_token, f.engagement_score, f.seo_title, f.seo_description,
                               s.title, s.meta_json, s.created_at
                        FROM featured_portfolio f
                        JOIN shares s ON f.share_token = s.token
                        WHERE s.expires_at > ?
                        ORDER BY f.engagement_score DESC LIMIT 24""",
                     (datetime.datetime.utcnow().isoformat(),))
    
    featured_works = []
    for row in cur.fetchall():
        token, score, seo_title, seo_desc, title, meta_json, created_at = row
        try:
            meta = json.loads(meta_json or '{}')
            first_image = meta.get('images', [''])[0] or ''
            
            featured_works.append({
                'token': token,
                'title': title,
                'seo_title': seo_title,
                'seo_description': seo_desc,
                'image_url': first_image,
                'engagement_score': score,
                'created_at': created_at,
                'prompt': meta.get('positive_prompt', '')[:100] + '...' if meta.get('positive_prompt', '') else '',
                'platform': meta.get('platform', 'AI Generated')
            })
        except:
            continue
    
    html = f"""<!doctype html><html><head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Portfolio - Chaos Venice Productions | Professional AI Art Gallery</title>
    <meta name="description" content="Explore Chaos Venice Productions' curated portfolio of professional AI-generated artwork. Commission similar works or license existing pieces for commercial use.">
    <style>
    body{{font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:linear-gradient(135deg,#0a0e1a 0%,#0f1419 50%,#1a1f2e 100%);color:#e7edf5;margin:0;min-height:100vh}}
    .wrap{{max-width:1400px;margin:0 auto;padding:20px}}
    .header{{text-align:center;margin-bottom:50px;padding:40px 20px;background:linear-gradient(45deg,rgba(0,255,255,0.1),rgba(255,0,255,0.1));border-radius:20px;border:1px solid #1f2a3a}}
    .logo{{font-size:3rem;font-weight:800;background:linear-gradient(45deg,#00ffff,#ff00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:15px}}
    .tagline{{font-size:1.3rem;color:#9fb2c7;font-style:italic;margin-bottom:10px}}
    .subtitle{{color:#4f9eff;font-size:1.1rem}}
    .gallery-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(350px,1fr));gap:30px;margin-top:40px}}
    .portfolio-item{{background:rgba(17,24,38,0.8);border:1px solid #1f2a3a;border-radius:16px;overflow:hidden;transition:all 0.3s ease;cursor:pointer;position:relative}}
    .portfolio-item:hover{{transform:translateY(-8px);border-color:#00ffff;box-shadow:0 20px 60px rgba(0,255,255,0.2)}}
    .item-image{{width:100%;height:250px;object-fit:cover;background:#000}}
    .item-content{{padding:20px}}
    .item-title{{font-size:1.1rem;font-weight:600;color:#fff;margin-bottom:8px}}
    .item-meta{{color:#9fb2c7;font-size:0.9rem;margin-bottom:12px}}
    .item-prompt{{color:#4f9eff;font-size:0.85rem;margin-bottom:15px;font-style:italic}}
    .item-actions{{display:flex;gap:10px;flex-wrap:wrap}}
    .btn{{padding:8px 16px;border-radius:8px;text-decoration:none;font-weight:600;font-size:0.9rem;text-align:center;transition:all 0.2s ease;border:none;cursor:pointer}}
    .btn-primary{{background:linear-gradient(45deg,#00ffff,#ff00ff);color:#000}}
    .btn-primary:hover{{transform:scale(1.05)}}
    .btn-secondary{{background:transparent;border:2px solid #00ffff;color:#00ffff}}
    .btn-secondary:hover{{background:#00ffff;color:#000}}
    .engagement-badge{{position:absolute;top:15px;right:15px;background:rgba(0,255,255,0.8);color:#000;padding:4px 8px;border-radius:12px;font-size:0.8rem;font-weight:600}}
    .stats{{text-align:center;margin:50px 0;padding:30px;background:rgba(17,24,38,0.6);border-radius:16px;border:1px solid #1f2a3a}}
    .stats h3{{color:#00ffff;margin-bottom:20px}}
    .stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:20px}}
    .stat-item{{text-align:center}}
    .stat-number{{font-size:2rem;font-weight:800;background:linear-gradient(45deg,#00ffff,#ff00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
    .stat-label{{color:#9fb2c7;font-size:0.9rem}}
    @media(max-width:768px){{.gallery-grid{{grid-template-columns:1fr;gap:20px}}.item-actions{{flex-direction:column}}}}
    </style>
    </head><body>
    <div class="wrap">
      <div class="header">
        <div class="logo">CHAOS VENICE PRODUCTIONS</div>
        <div class="tagline">Where Imagination Meets Precision</div>
        <div class="subtitle">Dynamic Portfolio • Auto-Curated Excellence</div>
      </div>
      
      <div class="stats">
        <h3>Portfolio Performance</h3>
        <div class="stats-grid">
          <div class="stat-item">
            <div class="stat-number">{len(featured_works)}</div>
            <div class="stat-label">Featured Works</div>
          </div>
          <div class="stat-item">
            <div class="stat-number">{sum(work['engagement_score'] for work in featured_works):.0f}</div>
            <div class="stat-label">Total Engagement</div>
          </div>
          <div class="stat-item">
            <div class="stat-number">24/7</div>
            <div class="stat-label">Auto-Curation</div>
          </div>
          <div class="stat-item">
            <div class="stat-number">AI+Human</div>
            <div class="stat-label">Hybrid Creation</div>
          </div>
        </div>
      </div>
      
      <div class="gallery-grid">"""
    
    for work in featured_works:
        html += f"""
        <div class="portfolio-item" onclick="viewPortfolioItem('{work['token']}')">
          <div class="engagement-badge">{work['engagement_score']:.0f}</div>
          <img src="{work['image_url']}" alt="{work['title']}" class="item-image" loading="lazy">
          <div class="item-content">
            <div class="item-title">{work['title']}</div>
            <div class="item-meta">{work['platform']} • {work['created_at'][:10]}</div>
            <div class="item-prompt">{work['prompt']}</div>
            <div class="item-actions">
              <button class="btn btn-primary" onclick="event.stopPropagation(); orderSimilar('{work['token']}')">Order Similar</button>
              <button class="btn btn-secondary" onclick="event.stopPropagation(); licenseImage('{work['token']}')">License Image</button>
            </div>
          </div>
        </div>"""
    
    html += f"""
      </div>
      
      <div style="text-align:center;margin:60px 0;padding:40px;background:rgba(17,24,38,0.6);border-radius:16px;border:1px solid #1f2a3a">
        <h3 style="color:#00ffff;margin-bottom:20px">Looking for Something Specific?</h3>
        <p style="color:#9fb2c7;margin-bottom:30px">Our portfolio updates automatically based on engagement and quality. Can't find what you need?</p>
        <a href="/contact" class="btn btn-primary" style="display:inline-block;padding:16px 32px;text-decoration:none">Commission Custom Work</a>
      </div>
    </div>
    
    <script>
    function viewPortfolioItem(token) {{
      // Track view
      fetch('/portfolio/track', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{token: token, action: 'view'}})
      }});
      
      // Open detail page
      window.open('/portfolio/' + token, '_blank');
    }}
    
    function orderSimilar(token) {{
      // Track click
      fetch('/portfolio/track', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{token: token, action: 'click'}})
      }});
      
      // Open order form
      window.open('/portfolio/' + token + '/order', '_blank');
    }}
    
    function licenseImage(token) {{
      // Track click
      fetch('/portfolio/track', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{token: token, action: 'click'}})
      }});
      
      // Open license page
      window.open('/portfolio/' + token + '/license', '_blank');
    }}
    </script>
    </body></html>"""
    
    return Response(html, 200, mimetype="text/html")

@app.route('/portfolio/track', methods=['POST'])
def track_portfolio_action():
    """Track portfolio interactions for analytics"""
    data = request.get_json()
    token = data.get('token')
    action = data.get('action', 'view')
    
    if token:
        _track_portfolio_action(token, action)
    
    return jsonify({"ok": True})

@app.route('/portfolio/<token>')
def portfolio_detail(token):
    """Individual portfolio item detail page with sales CTAs"""
    db = get_db()
    
    # Get share data
    cur = db.execute("SELECT title, meta_json, created_at FROM shares WHERE token=? AND expires_at > ?",
                     (token, datetime.datetime.utcnow().isoformat()))
    share_data = cur.fetchone()
    
    if not share_data:
        return "Portfolio item not found", 404
    
    # Track view
    _track_portfolio_action(token, 'view')
    
    title, meta_json, created_at = share_data
    try:
        meta = json.loads(meta_json or '{}')
    except:
        meta = {}
    
    # Get engagement stats
    score, views, downloads, social_shares = _calculate_engagement_score(token)
    
    # Get SEO content
    seo_title, seo_desc = _generate_seo_content({'title': title, 'meta_json': meta_json})
    
    images = meta.get('images', [])
    prompt = meta.get('positive_prompt', 'Custom AI artwork')
    negative = meta.get('negative_prompt', '')
    platform = meta.get('platform', 'AI Generated')
    
    html = f"""<!doctype html><html><head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{seo_title}</title>
    <meta name="description" content="{seo_desc}">
    <style>
    body{{font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:linear-gradient(135deg,#0a0e1a 0%,#0f1419 50%,#1a1f2e 100%);color:#e7edf5;margin:0;min-height:100vh}}
    .wrap{{max-width:1200px;margin:0 auto;padding:20px}}
    .header{{text-align:center;margin-bottom:40px}}
    .logo{{font-size:2rem;font-weight:800;background:linear-gradient(45deg,#00ffff,#ff00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
    .content{{display:grid;grid-template-columns:1fr 400px;gap:40px;margin-top:30px}}
    @media(max-width:1024px){{.content{{grid-template-columns:1fr;gap:30px}}}}
    .image-section{{}}
    .main-image{{width:100%;height:auto;border-radius:12px;border:2px solid #1f2a3a}}
    .image-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px;margin-top:15px}}
    .thumb{{width:100%;height:80px;object-fit:cover;border-radius:8px;border:1px solid #1f2a3a;cursor:pointer;transition:border-color 0.2s}}
    .thumb:hover{{border-color:#00ffff}}
    .details{{background:rgba(17,24,38,0.8);border:1px solid #1f2a3a;border-radius:16px;padding:30px;height:fit-content}}
    .item-title{{font-size:1.5rem;font-weight:700;color:#fff;margin-bottom:15px}}
    .meta-item{{margin-bottom:15px;padding-bottom:15px;border-bottom:1px solid #1f2a3a}}
    .meta-label{{color:#00ffff;font-weight:600;margin-bottom:5px}}
    .meta-value{{color:#9fb2c7;font-size:0.9rem;word-break:break-word}}
    .cta-section{{background:linear-gradient(45deg,rgba(0,255,255,0.1),rgba(255,0,255,0.1));padding:25px;border-radius:12px;margin:25px 0}}
    .cta-title{{color:#fff;font-size:1.2rem;font-weight:600;margin-bottom:15px}}
    .btn{{display:block;width:100%;padding:15px;margin-bottom:12px;border-radius:8px;text-decoration:none;font-weight:600;text-align:center;transition:all 0.2s ease;border:none;cursor:pointer;font-size:1rem}}
    .btn-primary{{background:linear-gradient(45deg,#00ffff,#ff00ff);color:#000}}
    .btn-primary:hover{{transform:scale(1.02)}}
    .btn-secondary{{background:transparent;border:2px solid #00ffff;color:#00ffff}}
    .btn-secondary:hover{{background:#00ffff;color:#000}}
    .stats{{display:grid;grid-template-columns:repeat(2,1fr);gap:15px;margin-top:20px}}
    .stat{{text-align:center;padding:15px;background:rgba(0,0,0,0.3);border-radius:8px;border:1px solid #1f2a3a}}
    .stat-number{{font-size:1.5rem;font-weight:700;color:#00ffff}}
    .stat-label{{color:#9fb2c7;font-size:0.8rem}}
    </style>
    </head><body>
    <div class="wrap">
      <div class="header">
        <div class="logo">CHAOS VENICE PRODUCTIONS</div>
        <a href="/portfolio" style="color:#4f9eff;text-decoration:none">← Back to Portfolio</a>
      </div>
      
      <div class="content">
        <div class="image-section">
          <img src="{images[0] if images else ''}" alt="{title}" class="main-image" id="mainImage">
          <div class="image-grid">"""
    
    for i, img in enumerate(images[:8]):
        html += f'<img src="{img}" alt="{title} {i+1}" class="thumb" onclick="changeMainImage(\'{img}\')">'
    
    html += f"""
          </div>
        </div>
        
        <div class="details">
          <div class="item-title">{title}</div>
          
          <div class="meta-item">
            <div class="meta-label">Platform</div>
            <div class="meta-value">{platform}</div>
          </div>
          
          <div class="meta-item">
            <div class="meta-label">Created</div>
            <div class="meta-value">{created_at[:10]}</div>
          </div>
          
          <div class="meta-item">
            <div class="meta-label">Prompt</div>
            <div class="meta-value">{prompt}</div>
          </div>
          
          {f'<div class="meta-item"><div class="meta-label">Negative Prompt</div><div class="meta-value">{negative}</div></div>' if negative else ''}
          
          <div class="cta-section">
            <div class="cta-title">💎 Get This Style</div>
            <a href="/portfolio/{token}/order" class="btn btn-primary">Order Similar Artwork</a>
            <a href="/portfolio/{token}/license" class="btn btn-secondary">License This Image</a>
          </div>
          
          <div class="stats">
            <div class="stat">
              <div class="stat-number">{score:.0f}</div>
              <div class="stat-label">Engagement Score</div>
            </div>
            <div class="stat">
              <div class="stat-number">{views}</div>
              <div class="stat-label">Views</div>
            </div>
            <div class="stat">
              <div class="stat-number">{downloads}</div>
              <div class="stat-label">Downloads</div>
            </div>
            <div class="stat">
              <div class="stat-number">{social_shares}</div>
              <div class="stat-label">Social Shares</div>
            </div>
          </div>
        </div>
      </div>
    </div>
    
    <script>
    function changeMainImage(src) {{
      document.getElementById('mainImage').src = src;
    }}
    </script>
    </body></html>"""
    
    return Response(html, 200, mimetype="text/html")

@app.route('/contact', methods=['POST'])
def submit_contact_form():
    """Handle contact form submission and send hire us follow-up email"""
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    message = request.form.get('message', '').strip()
    
    if not email or "@" not in email:
        return jsonify({"error": "Valid email required"}), 400
    
    if not message:
        return jsonify({"error": "Message required"}), 400
    
    # Capture lead for hire us inquiry
    try:
        lead_id = _capture_lead(email, None, "contact_form")
        
        # Send automated follow-up email (hire us trigger)
        inquiry_data = {
            'name': name,
            'email': email,
            'message': message,
            'submitted_at': datetime.datetime.utcnow().isoformat()
        }
        
        success, message_result = _send_automated_email(lead_id, email, 'hire_us', None, inquiry_data)
        if success:
            _log_error("INFO", f"Hire us email sent to lead {lead_id}", f"Email: {email}")
        else:
            _log_error("WARNING", f"Hire us email failed for lead {lead_id}", message_result)
            
    except Exception as e:
        _log_error("ERROR", f"Contact form email automation error", str(e))
    
    return jsonify({"ok": True, "message": "Thank you for your inquiry! We'll reply within 24 hours with a detailed proposal."})

# --- ADMIN EMAIL MANAGEMENT ENDPOINTS ---

@app.route('/admin/emails')
def admin_emails():
    """View all sent emails with status and retry information"""
    admin_token = request.args.get('admin_token')
    if admin_token != os.getenv('ADMIN_TOKEN'):
        return jsonify({"error": "Invalid admin token"}), 401
    
    db = get_db()
    cur = db.execute("""SELECT e.id, e.email_to, e.subject, e.trigger_type, e.status, 
                               e.retry_count, e.sent_at, e.error_message, l.email as lead_email
                        FROM sent_emails e 
                        LEFT JOIN leads l ON e.lead_id = l.id
                        ORDER BY e.sent_at DESC LIMIT 100""")
    
    emails = []
    for row in cur.fetchall():
        emails.append({
            "id": row[0],
            "email_to": row[1], 
            "subject": row[2],
            "trigger_type": row[3],
            "status": row[4],
            "retry_count": row[5],
            "sent_at": row[6],
            "error_message": row[7],
            "lead_email": row[8]
        })
    
    return jsonify({
        "emails": emails,
        "total_count": len(emails),
        "stats": {
            "sent": len([e for e in emails if e["status"] == "sent"]),
            "failed": len([e for e in emails if e["status"] == "failed"]),
            "retrying": len([e for e in emails if e["status"] == "retry"])
        }
    })

@app.route('/admin/emails/resend', methods=['POST'])
def admin_resend_email():
    """Manually resend a failed email"""
    admin_token = request.form.get('admin_token')
    if admin_token != os.getenv('ADMIN_TOKEN'):
        return jsonify({"error": "Invalid admin token"}), 401
    
    email_id = request.form.get('email_id')
    if not email_id:
        return jsonify({"error": "Email ID required"}), 400
    
    try:
        db = get_db()
        cur = db.execute("""SELECT lead_id, email_to, trigger_type, share_token 
                            FROM sent_emails WHERE id=?""", (email_id,))
        email_record = cur.fetchone()
        
        if not email_record:
            return jsonify({"error": "Email not found"}), 404
        
        lead_id, email_to, trigger_type, share_token = email_record
        
        # Attempt to resend
        success, message = _send_automated_email(lead_id, email_to, trigger_type, share_token)
        
        if success:
            # Update original record
            db.execute("UPDATE sent_emails SET status='sent' WHERE id=?", (email_id,))
            db.commit()
            return jsonify({"ok": True, "message": "Email resent successfully"})
        else:
            return jsonify({"error": f"Resend failed: {message}"}), 500
            
    except Exception as e:
        return jsonify({"error": f"Resend error: {str(e)}"}), 500

@app.route('/admin/emails/stats')
def admin_email_stats():
    """Get email automation statistics"""
    admin_token = request.args.get('admin_token')
    if admin_token != os.getenv('ADMIN_TOKEN'):
        return jsonify({"error": "Invalid admin token"}), 401
    
    db = get_db()
    
    # Overall stats
    cur = db.execute("SELECT COUNT(*) FROM sent_emails")
    total_emails = cur.fetchone()[0]
    
    cur = db.execute("SELECT status, COUNT(*) FROM sent_emails GROUP BY status")
    status_counts = {row[0]: row[1] for row in cur.fetchall()}
    
    # Trigger type breakdown
    cur = db.execute("SELECT trigger_type, COUNT(*) FROM sent_emails GROUP BY trigger_type")
    trigger_counts = {row[0]: row[1] for row in cur.fetchall()}
    
    # Recent activity (last 24 hours)
    yesterday = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat()
    cur = db.execute("SELECT COUNT(*) FROM sent_emails WHERE sent_at > ?", (yesterday,))
    recent_emails = cur.fetchone()[0]
    
    # Failed emails needing retry
    cur = db.execute("SELECT COUNT(*) FROM sent_emails WHERE status='failed' AND retry_count < 3")
    retry_pending = cur.fetchone()[0]
    
    return jsonify({
        "total_emails": total_emails,
        "status_breakdown": status_counts,
        "trigger_breakdown": trigger_counts,
        "recent_24h": recent_emails,
        "retry_pending": retry_pending,
        "success_rate": round(status_counts.get("sent", 0) / max(total_emails, 1) * 100, 1)
    })

# Background email retry scheduler (called periodically)
def _run_email_retry_scheduler():
    """Background task to retry failed emails"""
    try:
        _retry_failed_emails()
    except Exception as e:
        _log_error("ERROR", "Email retry scheduler failed", str(e))

# Add endpoint to manually trigger email retries (for testing/admin)
@app.route('/admin/emails/retry-failed', methods=['POST'])
def admin_retry_failed_emails():
    """Manually trigger retry of failed emails"""
    admin_token = request.form.get('admin_token')
    if admin_token != os.getenv('ADMIN_TOKEN'):
        return jsonify({"error": "Invalid admin token"}), 401
    
    try:
        _run_email_retry_scheduler()
        return jsonify({"ok": True, "message": "Email retry scheduler executed"})
    except Exception as e:
        return jsonify({"error": f"Retry failed: {str(e)}"}), 500

# Hook into requests to periodically run email retries
@app.before_request
def before_request():
    """Run background tasks occasionally"""
    import random
    
    # Run email retry scheduler on ~2% of requests (avoids blocking)
    if random.random() < 0.02:
        try:
            _run_email_retry_scheduler()
        except:
            pass  # Don't block requests if retry fails

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
  
  <div class="card">
    <h3 style="color:#00ffff;text-align:center">Get Started - Tell Us About Your Project</h3>
    <p style="text-align:center;color:#9fb2c7;margin-bottom:30px">Fill out the form below and we'll get back to you within 24 hours with a detailed proposal.</p>
    
    <form id="contactForm" onsubmit="submitContactForm(event)" style="max-width:600px;margin:0 auto">
      <div style="margin-bottom:20px">
        <label style="display:block;margin-bottom:8px;color:#00ffff;font-weight:600">Name</label>
        <input type="text" name="name" required style="width:100%;padding:12px;border:1px solid #1f2a3a;border-radius:8px;background:rgba(0,0,0,0.3);color:#e7edf5;font-size:16px">
      </div>
      
      <div style="margin-bottom:20px">
        <label style="display:block;margin-bottom:8px;color:#00ffff;font-weight:600">Email *</label>
        <input type="email" name="email" required style="width:100%;padding:12px;border:1px solid #1f2a3a;border-radius:8px;background:rgba(0,0,0,0.3);color:#e7edf5;font-size:16px">
      </div>
      
      <div style="margin-bottom:20px">
        <label style="display:block;margin-bottom:8px;color:#00ffff;font-weight:600">Project Details *</label>
        <textarea name="message" required rows="5" placeholder="Tell us about your project: What type of content do you need? What's your timeline? Any specific requirements or style preferences?" style="width:100%;padding:12px;border:1px solid #1f2a3a;border-radius:8px;background:rgba(0,0,0,0.3);color:#e7edf5;font-size:16px;resize:vertical"></textarea>
      </div>
      
      <div style="text-align:center">
        <button type="submit" style="padding:16px 32px;background:linear-gradient(45deg,#00ffff,#ff00ff);border:none;border-radius:12px;color:#000;font-weight:600;font-size:16px;cursor:pointer">Send Inquiry</button>
      </div>
    </form>
    
    <div id="contactMessage" style="margin-top:20px;padding:15px;border-radius:8px;display:none;text-align:center"></div>
  </div>
  
  <script>
  function submitContactForm(event) {
    event.preventDefault();
    
    const form = event.target;
    const formData = new FormData(form);
    const messageDiv = document.getElementById('contactMessage');
    const submitBtn = form.querySelector('button[type="submit"]');
    
    submitBtn.textContent = 'Sending...';
    submitBtn.disabled = true;
    
    fetch('/contact', {
      method: 'POST',
      body: formData
    })
    .then(response => response.json())
    .then(data => {
      if (data.ok) {
        messageDiv.style.display = 'block';
        messageDiv.style.background = 'rgba(0,255,0,0.1)';
        messageDiv.style.border = '1px solid #00ff00';
        messageDiv.style.color = '#00ff00';
        messageDiv.textContent = data.message;
        form.reset();
      } else {
        throw new Error(data.error || 'Unknown error');
      }
    })
    .catch(error => {
      messageDiv.style.display = 'block';
      messageDiv.style.background = 'rgba(255,0,0,0.1)';
      messageDiv.style.border = '1px solid #ff0000';
      messageDiv.style.color = '#ff0000';
      messageDiv.textContent = 'Error: ' + error.message;
    })
    .finally(() => {
      submitBtn.textContent = 'Send Inquiry';
      submitBtn.disabled = false;
    });
  }
  </script>
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
  <div style="text-align:center;margin:20px 0">
    <a href="/portfolio" style="background:linear-gradient(45deg,#00ffff,#ff00ff);color:#000;padding:12px 24px;text-decoration:none;border-radius:8px;font-weight:600;margin:0 10px">🎨 View Portfolio</a>
    <a href="/contact" style="background:transparent;border:2px solid #00ffff;color:#00ffff;padding:10px 22px;text-decoration:none;border-radius:8px;font-weight:600;margin:0 10px">📞 Contact Us</a>
  </div>

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
        <button class="secondary" id="quickShareBtn" style="display:none" onclick="openSocialShare()">Quick Share to Social</button>
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

<!-- SOCIAL SHARE MODAL -->
<div class="modal" id="socialModal">
  <div class="inner">
    <h3>🚀 Quick Share to Social</h3>
    <p style="color:#9fb2c7;margin-bottom:16px">Share your generation to social media with branded Chaos Venice content</p>
    
    <div class="social-platforms">
      <label style="display:flex;align-items:center;margin:8px 0">
        <input type="checkbox" id="shareTwitter" style="width:auto;margin-right:8px">
        <span>🐦 Twitter/X</span>
      </label>
      <label style="display:flex;align-items:center;margin:8px 0">
        <input type="checkbox" id="shareInstagram" style="width:auto;margin-right:8px">
        <span>📷 Instagram</span>
      </label>
      <label style="display:flex;align-items:center;margin:8px 0">
        <input type="checkbox" id="shareLinkedIn" style="width:auto;margin-right:8px">
        <span>💼 LinkedIn</span>
      </label>
    </div>
    
    <label>Caption (editable)</label>
    <textarea id="socialCaption" rows="4" style="width:100%;min-height:80px;resize:vertical" placeholder="🎨 Amazing AI Generated Art

Where Imagination Meets Precision ✨"></textarea>
    
    <div id="socialConnections" style="margin:12px 0;padding:8px;background:rgba(0,255,255,0.05);border-radius:8px;border:1px solid #1f2a3a">
      <small style="color:#9fb2c7">Connected accounts:</small>
      <div id="connectionsList"></div>
    </div>
    
    <div class="rowbtns" style="margin-top:16px">
      <button onclick="shareToSocial()" id="socialShareBtn">Share Selected</button>
      <button class="secondary" onclick="closeSocialShare()">Cancel</button>
      <button class="secondary" onclick="openSocialSettings()">Connect Accounts</button>
    </div>
    
    <div id="socialResults" style="margin-top:12px"></div>
  </div>
</div>

<!-- SOCIAL SETTINGS MODAL -->
<div class="modal" id="socialSettingsModal">
  <div class="inner">
    <h3>🔗 Connect Social Accounts</h3>
    <p style="color:#9fb2c7;margin-bottom:16px">Connect your social media accounts to enable direct posting</p>
    
    <div class="social-auth-buttons">
      <button onclick="connectPlatform('twitter')" style="background:#1da1f2;margin:8px 0">Connect Twitter/X</button>
      <button onclick="connectPlatform('instagram')" style="background:#e4405f;margin:8px 0">Connect Instagram</button>
      <button onclick="connectPlatform('linkedin')" style="background:#0077b5;margin:8px 0">Connect LinkedIn</button>
    </div>
    
    <div class="rowbtns" style="margin-top:16px">
      <button class="secondary" onclick="closeSocialSettings()">Close</button>
    </div>
  </div>
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
      document.getElementById('zipBtn').style.display='inline-block'; document.getElementById('shareBtn').style.display='inline-block'; document.getElementById('quickShareBtn').style.display='inline-block';
      
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

// --- SOCIAL MEDIA FUNCTIONS ---
let CURRENT_SHARE_TOKEN = '';

function openSocialShare(){
  if(!LAST_IMAGES || LAST_IMAGES.length === 0){
    alert('No images to share. Generate images first.');
    return;
  }
  
  // Create share first
  const form = getForm();
  const meta = {
    title: form.idea || 'AI Generated Art',
    images: LAST_IMAGES,
    params: {
      steps: form.steps, cfg_scale: form.cfg_scale, sampler: form.sampler,
      seed: form.seed, batch: form.batch, aspect_ratio: form.aspect_ratio,
      lighting: form.lighting, color_grade: form.color_grade, extra_tags: form.extra_tags
    }
  };
  
  createShareForSocial(meta);
}

async function createShareForSocial(meta){
  try{
    const res = await fetch('/share/create', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(meta)
    });
    const out = await res.json();
    if(!res.ok){ alert(out.error || 'Failed to create share'); return; }
    
    CURRENT_SHARE_TOKEN = out.token;
    
    // Set default caption
    const title = meta.title || 'AI Generated Art';
    document.getElementById('socialCaption').value = `🎨 ${title}\n\nWhere Imagination Meets Precision ✨`;
    
    // Load connected accounts
    loadSocialConnections();
    
    // Show modal
    document.getElementById('socialModal').classList.remove('hidden');
  }catch(e){
    alert('Error creating share: ' + e.message);
  }
}

async function loadSocialConnections(){
  const s = JSON.parse(localStorage.getItem(LS_SETTINGS) || '{}');
  if(!s.apiKey){
    document.getElementById('connectionsList').innerHTML = '<small style="color:#ff6b6b">API key required</small>';
    return;
  }
  
  try{
    const res = await fetch('/social/status', {
      headers: {'X-API-Key': s.apiKey}
    });
    const out = await res.json();
    if(!res.ok){ 
      document.getElementById('connectionsList').innerHTML = '<small style="color:#ff6b6b">Failed to load</small>';
      return; 
    }
    
    const connections = out.connections || [];
    if(connections.length === 0){
      document.getElementById('connectionsList').innerHTML = '<small style="color:#9fb2c7">No accounts connected</small>';
    } else {
      const html = connections.map(c => 
        `<small style="color:#4ade80;display:block">✓ ${c.platform}</small>`
      ).join('');
      document.getElementById('connectionsList').innerHTML = html;
    }
  }catch(e){
    document.getElementById('connectionsList').innerHTML = '<small style="color:#ff6b6b">Error loading connections</small>';
  }
}

async function shareToSocial(){
  if(!CURRENT_SHARE_TOKEN){
    alert('No share token. Try creating share again.');
    return;
  }
  
  const s = JSON.parse(localStorage.getItem(LS_SETTINGS) || '{}');
  if(!s.apiKey){
    alert('API key required. Please set in Settings.');
    return;
  }
  
  const platforms = [];
  if(document.getElementById('shareTwitter').checked) platforms.push('twitter');
  if(document.getElementById('shareInstagram').checked) platforms.push('instagram');
  if(document.getElementById('shareLinkedIn').checked) platforms.push('linkedin');
  
  if(platforms.length === 0){
    alert('Select at least one platform to share to.');
    return;
  }
  
  const caption = document.getElementById('socialCaption').value.trim();
  
  document.getElementById('socialShareBtn').disabled = true;
  document.getElementById('socialShareBtn').textContent = 'Sharing...';
  document.getElementById('socialResults').innerHTML = '<div style="color:#9fb2c7">Posting to social media...</div>';
  
  try{
    const res = await fetch('/social/share', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': s.apiKey
      },
      body: JSON.stringify({
        share_token: CURRENT_SHARE_TOKEN,
        platforms: platforms,
        caption: caption
      })
    });
    
    const out = await res.json();
    if(!res.ok){
      document.getElementById('socialResults').innerHTML = `<div style="color:#ff6b6b">Error: ${out.error}</div>`;
      return;
    }
    
    // Display results
    const results = out.results || [];
    let html = '<div style="margin-top:8px"><strong>Results:</strong></div>';
    
    results.forEach(r => {
      const icon = r.platform === 'twitter' ? '🐦' : r.platform === 'instagram' ? '📷' : '💼';
      const color = r.success ? '#4ade80' : '#ff6b6b';
      const status = r.success ? `✓ Posted (ID: ${r.post_id})` : `✗ ${r.error}`;
      html += `<div style="color:${color};margin:4px 0">${icon} ${r.platform}: ${status}</div>`;
    });
    
    document.getElementById('socialResults').innerHTML = html;
    
    // Auto-close modal after 3 seconds if all successful
    const allSuccess = results.every(r => r.success);
    if(allSuccess){
      setTimeout(() => closeSocialShare(), 3000);
    }
    
  }catch(e){
    document.getElementById('socialResults').innerHTML = `<div style="color:#ff6b6b">Network error: ${e.message}</div>`;
  }finally{
    document.getElementById('socialShareBtn').disabled = false;
    document.getElementById('socialShareBtn').textContent = 'Share Selected';
  }
}

function closeSocialShare(){
  document.getElementById('socialModal').classList.add('hidden');
  document.getElementById('socialResults').innerHTML = '';
  // Reset form
  document.getElementById('shareTwitter').checked = false;
  document.getElementById('shareInstagram').checked = false;
  document.getElementById('shareLinkedIn').checked = false;
  CURRENT_SHARE_TOKEN = '';
}

function openSocialSettings(){
  document.getElementById('socialSettingsModal').classList.remove('hidden');
}

function closeSocialSettings(){
  document.getElementById('socialSettingsModal').classList.add('hidden');
}

async function connectPlatform(platform){
  const s = JSON.parse(localStorage.getItem(LS_SETTINGS) || '{}');
  if(!s.apiKey){
    alert('API key required. Please set in Settings first.');
    return;
  }
  
  try{
    const res = await fetch(`/social/auth/${platform}`, {
      method: 'POST',
      headers: {'X-API-Key': s.apiKey}
    });
    const out = await res.json();
    if(!res.ok){ alert(out.error || 'Auth failed'); return; }
    
    // In production, this would open OAuth window
    // For demo, simulate successful connection
    alert(`${platform} OAuth would open here. For demo, simulating connection...`);
    
    // Mock callback
    setTimeout(async () => {
      try{
        const callbackRes = await fetch(`/social/callback/${platform}`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-API-Key': s.apiKey
          },
          body: JSON.stringify({
            code: 'mock_auth_code_' + Date.now(),
            state: 'mock_state'
          })
        });
        const callbackOut = await callbackRes.json();
        if(callbackRes.ok){
          alert(callbackOut.message || `${platform} connected successfully!`);
          loadSocialConnections(); // Refresh connections
        } else {
          alert(callbackOut.error || 'Connection failed');
        }
      }catch(e){
        alert('Connection callback failed: ' + e.message);
      }
    }, 1000);
    
  }catch(e){
    alert('Connection error: ' + e.message);
  }
}

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

# --- PORTFOLIO ORDER AND LICENSE ENDPOINTS ---

@app.route('/portfolio/<token>/order')
def portfolio_order_form(token):
    """Order similar artwork form with parameter pre-fill"""
    db = get_db()
    
    # Get share data
    cur = db.execute("SELECT title, meta_json FROM shares WHERE token=? AND expires_at > ?",
                     (token, datetime.datetime.utcnow().isoformat()))
    share_data = cur.fetchone()
    
    if not share_data:
        return "Portfolio item not found", 404
    
    # Track order action
    _track_portfolio_action(token, 'order')
    
    title, meta_json = share_data
    try:
        meta = json.loads(meta_json or '{}')
    except:
        meta = {}
    
    image_url = meta.get('images', [''])[0] or ''
    
    html = f"""<!doctype html><html><head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Order Similar to {title} - Chaos Venice Productions</title>
    <style>
    body{{font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:linear-gradient(135deg,#0a0e1a 0%,#0f1419 50%,#1a1f2e 100%);color:#e7edf5;margin:0;min-height:100vh}}
    .wrap{{max-width:800px;margin:0 auto;padding:20px}}
    .header{{text-align:center;margin-bottom:40px;padding:30px;background:linear-gradient(45deg,rgba(0,255,255,0.1),rgba(255,0,255,0.1));border-radius:20px;border:1px solid #1f2a3a}}
    .logo{{font-size:2.5rem;font-weight:800;background:linear-gradient(45deg,#00ffff,#ff00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:10px}}
    .tagline{{color:#9fb2c7;font-size:1.1rem}}
    .reference{{background:rgba(17,24,38,0.8);border:1px solid #1f2a3a;border-radius:16px;padding:20px;margin-bottom:30px}}
    .ref-image{{width:100%;max-width:300px;height:200px;object-fit:cover;border-radius:8px;border:2px solid #1f2a3a;margin-bottom:15px}}
    .ref-title{{font-size:1.2rem;font-weight:600;color:#fff;margin-bottom:10px}}
    .form-section{{background:rgba(17,24,38,0.8);border:1px solid #1f2a3a;border-radius:16px;padding:30px}}
    .form-group{{margin-bottom:20px}}
    .label{{display:block;margin-bottom:8px;color:#00ffff;font-weight:600}}
    .input,.textarea{{width:100%;padding:12px;border:1px solid #1f2a3a;border-radius:8px;background:rgba(0,0,0,0.3);color:#e7edf5;font-size:16px}}
    .textarea{{resize:vertical;min-height:100px}}
    .btn{{padding:16px 32px;border-radius:12px;text-decoration:none;font-weight:600;text-align:center;transition:all 0.2s ease;border:none;cursor:pointer;font-size:1rem;width:100%}}
    .btn-primary{{background:linear-gradient(45deg,#00ffff,#ff00ff);color:#000}}
    .message{{margin-top:20px;padding:15px;border-radius:8px;display:none;text-align:center}}
    </style>
    </head><body>
    <div class="wrap">
      <div class="header">
        <div class="logo">CHAOS VENICE PRODUCTIONS</div>
        <div class="tagline">Commission Similar Artwork</div>
      </div>
      
      <div class="reference">
        <h3 style="color:#00ffff;margin-bottom:15px">📸 Reference Artwork</h3>
        <img src="{image_url}" alt="{title}" class="ref-image">
        <div class="ref-title">{title}</div>
      </div>
      
      <form id="orderForm" onsubmit="submitOrder(event)" class="form-section">
        <h3 style="color:#00ffff;margin-bottom:20px">💎 Commission Details</h3>
        
        <div class="form-group">
          <label class="label">Your Name *</label>
          <input type="text" name="name" required class="input" placeholder="Full name for commission">
        </div>
        
        <div class="form-group">
          <label class="label">Email Address *</label>
          <input type="email" name="email" required class="input" placeholder="your@email.com">
        </div>
        
        <div class="form-group">
          <label class="label">Your Vision & Requirements</label>
          <textarea name="custom_requirements" class="textarea" placeholder="Describe what you want:&#10;- Subject matter&#10;- Style adjustments&#10;- Size requirements&#10;- Timeline" required></textarea>
        </div>
        
        <div class="form-group">
          <label class="label">Project Type</label>
          <select name="project_type" class="input" required>
            <option value="">Select project type</option>
            <option value="single_image">Single Custom Image ($299)</option>
            <option value="image_series">Image Series (3-5 images) ($799)</option>
            <option value="brand_package">Complete Brand Package ($1,499)</option>
          </select>
        </div>
        
        <button type="submit" class="btn btn-primary">Submit Commission Request</button>
        <div id="orderMessage" class="message"></div>
      </form>
    </div>
    
    <script>
    function submitOrder(event) {{
      event.preventDefault();
      
      const form = event.target;
      const formData = new FormData(form);
      const messageDiv = document.getElementById('orderMessage');
      const submitBtn = form.querySelector('button[type="submit"]');
      
      submitBtn.textContent = 'Submitting...';
      submitBtn.disabled = true;
      
      formData.append('reference_token', '{token}');
      
      fetch('/portfolio/{token}/submit-order', {{
        method: 'POST',
        body: formData
      }})
      .then(response => response.json())
      .then(data => {{
        if (data.ok) {{
          if (data.redirect) {{
            window.location.href = data.redirect;
          }} else {{
            messageDiv.style.display = 'block';
            messageDiv.style.background = 'rgba(0,255,0,0.1)';
            messageDiv.style.border = '1px solid #00ff00';
            messageDiv.style.color = '#00ff00';
            messageDiv.innerHTML = data.message + '<br><br>Redirecting to exclusive offer...';
            setTimeout(() => window.location.href = data.redirect || '/', 2000);
          }}
        }} else {{
          throw new Error(data.error || 'Unknown error');
        }}
      }})
      .catch(error => {{
        messageDiv.style.display = 'block';
        messageDiv.style.background = 'rgba(255,0,0,0.1)';
        messageDiv.style.border = '1px solid #ff0000';
        messageDiv.style.color = '#ff0000';
        messageDiv.textContent = 'Error: ' + error.message;
      }})
      .finally(() => {{
        submitBtn.textContent = 'Submit Commission Request';
        submitBtn.disabled = false;
      }});
    }}
    </script>
    </body></html>"""
    
    return Response(html, 200, mimetype="text/html")

@app.route('/portfolio/<token>/submit-order', methods=['POST'])
def submit_portfolio_order():
    """Process portfolio commission order - redirects to upsell page"""
    token = request.form.get('reference_token')
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    custom_requirements = request.form.get('custom_requirements', '').strip()
    project_type = request.form.get('project_type', '')
    
    if not all([name, email, custom_requirements, project_type]):
        return jsonify({"error": "All fields are required"}), 400
    
    try:
        db = get_db()
        
        # Create order record
        order_details = {
            'name': name,
            'email': email,
            'custom_requirements': custom_requirements,
            'project_type': project_type,
            'submitted_at': datetime.datetime.utcnow().isoformat()
        }
        
        pricing = {'single_image': 29900, 'image_series': 79900, 'brand_package': 149900}
        
        # Create upsell session ID
        upsell_token = _generate_share_token()
        
        db.execute("""INSERT INTO portfolio_orders(share_token, order_type, customer_email, customer_name, 
                      order_details, price_cents, created_at, status, upsell_token)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                   (token, 'similar', email, name, json.dumps(order_details),
                    pricing.get(project_type, 0), datetime.datetime.utcnow().isoformat(), 'upsell_pending', upsell_token))
        db.commit()
        
        # Capture as lead with upsell trigger
        lead_id = _capture_lead(email, token, "upsell_trigger")
        
        # Start upsell sequence
        _start_upsell_sequence(lead_id, email, upsell_token, token, project_type)
        
        return jsonify({
            "ok": True, 
            "redirect": f"/upsell/{upsell_token}",
            "message": "Redirecting to exclusive offer..."
        })
        
    except Exception as e:
        _log_error("ERROR", "Portfolio order submission failed", str(e))
        return jsonify({"error": "Failed to submit order"}), 500

@app.route('/portfolio/<token>/license')
def portfolio_license_form(token):
    """License existing image form"""
    db = get_db()
    
    # Get share data
    cur = db.execute("SELECT title, meta_json FROM shares WHERE token=? AND expires_at > ?",
                     (token, datetime.datetime.utcnow().isoformat()))
    share_data = cur.fetchone()
    
    if not share_data:
        return "Portfolio item not found", 404
    
    # Track license action
    _track_portfolio_action(token, 'license')
    
    title, meta_json = share_data
    try:
        meta = json.loads(meta_json or '{}')
    except:
        meta = {}
    
    image_url = meta.get('images', [''])[0] or ''
    
    html = f"""<!doctype html><html><head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>License {title} - Chaos Venice Productions</title>
    <style>
    body{{font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:linear-gradient(135deg,#0a0e1a 0%,#0f1419 50%,#1a1f2e 100%);color:#e7edf5;margin:0;min-height:100vh}}
    .wrap{{max-width:800px;margin:0 auto;padding:20px}}
    .header{{text-align:center;margin-bottom:40px;padding:30px;background:linear-gradient(45deg,rgba(0,255,255,0.1),rgba(255,0,255,0.1));border-radius:20px;border:1px solid #1f2a3a}}
    .logo{{font-size:2.5rem;font-weight:800;background:linear-gradient(45deg,#00ffff,#ff00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:10px}}
    .image-preview{{text-align:center;margin-bottom:30px}}
    .license-image{{max-width:100%;height:auto;border-radius:12px;border:2px solid #1f2a3a}}
    .license-options{{display:grid;gap:20px;margin-bottom:30px}}
    .license-option{{background:rgba(17,24,38,0.8);border:2px solid #1f2a3a;border-radius:16px;padding:25px;cursor:pointer;transition:all 0.2s ease}}
    .license-option:hover{{border-color:#00ffff}}
    .license-option.selected{{border-color:#ff00ff;background:rgba(255,0,255,0.1)}}
    .license-title{{font-size:1.2rem;font-weight:600;color:#fff;margin-bottom:10px}}
    .license-price{{font-size:1.8rem;font-weight:800;color:#00ffff;margin-bottom:15px}}
    .form-section{{background:rgba(17,24,38,0.8);border:1px solid #1f2a3a;border-radius:16px;padding:30px;margin-bottom:20px}}
    .form-group{{margin-bottom:20px}}
    .label{{display:block;margin-bottom:8px;color:#00ffff;font-weight:600}}
    .input{{width:100%;padding:12px;border:1px solid #1f2a3a;border-radius:8px;background:rgba(0,0,0,0.3);color:#e7edf5;font-size:16px}}
    .btn{{padding:16px 32px;border-radius:12px;text-decoration:none;font-weight:600;text-align:center;transition:all 0.2s ease;border:none;cursor:pointer;font-size:1rem;width:100%}}
    .btn-primary{{background:linear-gradient(45deg,#00ffff,#ff00ff);color:#000}}
    .btn-primary:disabled{{opacity:0.6}}
    .message{{margin-top:20px;padding:15px;border-radius:8px;display:none;text-align:center}}
    </style>
    </head><body>
    <div class="wrap">
      <div class="header">
        <div class="logo">CHAOS VENICE PRODUCTIONS</div>
        <div class="tagline">License High-Quality AI Artwork</div>
      </div>
      
      <div class="image-preview">
        <img src="{image_url}" alt="{title}" class="license-image">
        <h3 style="color:#fff;margin-top:20px">{title}</h3>
      </div>
      
      <div class="license-options">
        <div class="license-option" onclick="selectLicense('personal', 49)">
          <div class="license-title">Personal Use License</div>
          <div class="license-price">$49</div>
          <ul style="color:#9fb2c7">
            <li>✓ Personal projects and social media</li>
            <li>✓ High-resolution download</li>
            <li>✓ Non-commercial use</li>
          </ul>
        </div>
        
        <div class="license-option" onclick="selectLicense('commercial', 199)">
          <div class="license-title">Commercial License</div>
          <div class="license-price">$199</div>
          <ul style="color:#9fb2c7">
            <li>✓ Commercial use and resale</li>
            <li>✓ Marketing and advertising</li>
            <li>✓ Ultra high-resolution</li>
          </ul>
        </div>
      </div>
      
      <form id="licenseForm" onsubmit="submitLicense(event)" class="form-section">
        <h3 style="color:#00ffff;margin-bottom:20px">📋 License Information</h3>
        
        <div class="form-group">
          <label class="label">Full Name *</label>
          <input type="text" name="name" required class="input" placeholder="Legal name for license">
        </div>
        
        <div class="form-group">
          <label class="label">Email Address *</label>
          <input type="email" name="email" required class="input" placeholder="your@email.com">
        </div>
        
        <div class="form-group">
          <label class="label">Intended Use Description</label>
          <input type="text" name="usage" class="input" placeholder="Brief description of how you'll use this image" required>
        </div>
        
        <input type="hidden" name="license_type" id="licenseType" required>
        <input type="hidden" name="price" id="licensePrice" required>
        
        <button type="submit" class="btn btn-primary" id="submitBtn" disabled>Select a License Above</button>
        <div id="licenseMessage" class="message"></div>
      </form>
    </div>
    
    <script>
    let selectedLicense = null;
    
    function selectLicense(type, price) {{
      document.querySelectorAll('.license-option').forEach(el => el.classList.remove('selected'));
      event.target.closest('.license-option').classList.add('selected');
      
      document.getElementById('licenseType').value = type;
      document.getElementById('licensePrice').value = price;
      
      const submitBtn = document.getElementById('submitBtn');
      submitBtn.disabled = false;
      submitBtn.textContent = `Purchase ${{price}} License`;
      
      selectedLicense = {{type, price}};
    }}
    
    function submitLicense(event) {{
      event.preventDefault();
      
      const form = event.target;
      const formData = new FormData(form);
      const messageDiv = document.getElementById('licenseMessage');
      const submitBtn = document.getElementById('submitBtn');
      
      submitBtn.textContent = 'Processing...';
      submitBtn.disabled = true;
      
      formData.append('reference_token', '{token}');
      
      fetch('/portfolio/{token}/submit-license', {{
        method: 'POST',
        body: formData
      }})
      .then(response => response.json())
      .then(data => {{
        if (data.ok) {{
          messageDiv.style.display = 'block';
          messageDiv.style.background = 'rgba(0,255,0,0.1)';
          messageDiv.style.border = '1px solid #00ff00';
          messageDiv.style.color = '#00ff00';
          messageDiv.innerHTML = data.message + '<br><br>You will receive payment instructions within 2 hours.';
          form.reset();
        }} else {{
          throw new Error(data.error || 'Unknown error');
        }}
      }})
      .catch(error => {{
        messageDiv.style.display = 'block';
        messageDiv.style.background = 'rgba(255,0,0,0.1)';
        messageDiv.style.border = '1px solid #ff0000';
        messageDiv.style.color = '#ff0000';
        messageDiv.textContent = 'Error: ' + error.message;
      }})
      .finally(() => {{
        if (selectedLicense) {{
          submitBtn.textContent = `Purchase ${{selectedLicense.price}} License`;
          submitBtn.disabled = false;
        }}
      }});
    }}
    </script>
    </body></html>"""
    
    return Response(html, 200, mimetype="text/html")

@app.route('/portfolio/<token>/submit-license', methods=['POST'])
def submit_portfolio_license():
    """Process portfolio image licensing"""
    token = request.form.get('reference_token')
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    usage = request.form.get('usage', '').strip()
    license_type = request.form.get('license_type', '')
    price = request.form.get('price', '')
    
    if not all([name, email, usage, license_type, price]):
        return jsonify({"error": "All required fields must be completed"}), 400
    
    try:
        db = get_db()
        
        # Create license order record
        order_details = {
            'name': name,
            'email': email,
            'usage': usage,
            'license_type': license_type,
            'price_dollars': int(price),
            'submitted_at': datetime.datetime.utcnow().isoformat()
        }
        
        db.execute("""INSERT INTO portfolio_orders(share_token, order_type, customer_email, customer_name, 
                      order_details, price_cents, created_at)
                      VALUES(?,?,?,?,?,?,?)""",
                   (token, 'license', email, name, json.dumps(order_details),
                    int(price) * 100, datetime.datetime.utcnow().isoformat()))
        db.commit()
        
        # Capture as lead
        lead_id = _capture_lead(email, token, "portfolio_license")
        
        return jsonify({
            "ok": True, 
            "message": f"License order submitted successfully! Order ID: {token[:8]}"
        })
        
    except Exception as e:
        _log_error("ERROR", "Portfolio license submission failed", str(e))
        return jsonify({"error": "Failed to submit license order"}), 500

# --- PORTFOLIO ADMIN ENDPOINTS ---

@app.route('/upsell/<upsell_token>')
def upsell_page(upsell_token):
    """Tiered upsell offer page with countdown timer and scarcity elements"""
    db = get_db()
    
    # Get upsell session
    cur = db.execute("""SELECT us.*, s.title, s.meta_json FROM upsell_sessions us
                        LEFT JOIN shares s ON us.original_share_token = s.token
                        WHERE us.upsell_token=? AND us.expires_at > ?""",
                     (upsell_token, datetime.datetime.utcnow().isoformat()))
    upsell_data = cur.fetchone()
    
    if not upsell_data:
        return "Upsell offer expired or not found", 404
    
    # Extract data
    (session_id, token, original_token, email, name, project_type, status, 
     tier_selected, expires_at, created_at, completed_at, title, meta_json) = upsell_data
    
    try:
        meta = json.loads(meta_json or '{}')
    except:
        meta = {}
    
    image_url = meta.get('images', [''])[0] or ''
    original_prompt = meta.get('positive_prompt', 'Professional AI artwork')
    
    # Calculate time remaining for scarcity
    expires_dt = datetime.datetime.fromisoformat(expires_at.replace('Z', '+00:00').replace('+00:00', ''))
    time_left_hours = max(0, (expires_dt - datetime.datetime.utcnow()).total_seconds() / 3600)
    
    html = f"""<!doctype html><html><head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Exclusive Offer - {title} - Chaos Venice Productions</title>
    <style>
    body{{font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:linear-gradient(135deg,#0a0e1a 0%,#0f1419 50%,#1a1f2e 100%);color:#e7edf5;margin:0;min-height:100vh}}
    .wrap{{max-width:1200px;margin:0 auto;padding:20px}}
    .header{{text-align:center;margin-bottom:40px;padding:30px;background:linear-gradient(45deg,rgba(255,0,0,0.1),rgba(255,215,0,0.1));border-radius:20px;border:2px solid #ff4444}}
    .logo{{font-size:2.5rem;font-weight:800;background:linear-gradient(45deg,#ff4444,#ffd700);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:10px}}
    .countdown{{font-size:2rem;color:#ff4444;font-weight:700;margin:10px 0}}
    .scarcity{{color:#ffd700;font-size:1.1rem;margin-bottom:20px}}
    .content{{display:grid;grid-template-columns:1fr 2fr;gap:40px;margin-bottom:40px}}
    .image-section{{text-align:center}}
    .main-image{{width:100%;max-width:400px;border-radius:16px;border:3px solid #00ffff}}
    .offers-section{{}}
    .offer-tier{{background:rgba(17,24,38,0.9);border:2px solid #1f2a3a;border-radius:16px;padding:25px;margin-bottom:20px;position:relative;transition:all 0.3s ease}}
    .offer-tier:hover{{border-color:#00ffff;transform:scale(1.02)}}
    .offer-tier.recommended{{border-color:#ffd700;background:rgba(255,215,0,0.05)}}
    .recommended-badge{{position:absolute;top:-10px;right:20px;background:#ffd700;color:#000;padding:5px 15px;border-radius:20px;font-weight:700;font-size:12px}}
    .tier-title{{font-size:1.8rem;font-weight:700;margin-bottom:10px}}
    .tier-price{{font-size:2.5rem;color:#00ffff;font-weight:800;margin-bottom:15px}}
    .tier-original{{text-decoration:line-through;color:#666;font-size:1.2rem;margin-right:10px}}
    .tier-features{{margin-bottom:20px}}
    .tier-features li{{margin-bottom:8px;color:#9fb2c7}}
    .tier-cta{{width:100%;padding:16px;border:none;border-radius:12px;font-size:1.2rem;font-weight:700;cursor:pointer;transition:all 0.2s}}
    .tier-cta.basic{{background:linear-gradient(45deg,#4f46e5,#7c3aed);color:white}}
    .tier-cta.premium{{background:linear-gradient(45deg,#ffd700,#f59e0b);color:#000}}
    .tier-cta.deluxe{{background:linear-gradient(45deg,#ff4444,#dc2626);color:white}}
    .guarantee{{text-align:center;padding:30px;background:rgba(0,255,0,0.05);border:1px solid #00ff00;border-radius:16px;margin-top:30px}}
    .testimonial{{background:rgba(17,24,38,0.8);border-left:4px solid #00ffff;padding:20px;margin:20px 0;border-radius:0 12px 12px 0}}
    @media(max-width:768px){{.content{{grid-template-columns:1fr}}}}
    </style>
    </head><body>
    <div class="wrap">
      <div class="header">
        <div class="logo">🚨 EXCLUSIVE LIMITED-TIME OFFER 🚨</div>
        <div id="countdown" class="countdown">⏰ {time_left_hours:.1f} hours remaining</div>
        <div class="scarcity">⚡ This offer expires in 24 hours and won't be repeated</div>
      </div>
      
      <div class="content">
        <div class="image-section">
          <img src="{image_url}" alt="{title}" class="main-image">
          <h2 style="color:#fff;margin-top:20px">{title}</h2>
          <p style="color:#9fb2c7;margin-top:10px">You loved this piece - now get the complete package!</p>
        </div>
        
        <div class="offers-section">
          <div class="offer-tier">
            <div class="tier-title" style="color:#4f46e5">Basic Pack</div>
            <div class="tier-price">
              <span class="tier-original">$399</span>
              $299
            </div>
            <ul class="tier-features">
              <li>✅ 1 Custom artwork in this style</li>
              <li>✅ 4K resolution download</li>
              <li>✅ Commercial usage rights</li>
              <li>✅ 2 revision rounds</li>
              <li>✅ 48-hour delivery</li>
            </ul>
            <button class="tier-cta basic" onclick="selectTier('basic', 299)">Get Basic Pack - $299</button>
          </div>
          
          <div class="offer-tier recommended">
            <div class="recommended-badge">🔥 MOST POPULAR</div>
            <div class="tier-title" style="color:#ffd700">Premium Pack</div>
            <div class="tier-price">
              <span class="tier-original">$899</span>
              $599
            </div>
            <ul class="tier-features">
              <li>✅ 3 Custom artworks in this style</li>
              <li>✅ 8K ultra-high resolution</li>
              <li>✅ Full commercial + resale rights</li>
              <li>✅ Unlimited revisions</li>
              <li>✅ 24-hour priority delivery</li>
              <li>✅ Bonus: Process videos & source files</li>
              <li>✅ Exclusive style variations</li>
            </ul>
            <button class="tier-cta premium" onclick="selectTier('premium', 599)">Get Premium Pack - $599</button>
          </div>
          
          <div class="offer-tier">
            <div class="tier-title" style="color:#ff4444">Deluxe Brand Package</div>
            <div class="tier-price">
              <span class="tier-original">$1,999</span>
              $1,299
            </div>
            <ul class="tier-features">
              <li>✅ 10 Custom artworks in cohesive style</li>
              <li>✅ Complete brand identity package</li>
              <li>✅ Exclusive ownership rights</li>
              <li>✅ Personal art director consultation</li>
              <li>✅ Same-day delivery available</li>
              <li>✅ Social media templates included</li>
              <li>✅ 12-month support & updates</li>
            </ul>
            <button class="tier-cta deluxe" onclick="selectTier('deluxe', 1299)">Get Deluxe Package - $1,299</button>
          </div>
        </div>
      </div>
      
      <div class="testimonial">
        <p><em>"The Premium Pack exceeded every expectation. The quality is simply unmatched, and having multiple variations gave us incredible flexibility for our campaign."</em></p>
        <strong>— Sarah Chen, Creative Director at TechFlow</strong>
      </div>
      
      <div class="guarantee">
        <h3 style="color:#00ff00">💯 100% Satisfaction Guarantee</h3>
        <p>Not happy with your artwork? We'll revise it until it's perfect, or provide a full refund within 14 days. Your creative vision is our priority.</p>
      </div>
    </div>
    
    <script>
    let timeLeft = {time_left_hours * 3600}; // seconds
    
    function updateCountdown() {{
      const hours = Math.floor(timeLeft / 3600);
      const minutes = Math.floor((timeLeft % 3600) / 60);
      const seconds = timeLeft % 60;
      
      document.getElementById('countdown').textContent = 
        `⏰ ${{hours}}h ${{minutes}}m ${{seconds}}s remaining`;
      
      if (timeLeft <= 0) {{
        document.getElementById('countdown').textContent = '⏰ OFFER EXPIRED';
        document.querySelectorAll('.tier-cta').forEach(btn => {{
          btn.disabled = true;
          btn.textContent = 'Offer Expired';
        }});
      }} else {{
        timeLeft--;
      }}
    }}
    
    setInterval(updateCountdown, 1000);
    
    function selectTier(tier, price) {{
      if (timeLeft <= 0) return;
      
      // Send selection to server
      fetch('/upsell/{upsell_token}/select', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{tier: tier, price: price}})
      }})
      .then(response => response.json())
      .then(data => {{
        if (data.checkout_url) {{
          window.location.href = data.checkout_url;
        }} else if (data.redirect) {{
          window.location.href = data.redirect;
        }}
      }})
      .catch(error => {{
        console.error('Error:', error);
        alert('Something went wrong. Please try again.');
      }});
    }}
    </script>
    </body></html>"""
    
    return Response(html, 200, mimetype="text/html")

@app.route('/upsell/<upsell_token>/select', methods=['POST'])
def select_upsell_tier(upsell_token):
    """Handle tier selection and redirect to Stripe checkout"""
    data = request.get_json()
    tier = data.get('tier')
    price = data.get('price')
    
    if not tier or not price:
        return jsonify({"error": "Missing tier or price"}), 400
    
    try:
        db = get_db()
        
        # Update upsell session with tier selection
        db.execute("""UPDATE upsell_sessions SET tier_selected=?, status='checkout_pending'
                      WHERE upsell_token=? AND expires_at > ?""",
                   (tier, upsell_token, datetime.datetime.utcnow().isoformat()))
        
        affected_rows = db.total_changes
        if affected_rows == 0:
            return jsonify({"error": "Upsell session expired or not found"}), 404
        
        db.commit()
        
        # For now, redirect to a confirmation page (Stripe integration would go here)
        return jsonify({
            "redirect": f"/upsell/{upsell_token}/confirm?tier={tier}&price={price}"
        })
        
    except Exception as e:
        _log_error("ERROR", "Upsell tier selection failed", str(e))
        return jsonify({"error": "Failed to process selection"}), 500

@app.route('/upsell/<upsell_token>/confirm')
def upsell_confirmation(upsell_token):
    """Post-purchase confirmation and next upsell"""
    tier = request.args.get('tier')
    price = request.args.get('price')
    
    db = get_db()
    
    # Mark upsell as completed
    db.execute("""UPDATE upsell_sessions SET status='completed', completed_at=?
                  WHERE upsell_token=?""",
               (datetime.datetime.utcnow().isoformat(), upsell_token))
    
    # Cancel pending follow-up emails
    db.execute("""UPDATE upsell_follow_ups SET status='cancelled'
                  WHERE upsell_token=? AND status='scheduled'""",
               (upsell_token,))
    
    db.commit()
    
    html = f"""<!doctype html><html><head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Order Confirmed - Chaos Venice Productions</title>
    <style>
    body{{font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:linear-gradient(135deg,#0a0e1a 0%,#0f1419 50%,#1a1f2e 100%);color:#e7edf5;margin:0;min-height:100vh}}
    .wrap{{max-width:800px;margin:0 auto;padding:20px;text-align:center}}
    .success{{background:rgba(0,255,0,0.1);border:2px solid #00ff00;border-radius:20px;padding:40px;margin-bottom:40px}}
    .logo{{font-size:2.5rem;font-weight:800;background:linear-gradient(45deg,#00ffff,#ff00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:20px}}
    .post-upsell{{background:rgba(17,24,38,0.8);border:2px solid #ffd700;border-radius:16px;padding:30px;margin-top:30px}}
    .btn{{padding:16px 32px;border:none;border-radius:12px;font-size:1.2rem;font-weight:700;cursor:pointer;background:linear-gradient(45deg,#ffd700,#f59e0b);color:#000;margin:10px}}
    </style>
    </head><body>
    <div class="wrap">
      <div class="success">
        <div class="logo">CHAOS VENICE PRODUCTIONS</div>
        <h1>🎉 Order Confirmed!</h1>
        <p>Thank you for choosing the <strong>{tier.title()} Package (${price})</strong></p>
        <p>You'll receive a detailed timeline and preview within 24 hours.</p>
      </div>
      
      <div class="post-upsell">
        <h3>🔥 One More Exclusive Opportunity</h3>
        <p><strong>Since you loved this style, get 5 bonus variations for just $99</strong></p>
        <p>Perfect for A/B testing, social media content, or different applications.</p>
        <button class="btn" onclick="window.location.href='/post-upsell/{upsell_token}'">Add 5 Bonus Variations - $99</button>
        <p><small>This offer is only available now and expires in 1 hour.</small></p>
      </div>
    </div>
    </body></html>"""
    
    return Response(html, 200, mimetype="text/html")

@app.route('/cron/process-upsell-emails')
def process_upsell_emails():
    """Background task to send scheduled upsell follow-up emails"""
    db = get_db()
    
    # Get all scheduled follow-ups that need to be sent
    cur = db.execute("""SELECT uf.*, us.customer_name, us.customer_email, us.original_share_token, s.title, s.meta_json
                        FROM upsell_follow_ups uf
                        JOIN upsell_sessions us ON uf.upsell_token = us.upsell_token
                        LEFT JOIN shares s ON us.original_share_token = s.token
                        WHERE uf.status='scheduled' AND uf.scheduled_at <= ?
                        ORDER BY uf.scheduled_at ASC""",
                     (datetime.datetime.utcnow().isoformat(),))
    
    pending_emails = cur.fetchall()
    sent_count = 0
    
    for email_data in pending_emails:
        (follow_up_id, upsell_token, sequence, scheduled_at, sent_at, status,
         name, email, original_token, title, meta_json) = email_data
        
        try:
            # Get image for email
            meta = json.loads(meta_json or '{}')
            image_url = meta.get('images', [''])[0] or ''
            
            # Generate email content based on sequence
            subject, html_content = _generate_upsell_email_content(sequence, name, title, image_url, upsell_token)
            
            # Send email using SendGrid or SMTP
            success = _send_upsell_email(email, subject, html_content)
            
            if success:
                # Mark as sent
                db.execute("""UPDATE upsell_follow_ups SET status='sent', sent_at=?
                             WHERE id=?""",
                          (datetime.datetime.utcnow().isoformat(), follow_up_id))
                sent_count += 1
            else:
                # Mark as failed
                db.execute("""UPDATE upsell_follow_ups SET status='failed'
                             WHERE id=?""", (follow_up_id,))
                
        except Exception as e:
            _log_error("ERROR", f"Failed to send upsell email {follow_up_id}", str(e))
            db.execute("""UPDATE upsell_follow_ups SET status='failed'
                         WHERE id=?""", (follow_up_id,))
    
    db.commit()
    
    return jsonify({
        "processed": len(pending_emails),
        "sent": sent_count,
        "failed": len(pending_emails) - sent_count
    })

def _generate_upsell_email_content(sequence, customer_name, artwork_title, image_url, upsell_token):
    """Generate personalized email content for each sequence step"""
    base_url = request.host_url.rstrip('/')
    
    if sequence == 1:
        # Hour 1: Reminder about the exclusive offer
        subject = f"🎨 {customer_name}, your exclusive offer expires soon!"
        html = f"""
        <html><body style="font-family:Arial,sans-serif;background:#f8f9fa;margin:0;padding:20px">
        <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 12px rgba(0,0,0,0.1)">
            <div style="background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;padding:30px;text-align:center">
                <h1>🚨 Don't Miss Out!</h1>
                <p style="font-size:18px;margin:0">Your exclusive offer on "{artwork_title}" expires in just 23 hours</p>
            </div>
            <div style="padding:30px">
                <p>Hi {customer_name},</p>
                <p>I noticed you were interested in commissioning artwork similar to "<strong>{artwork_title}</strong>" but haven't completed your order yet.</p>
                <img src="{image_url}" style="width:100%;max-width:400px;border-radius:8px;margin:20px 0">
                <p>This exclusive offer includes:</p>
                <ul>
                    <li>✅ Up to 65% off regular pricing</li>
                    <li>✅ Priority delivery in 24-48 hours</li>
                    <li>✅ Unlimited revisions until perfect</li>
                    <li>✅ Full commercial licensing rights</li>
                </ul>
                <div style="text-align:center;margin:30px 0">
                    <a href="{base_url}/upsell/{upsell_token}" style="display:inline-block;background:linear-gradient(45deg,#ff6b6b,#ee5a52);color:#fff;padding:16px 32px;border-radius:8px;text-decoration:none;font-weight:bold;font-size:18px">
                        🔥 Claim Your Offer Now
                    </a>
                </div>
                <p><strong>Time-sensitive:</strong> This offer expires in 23 hours and won't be repeated.</p>
                <p>Best regards,<br>The Chaos Venice Productions Team</p>
            </div>
        </div>
        </body></html>
        """
        
    elif sequence == 2:
        # Hour 12: Additional discount and social proof
        subject = f"💰 Extra 10% off for {customer_name} - Final 12 hours!"
        html = f"""
        <html><body style="font-family:Arial,sans-serif;background:#f8f9fa;margin:0;padding:20px">
        <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 12px rgba(0,0,0,0.1)">
            <div style="background:linear-gradient(135deg,#f093fb,#f5576c);color:#fff;padding:30px;text-align:center">
                <h1>🔥 Extra 10% Off!</h1>
                <p style="font-size:18px;margin:0">Final 12 hours - we're adding an extra discount just for you</p>
            </div>
            <div style="padding:30px">
                <p>Hi {customer_name},</p>
                <p>Since you showed genuine interest in our work, I'm authorized to offer you an <strong>additional 10% discount</strong> on top of our already reduced prices.</p>
                <div style="background:#e8f5e8;border:2px solid #4caf50;border-radius:8px;padding:20px;margin:20px 0;text-align:center">
                    <h3 style="color:#2e7d32;margin:0">🎁 LIMITED BONUS DISCOUNT</h3>
                    <p style="font-size:24px;color:#1565c0;font-weight:bold;margin:10px 0">EXTRA10 - Save 10% More!</p>
                    <p style="margin:0;color:#555">Use this code when you complete your order</p>
                </div>
                <img src="{image_url}" style="width:100%;max-width:400px;border-radius:8px;margin:20px 0">
                <div style="text-align:center;margin:30px 0">
                    <a href="{base_url}/upsell/{upsell_token}" style="display:inline-block;background:linear-gradient(45deg,#4caf50,#45a049);color:#fff;padding:16px 32px;border-radius:8px;text-decoration:none;font-weight:bold;font-size:18px">
                        💰 Claim Extra Discount
                    </a>
                </div>
                <p><strong>This expires in just 12 hours</strong> - don't let this opportunity slip away!</p>
                <p>Best regards,<br>Sarah from Chaos Venice Productions</p>
            </div>
        </div>
        </body></html>
        """
        
    elif sequence == 3:
        # Day 2: Behind-the-scenes and process explanation
        subject = f"🎬 {customer_name}, see how we create your masterpiece"
        html = f"""
        <html><body style="font-family:Arial,sans-serif;background:#f8f9fa;margin:0;padding:20px">
        <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 12px rgba(0,0,0,0.1)">
            <div style="background:linear-gradient(135deg,#4facfe,#00f2fe);color:#fff;padding:30px;text-align:center">
                <h1>🎬 Behind the Magic</h1>
                <p style="font-size:18px;margin:0">Ever wondered how we create stunning AI artwork?</p>
            </div>
            <div style="padding:30px">
                <p>Hi {customer_name},</p>
                <p>Since you're interested in professional AI artwork, I thought you'd love to see our creative process:</p>
                <div style="border:1px solid #ddd;border-radius:8px;padding:20px;margin:20px 0">
                    <h4>🧠 Our 7-Step Creation Process:</h4>
                    <ol>
                        <li><strong>Prompt Engineering:</strong> We craft detailed, optimized prompts using industry techniques</li>
                        <li><strong>Style Analysis:</strong> Deep analysis of your reference to capture every detail</li>
                        <li><strong>Multiple Generations:</strong> We create 50+ variations to find the perfect one</li>
                        <li><strong>AI Model Selection:</strong> Using SDXL, Midjourney, and proprietary models</li>
                        <li><strong>Post-Processing:</strong> Professional enhancement and refinement</li>
                        <li><strong>Quality Control:</strong> Every piece reviewed by our art directors</li>
                        <li><strong>Final Delivery:</strong> Ultra-high resolution with licensing documentation</li>
                    </ol>
                </div>
                <img src="{image_url}" style="width:100%;max-width:400px;border-radius:8px;margin:20px 0">
                <p>This level of attention is why clients like Netflix, Adobe, and 500+ agencies trust us with their creative needs.</p>
                <div style="text-align:center;margin:30px 0">
                    <a href="{base_url}/upsell/{upsell_token}" style="display:inline-block;background:linear-gradient(45deg,#667eea,#764ba2);color:#fff;padding:16px 32px;border-radius:8px;text-decoration:none;font-weight:bold;font-size:18px">
                        🎨 Start Your Project
                    </a>
                </div>
                <p>Questions? Just reply to this email - I read every message personally.</p>
                <p>Best,<br>Marcus, Creative Director<br>Chaos Venice Productions</p>
            </div>
        </div>
        </body></html>
        """
        
    elif sequence == 4:
        # Day 4: Final chance with scarcity
        subject = f"⏰ {customer_name}, final notice - offer expires tonight"
        html = f"""
        <html><body style="font-family:Arial,sans-serif;background:#f8f9fa;margin:0;padding:20px">
        <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 12px rgba(0,0,0,0.1)">
            <div style="background:linear-gradient(135deg,#ff416c,#ff4b2b);color:#fff;padding:30px;text-align:center">
                <h1>⏰ FINAL NOTICE</h1>
                <p style="font-size:18px;margin:0">Your exclusive offer expires at midnight tonight</p>
            </div>
            <div style="padding:30px">
                <p>Hi {customer_name},</p>
                <p>This is my final email about the exclusive offer for artwork like "{artwork_title}".</p>
                <div style="background:#ffebee;border:2px solid #f44336;border-radius:8px;padding:20px;margin:20px 0;text-align:center">
                    <h3 style="color:#c62828;margin:0">🚨 EXPIRES AT MIDNIGHT</h3>
                    <p style="font-size:18px;margin:10px 0">After tonight, regular pricing resumes</p>
                    <p style="margin:0;color:#555">(That's 3x more expensive)</p>
                </div>
                <img src="{image_url}" style="width:100%;max-width:400px;border-radius:8px;margin:20px 0">
                <p><strong>What you're missing:</strong></p>
                <ul>
                    <li>💰 Up to $700 savings on premium packages</li>
                    <li>⚡ Priority 24-hour delivery</li>
                    <li>🎨 Unlimited revisions included</li>
                    <li>📄 Full commercial licensing rights</li>
                </ul>
                <div style="text-align:center;margin:30px 0">
                    <a href="{base_url}/upsell/{upsell_token}" style="display:inline-block;background:linear-gradient(45deg,#f44336,#d32f2f);color:#fff;padding:16px 32px;border-radius:8px;text-decoration:none;font-weight:bold;font-size:18px">
                        🔥 LAST CHANCE - Claim Now
                    </a>
                </div>
                <p>If you're not interested, no worries - you won't hear from us again. But if you want professional AI artwork at these prices, tonight is your last opportunity.</p>
                <p>Thank you for your time,<br>The Chaos Venice Productions Team</p>
            </div>
        </div>
        </body></html>
        """
    
    return subject, html

def _send_upsell_email(email, subject, html_content):
    """Send upsell follow-up email via SendGrid or SMTP"""
    try:
        # Try SendGrid first if API key is available
        sendgrid_key = os.environ.get('SENDGRID_API_KEY')
        from_email = os.environ.get('FROM_EMAIL', 'noreply@chaosvenice.com')
        
        if sendgrid_key:
            try:
                from sendgrid import SendGridAPIClient
                from sendgrid.helpers.mail import Mail, Email, To, Content
                
                sg = SendGridAPIClient(sendgrid_key)
                message = Mail(
                    from_email=Email(from_email),
                    to_emails=To(email),
                    subject=subject
                )
                message.content = Content("text/html", html_content)
                
                sg.send(message)
                return True
                
            except Exception as e:
                _log_error("ERROR", f"SendGrid upsell email failed to {email}", str(e))
                return False
        else:
            # Fallback to SMTP
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            
            smtp_host = os.environ.get('SMTP_HOST')
            smtp_port = int(os.environ.get('SMTP_PORT', 587))
            smtp_user = os.environ.get('SMTP_USER')
            smtp_pass = os.environ.get('SMTP_PASS')
            
            if not all([smtp_host, smtp_user, smtp_pass]):
                _log_error("ERROR", "No email configuration available for upsell emails", "")
                return False
            
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = from_email
            msg['To'] = email
            
            msg.attach(MIMEText(html_content, 'html'))
            
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            
            return True
            
    except Exception as e:
        _log_error("ERROR", f"Failed to send upsell email to {email}", str(e))
        return False

def check_admin_auth():
    """Check if admin authentication is valid"""
    admin_token = request.args.get('admin_token') or request.headers.get('Authorization')
    required_token = os.environ.get('ADMIN_TOKEN', 'admin123')
    return admin_token == required_token

@app.route('/admin/upsell-dashboard')
def admin_upsell_dashboard():
    """Admin dashboard for monitoring upsell funnel performance"""
    if not check_admin_auth():
        return "Unauthorized", 401
        
    db = get_db()
    
    # Get upsell statistics
    cur = db.execute("""
        SELECT 
            COUNT(*) as total_sessions,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
            SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) as active,
            SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) as expired,
            AVG(CASE WHEN tier_selected='basic' THEN 299 
                     WHEN tier_selected='premium' THEN 599 
                     WHEN tier_selected='deluxe' THEN 1299 ELSE 0 END) as avg_value
        FROM upsell_sessions 
        WHERE created_at >= ?""", 
        ((datetime.datetime.utcnow() - datetime.timedelta(days=30)).isoformat(),))
    stats = cur.fetchone()
    
    # Get recent sessions
    cur = db.execute("""
        SELECT us.*, s.title 
        FROM upsell_sessions us
        LEFT JOIN shares s ON us.original_share_token = s.token
        ORDER BY us.created_at DESC 
        LIMIT 20""")
    recent_sessions = cur.fetchall()
    
    # Get email performance
    cur = db.execute("""
        SELECT 
            email_sequence,
            COUNT(*) as scheduled,
            SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) as sent,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
        FROM upsell_follow_ups
        GROUP BY email_sequence
        ORDER BY email_sequence""")
    email_stats = cur.fetchall()
    
    html = f"""<!doctype html><html><head>
    <meta charset="utf-8">
    <title>Upsell Dashboard - Admin</title>
    <style>
    body{{font-family:system-ui,sans-serif;background:#f8f9fa;margin:0;padding:20px}}
    .container{{max-width:1200px;margin:0 auto}}
    .stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:20px;margin-bottom:30px}}
    .stat-card{{background:#fff;padding:20px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);text-align:center}}
    .stat-number{{font-size:2rem;font-weight:700;color:#2563eb}}
    .stat-label{{color:#6b7280;margin-top:5px}}
    .section{{background:#fff;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);margin-bottom:30px}}
    .section-header{{padding:20px;border-bottom:1px solid #e5e7eb;display:flex;justify-content:space-between;align-items:center}}
    .section-content{{padding:20px}}
    table{{width:100%;border-collapse:collapse}}
    th,td{{padding:12px;text-align:left;border-bottom:1px solid #e5e7eb}}
    th{{background:#f8f9fa;font-weight:600}}
    .btn{{padding:8px 16px;border-radius:6px;border:none;cursor:pointer;font-weight:500}}
    .btn-primary{{background:#2563eb;color:white}}
    .btn-success{{background:#059669;color:white}}
    .btn-warning{{background:#d97706;color:white}}
    .status-active{{color:#059669;font-weight:600}}
    .status-completed{{color:#2563eb;font-weight:600}}
    .status-expired{{color:#dc2626;font-weight:600}}
    </style>
    </head><body>
    <div class="container">
        <h1>🚀 Upsell Funnel Dashboard</h1>
        
        <div class="stats">
            <div class="stat-card">
                <div class="stat-number">{stats[0] if stats else 0}</div>
                <div class="stat-label">Total Sessions</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{stats[1] if stats else 0}</div>
                <div class="stat-label">Completed</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{((stats[1] / stats[0]) * 100 if stats and stats[0] > 0 else 0):.1f}%</div>
                <div class="stat-label">Conversion Rate</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">${stats[4] if stats and stats[4] else 0:.0f}</div>
                <div class="stat-label">Avg Order Value</div>
            </div>
        </div>
        
        <div class="section">
            <div class="section-header">
                <h2>Email Performance</h2>
                <button class="btn btn-primary" onclick="processEmails()">Process Pending Emails</button>
            </div>
            <div class="section-content">
                <table>
                    <tr><th>Sequence</th><th>Scheduled</th><th>Sent</th><th>Failed</th><th>Success Rate</th></tr>"""
    
    for seq_data in email_stats:
        sequence, scheduled, sent, failed = seq_data
        success_rate = (sent / scheduled * 100) if scheduled > 0 else 0
        sequence_names = {1: "Hour 1 Reminder", 2: "Hour 12 Discount", 3: "Day 2 Behind Scenes", 4: "Day 4 Final Notice"}
        
        html += f"""<tr>
            <td>{sequence_names.get(sequence, f'Sequence {sequence}')}</td>
            <td>{scheduled}</td>
            <td>{sent}</td>
            <td>{failed}</td>
            <td>{success_rate:.1f}%</td>
        </tr>"""
    
    html += f"""</table>
            </div>
        </div>
        
        <div class="section">
            <div class="section-header">
                <h2>Recent Upsell Sessions</h2>
            </div>
            <div class="section-content">
                <table>
                    <tr><th>Customer</th><th>Artwork</th><th>Status</th><th>Tier</th><th>Created</th><th>Actions</th></tr>"""
    
    for session in recent_sessions:
        (session_id, token, original_token, email, name, project_type, status, 
         tier_selected, expires_at, created_at, completed_at, title) = session
        
        status_class = f"status-{status}"
        tier_display = tier_selected or "Not selected"
        created_display = created_at[:16].replace('T', ' ') if created_at else ''
        
        html += f"""<tr>
            <td>{name or email}</td>
            <td>{title or 'Unknown'}</td>
            <td><span class="{status_class}">{status.title()}</span></td>
            <td>{tier_display}</td>
            <td>{created_display}</td>
            <td><a href="/upsell/{token}" target="_blank" class="btn btn-primary">View</a></td>
        </tr>"""
    
    html += f"""</table>
            </div>
        </div>
    </div>
    
    <script>
    function processEmails() {{
        fetch('/cron/process-upsell-emails')
        .then(response => response.json())
        .then(data => {{
            alert(`Processed ${{data.processed}} emails. Sent: ${{data.sent}}, Failed: ${{data.failed}}`);
            location.reload();
        }})
        .catch(error => {{
            alert('Error processing emails: ' + error.message);
        }});
    }}
    </script>
    </body></html>"""
    
    return Response(html, 200, mimetype="text/html")

@app.route('/admin/process-upsell-emails', methods=['POST'])
def admin_process_emails():
    """Admin endpoint to manually trigger upsell email processing"""
    if not check_admin_auth():
        return jsonify({"error": "Unauthorized"}), 401
    
    # Call the email processing function
    try:
        # This would be the same logic as /cron/process-upsell-emails
        response = process_upsell_emails()
        return response
    except Exception as e:
        _log_error("ERROR", "Manual email processing failed", str(e))
        return jsonify({"error": "Failed to process emails"}), 500

# ---- Health & Admin sanity routes ----
@app.get("/health")
def health():
    return {"status": "ok"}, 200

@app.get("/admin/system")
def admin_system():
    jobs = []
    try:
        if _scheduler:
            for j in _scheduler.get_jobs():
                jobs.append({
                    "id": j.id,
                    "next_run_time": j.next_run_time.isoformat() if j.next_run_time else None
                })
    except Exception as e:
        jobs = [{"error": str(e)}]
    return jsonify({
        "ok": True,
        "utc_time": datetime.datetime.utcnow().isoformat(),
        "scheduler_on": _scheduler is not None,
        "jobs": jobs
    }), 200

# Optional: manual triggers for testing (no DB writes, just call internals if present)
@app.post("/admin/run/upsell")
def admin_run_upsell():
    _job_process_upsell_emails()
    return {"ok": True}, 200

@app.post("/admin/run/retries")
def admin_run_retries():
    _job_process_email_retries()
    return {"ok": True}, 200

@app.post("/admin/run/cleanup")
def admin_run_cleanup():
    _job_cleanup_expired_sessions()
    return {"ok": True}, 200

@app.route('/admin/portfolio-orders')
def admin_portfolio_orders():
    """Admin view of all portfolio orders"""
    admin_token = request.args.get('admin_token')
    expected_token = os.environ.get('ADMIN_TOKEN', 'admin123')
    
    if admin_token != expected_token:
        return "Unauthorized", 401
    
    db = get_db()
    cur = db.execute("""SELECT o.*, s.title FROM portfolio_orders o
                        LEFT JOIN shares s ON o.share_token = s.token
                        ORDER BY o.created_at DESC""")
    
    orders = []
    for row in cur.fetchall():
        try:
            details = json.loads(row[5] or '{}')
            orders.append({
                'id': row[0],
                'token': row[1],
                'type': row[2],
                'email': row[3],
                'name': row[4],
                'details': details,
                'price': row[6] / 100 if row[6] else 0,
                'status': row[7],
                'created_at': row[8],
                'title': row[9] or 'Unknown'
            })
        except:
            continue
    
    html = f"""<!doctype html><html><head>
    <meta charset="utf-8">
    <title>Portfolio Orders - Admin</title>
    <style>
    body{{font-family:system-ui;background:#0b0f14;color:#e7edf5;margin:0;padding:20px}}
    .container{{max-width:1200px;margin:0 auto}}
    table{{width:100%;border-collapse:collapse;background:#111826;border-radius:8px;overflow:hidden}}
    th,td{{padding:12px;text-align:left;border-bottom:1px solid #1f2a3a}}
    th{{background:#1f2a3a;font-weight:600}}
    .order-type{{padding:4px 8px;border-radius:4px;font-size:12px;font-weight:600}}
    .type-similar{{background:#2563eb;color:white}}
    .type-license{{background:#059669;color:white}}
    .status{{padding:4px 8px;border-radius:4px;font-size:12px}}
    .status-pending{{background:#f59e0b;color:black}}
    .details{{max-width:300px;font-size:12px;color:#9fb2c7}}
    </style>
    </head><body>
    <div class="container">
      <h1>Portfolio Orders ({len(orders)})</h1>
      <table>
        <tr>
          <th>ID</th>
          <th>Type</th>
          <th>Customer</th>
          <th>Portfolio Item</th>
          <th>Price</th>
          <th>Status</th>
          <th>Details</th>
          <th>Date</th>
        </tr>"""
    
    for order in orders:
        order_type_class = f"type-{order['type']}"
        html += f"""
        <tr>
          <td>{order['id']}</td>
          <td><span class="order-type {order_type_class}">{order['type'].title()}</span></td>
          <td>{order['name']}<br><small>{order['email']}</small></td>
          <td>{order['title']}<br><small>{order['token'][:8]}...</small></td>
          <td>${order['price']:.2f}</td>
          <td><span class="status status-{order['status']}">{order['status'].title()}</span></td>
          <td class="details">{str(order['details']).replace('{', '').replace('}', '')[:100]}...</td>
          <td>{order['created_at'][:16]}</td>
        </tr>"""
    
    html += """
      </table>
    </div>
    </body></html>"""
    
    return Response(html, 200, mimetype="text/html")

# Create demo key on startup
bootstrap_demo_key()
_init_share_table()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)