# One image, two roles. Pick the role with the start command:
#   web:     gunicorn (default CMD)
#   worker:  python worker.py
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 10001 omixa
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN mkdir -p /app/temp /app/data && chown -R omixa:omixa /app/temp /app/data
USER omixa
EXPOSE 8080
# Stateless API. gthread workers: I/O-bound requests only; cleaning is done by worker.py.
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --workers ${WEB_CONCURRENCY:-3} --threads ${WEB_THREADS:-8} --worker-class gthread --timeout ${GUNICORN_TIMEOUT:-120} --graceful-timeout 30 --keep-alive 5 --max-requests 2000 --max-requests-jitter 200"]
