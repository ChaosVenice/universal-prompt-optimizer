# Dockerfile — Render-friendly Flask + Gunicorn
FROM python:3.11-slim

# System setup
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the rest of the app
COPY . .

# Render provides $PORT; default to 8000 for local runs
EXPOSE 8000

# Start with gunicorn serving the Flask app object "app" in app.py
# Threads help on Render; workers=2 is a safe default for small instances.
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT:-8000} -k gthread --threads 8 --workers 2 app:app"]
