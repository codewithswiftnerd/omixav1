web: gunicorn app:app --bind 0.0.0.0:$PORT --workers ${WEB_CONCURRENCY:-3} --threads ${WEB_THREADS:-8} --worker-class gthread --timeout ${GUNICORN_TIMEOUT:-120} --graceful-timeout 30 --keep-alive 5 --max-requests 2000 --max-requests-jitter 200 --access-logfile - --error-logfile -
worker: python worker.py
