"""Loads the app's database module (application/user_app/database.py) for the tests.

The mesh syncs the app's database, so the tests run against the real thing.
Uses the file in this checkout if it's there; otherwise (on a branch that
doesn't contain the app yet) reads it from the main branch with git. Never
modifies the app's code.
"""
import importlib.util
import os
import subprocess
import tempfile

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
APP_DB = "application/user_app/database.py"


def _load():
    path = os.path.join(REPO, APP_DB)
    if not os.path.exists(path):
        try:
            src = subprocess.run(["git", "show", f"main:{APP_DB}"], cwd=REPO, capture_output=True,
                                 check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return None
        path = os.path.join(tempfile.mkdtemp(prefix="trailmesh-appdb-"), "database.py")
        with open(path, "wb") as f:
            f.write(src)
    spec = importlib.util.spec_from_file_location("app_database", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


database = _load()


def new_db(directory, name, device_id):
    """A fresh app database with its identity set up."""
    db = database.Database(os.path.join(directory, f"{name}.db"))
    db.init_device(name, device_id)
    return db
