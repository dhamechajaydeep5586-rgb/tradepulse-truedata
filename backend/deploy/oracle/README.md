# Deploying the backend to Oracle Cloud Free Tier

The Django backend already reads `DATABASE_URL` via `dj-database-url`, so any
PostgreSQL instance works — point it at whatever DB you're using in
production.

## 1. Create the VM (OCI Console — do this manually)

1. Compute → Instances → Create Instance.
2. Image: **Ubuntu 24.04**. Shape: **VM.Standard.A1.Flex** (Ampere, up to
   4 OCPU / 24GB free) — fall back to `VM.Standard.E2.1.Micro` if A1 capacity
   isn't available in your region.
3. Assign a public IPv4 address. Generate/download an SSH key pair.
4. In the VCN's **Security List**, add ingress rules for TCP 80 and 443 from
   `0.0.0.0/0`. (Oracle blocks everything but 22 at the network layer by
   default — this step is easy to miss.)

## 2. Provision the VM

```bash
scp -i your-key.pem setup.sh ubuntu@<VM_IP>:~/
ssh -i your-key.pem ubuntu@<VM_IP>
chmod +x setup.sh && ./setup.sh
```

This installs Python 3.13, nginx, certbot, clones the repo, creates the
venv, and installs (but does not start) the `tradepulse-backend` systemd
service and nginx site.

## 3. Environment variables

Create `/home/ubuntu/tradepulse-truedata/backend/.env` on the VM with all the
vars listed in `doc/setup_guide.md` — `DATABASE_URL` (or the `DB_*` vars),
`SECRET_KEY`, `TRUEDATA_USERNAME`/`TRUEDATA_PASSWORD`, WhatsApp/telegram
tokens, etc.

Caveat: `systemd`'s `EnvironmentFile` is stricter than `python-dotenv` —
plain `KEY=VALUE` per line, no `export` prefix, and quote any value
containing spaces or `#`.

Also set in `.env`:
- `ALLOWED_HOSTS` — the VM's domain/IP.
- `CORS_ALLOWED_ORIGINS` / `CSRF_TRUSTED_ORIGINS` — the frontend's real URL,
  once you know where it's hosted.

## 4. Start it

```bash
sudo systemctl start tradepulse-backend
sudo systemctl status tradepulse-backend
```

Logs: `backend/logs/gunicorn-*.log`, or `journalctl -u tradepulse-backend -f`.

## 5. HTTPS

Edit `/etc/nginx/sites-available/tradepulse-backend`, set `server_name` to
your real domain, then:

```bash
sudo certbot --nginx -d api.yourdomain.com
```

If you don't have a domain yet, the frontend can hit the bare IP over HTTP
temporarily, but get a domain + TLS before going live — mixed content
(https frontend → http backend) will be blocked by browsers anyway.

## 6. Go live

1. Point the frontend's API base URL at this backend's URL, deploy the
   frontend wherever you've decided to host it.
2. Confirm signals/market-status/websocket flows work end-to-end against
   this backend (see project CLAUDE.md for the signal lifecycle to
   sanity-check).

## Redeploys going forward

```bash
ssh -i your-key.pem ubuntu@<VM_IP>
./tradepulse-truedata/backend/deploy/oracle/redeploy.sh
```

## APScheduler note

The backend starts `apscheduler` in-process from `AppConfig.ready()`
(`backend/stocks/apps.py` → `updater.py`), guarded only by `RUN_MAIN` (a
Django dev-server check) — in production it starts unconditionally in
**every** gunicorn worker process.

The systemd unit here uses `--workers 1` on purpose — do not raise it
without first moving the scheduler out of the request-serving process, or
you'll get duplicate scans and duplicate signals per the stale signal guard /
signal cap logic described in the project's CLAUDE.md.
