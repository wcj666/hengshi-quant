"""LaunchAgent-safe loopback-only WSGI server for the local trading dashboard.

No broker connection; this is a research dashboard, not an exchange-real-time service.
"""
from __future__ import annotations
from waitress import serve
from web_dashboard import create_app
from paper_runtime import start_quote_worker
from paper_contest import DEFAULT_STORE

if __name__ == '__main__':
    print('Local research dashboard at http://127.0.0.1:8765/ (127.0.0.1 only)', flush=True)
    stop = start_quote_worker(DEFAULT_STORE)
    try:
        serve(create_app(), host='127.0.0.1', port=8765, threads=4, connection_limit=50)
    finally:
        stop.set()
