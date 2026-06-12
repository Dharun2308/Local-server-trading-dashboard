"""Smoke-test runner: app on :8091 with HTTP-wire logging to a session log.

Enables DEBUG logging for urllib3 so every outbound API call (method + path)
lands in the log file — the post-run grep over that file is the proof that no
order endpoint was called during the smoke test.
"""
import logging
import os
import runpy
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.environ.get("SMOKE_LOG", "/tmp/smoke_session.log")

logging.basicConfig(
    filename=LOG,
    level=logging.DEBUG,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logging.getLogger("urllib3").setLevel(logging.DEBUG)

os.environ.setdefault("DASHBOARD_PORT", "8091")
sys.path.insert(0, BASE)
os.chdir(BASE)
runpy.run_path(os.path.join(BASE, "app.py"), run_name="__main__")
