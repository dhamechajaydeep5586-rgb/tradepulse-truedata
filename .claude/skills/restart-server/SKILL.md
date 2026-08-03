---
name: restart-server
description: Restart the TradePulse AI Django backend dev server (kill port 8000, reactivate venv, runserver)
---

Kill the existing process on port 8000, then restart cleanly:

```bash
lsof -ti :8000 | xargs kill -9 2>/dev/null
cd /home/jd/tradeplusai/tradepulse-ai/backend
source venv/bin/activate
python manage.py runserver
```
