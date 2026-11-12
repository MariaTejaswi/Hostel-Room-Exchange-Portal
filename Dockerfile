# Multi-stage Dockerfile for RoomLoop Platform
FROM python:3.12-slim as base

# Python environment settings
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH="/app"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements/ /app/requirements/
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements/production.txt

# Copy application source code
COPY . /app/

# Create media & static directories
RUN mkdir -p /app/staticfiles /app/media

# Collect static files for production
RUN python manage.py collectstatic --noinput || true

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
