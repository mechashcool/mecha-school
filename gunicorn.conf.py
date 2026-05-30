import os


bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
workers = int(os.environ.get('WEB_CONCURRENCY', 2))
threads = int(os.environ.get('GUNICORN_THREADS', 2))
timeout = int(os.environ.get('GUNICORN_TIMEOUT', 120))

accesslog = '-'
errorlog = '-'
preload_app = False

max_requests = int(os.environ.get('GUNICORN_MAX_REQUESTS', 500))
max_requests_jitter = int(os.environ.get('GUNICORN_MAX_REQUESTS_JITTER', 50))
