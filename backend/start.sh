#!/usr/bin/env bash
set -o errexit

# Try multiple known paths for Render's Python environment
for PYTHON in \
    /opt/render/project/src/.venv/bin/python \
    /opt/render/project/.venv/bin/python \
    "$(which python3 2>/dev/null)" \
    "$(which python 2>/dev/null)"; do
    if [ -x "$PYTHON" ]; then
        echo "==> Using Python: $PYTHON"
        # Run database migrations at runtime (when DB is reachable)
        $PYTHON manage.py migrate --no-input
        # Recreate any missing tables dynamically
        $PYTHON manage.py recreate_missing_tables
        # Use dynamic PORT from Render, default to 10000 if not set
        PORT="${PORT:-10000}"
        # NOTE: --workers must stay at 1. This app starts an in-process APScheduler
        # and logs into the TrueData session inside apps.py's AppConfig.ready(),
        # which runs once per OS process — extra worker *processes* would each start
        # their own scheduler (duplicate Telegram alerts, duplicate scheduled scans)
        # and their own competing TrueData login (only one WebSocket session per
        # login is allowed — a second connection attempt gets "User Already
        # Connected"). Use threads instead for request concurrency, which share the
        # same process/scheduler/session.
        # NOTE: --timeout 300 (not 60). The in-process APScheduler runs
        # run_periodic_scanners every 15 min, which walks ~500 symbols one-by-one
        # via REST (documented in live_signal_service.py as "taking many minutes").
        # That job runs in this same worker process, so a 60s timeout caused gunicorn's
        # arbiter to SIGKILL the worker mid-scan (logged as "WORKER TIMEOUT" / "perhaps
        # out of memory?" — that message is generic for any timeout-triggered kill, not
        # proof of an actual OOM).
        exec $PYTHON -m gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 4 --timeout 300
    fi
done

echo "==> ERROR: No Python found"
exit 1
