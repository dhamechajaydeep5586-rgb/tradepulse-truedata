#!/usr/bin/env bash
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt

# Debug: confirm gunicorn is installed and where
echo "==> Python: $(which python || which python3)"
echo "==> Gunicorn check:"
python -m gunicorn --version || python3 -m gunicorn --version || echo "gunicorn NOT found"
echo "==> pip show gunicorn:"
pip show gunicorn || echo "gunicorn not in pip"

python manage.py collectstatic --no-input
python manage.py migrate --no-input
python manage.py recreate_missing_tables

