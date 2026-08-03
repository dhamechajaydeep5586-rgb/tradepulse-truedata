#!/usr/bin/env bash
# Run ON THE VM to pull latest code and restart the backend.
set -euo pipefail

cd "$HOME/tradepulse-truedata"
git pull
cd backend
./venv/bin/pip install -r requirements.txt
./venv/bin/python manage.py migrate --no-input
./venv/bin/python manage.py recreate_missing_tables
sudo systemctl restart tradepulse-backend
sudo systemctl status tradepulse-backend --no-pager
