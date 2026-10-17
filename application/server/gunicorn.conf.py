"""Gunicorn settings, picked up automatically when gunicorn runs in this folder:

    gunicorn server:app

Every setting can be overridden with an environment variable. LAN_DISCOVERY=0
stops devices on the same wifi from finding the server on their own.
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


def when_ready(_arbiter):
    """Once listening, let devices on the same wifi find the server (LAN_DISCOVERY=0
    turns it off). Runs in the main gunicorn process, so it answers once however
    many workers there are."""
    from server import start_lan_discovery
    try:
        port = int(bind.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        print(f"Wifi discovery is off: can't tell the port from BIND={bind!r}")
        return
    start_lan_discovery(port)
