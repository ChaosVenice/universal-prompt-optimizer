import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = 1            # IMPORTANT on Replit so the scheduler runs once
worker_class = "gthread"
threads = 16
timeout = 120
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = "info"