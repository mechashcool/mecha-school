FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy project
COPY . .

# Create upload directory
RUN mkdir -p app/static/uploads

# Non-root user
RUN adduser --disabled-password --gecos '' appuser && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

# Entrypoint. Runtime details live in gunicorn.conf.py and can be tuned with
# WEB_CONCURRENCY, GUNICORN_THREADS, and PORT on Render.
CMD ["gunicorn", "wsgi:application"]
