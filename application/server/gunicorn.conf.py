"""Gunicorn settings, picked up automatically when gunicorn runs in this folder:

    gunicorn server:app

Every setting can be overridden with an environment variable.
"""

import os

bind = os.environ.get("BIND", "0.0.0.0:8000")
# SQLite handles one writer at a time, so a single process with a few threads
# is plenty; more workers would only wait on each other's locks.
workers = int(os.environ.get("WORKERS", "1"))
threads = int(os.environ.get("THREADS", "8"))
# A device's first sync can upload or download its whole database
timeout = int(os.environ.get("TIMEOUT", "120"))
accesslog = "-"
errorlog = "-"
