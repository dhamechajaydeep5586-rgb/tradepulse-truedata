#!/usr/bin/env bash
# Start backend (Django) + frontend (Vite) together in one terminal.
# Usage: ./dev.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"

cleanup() {
    echo ""
    echo "==> Stopping servers..."
    lsof -ti :8000 | xargs -r kill -9 2>/dev/null || true
    jobs -p | xargs -r kill -9 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Free up port 8000 in case a previous run left it occupied
lsof -ti :8000 | xargs -r kill -9 2>/dev/null || true

echo "==> Starting backend (Django) on :8000"
(
    cd "$BACKEND_DIR"
    source venv/bin/activate
    python manage.py runserver 0.0.0.0:8000
) 2>&1 | sed -u 's/^/[backend] /' &

echo "==> Starting frontend (Vite)"
(
    cd "$FRONTEND_DIR"
    npm run dev
) 2>&1 | sed -u 's/^/[frontend] /' &

wait
