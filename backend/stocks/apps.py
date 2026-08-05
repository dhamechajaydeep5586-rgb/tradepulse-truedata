import os
import sys

from django.apps import AppConfig


def _assert_single_gunicorn_worker():
    """Raise if gunicorn was invoked with more than one worker process.

    Audit fix H1. Workers are forked children of the gunicorn master and inherit its
    argv unchanged (no re-exec), so `sys.argv` still shows the original `--workers`/`-w`
    flag inside this worker process — the same assumption the C8 gunicorn-detection
    check above already relies on. `WEB_CONCURRENCY` is gunicorn's own env-var DEFAULT
    for worker count, used only when `--workers`/`-w` is NOT passed on the command
    line — NOT an override.

    Bug fix (found in a follow-up audit, verified against the real installed
    gunicorn package): this used to let WEB_CONCURRENCY unconditionally overwrite
    an explicit --workers value, which is gunicorn's precedence backwards — real
    gunicorn always lets an explicit CLI flag win. Both this app's deploy targets
    hardcode `--workers 1` on the command line, so with the old (wrong) precedence,
    WEB_CONCURRENCY being set to anything else for ANY unrelated reason (a hosting
    platform default, a leftover env var from an experiment) made this guard raise
    on every single boot even though the real gunicorn invocation was completely
    safe — a false-positive crash loop. Now only consulted when no explicit CLI
    flag was found, matching gunicorn's actual behavior.
    """
    workers = 1
    explicit_cli_workers = False
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg in ("--workers", "-w") and i + 1 < len(argv):
            try:
                workers = int(argv[i + 1])
                explicit_cli_workers = True
            except ValueError:
                pass
            break
        if arg.startswith("--workers="):
            try:
                workers = int(arg.split("=", 1)[1])
                explicit_cli_workers = True
            except ValueError:
                pass
            break

    if not explicit_cli_workers:
        web_concurrency = os.environ.get("WEB_CONCURRENCY")
        if web_concurrency:
            try:
                workers = int(web_concurrency)
            except ValueError:
                pass

    if workers > 1:
        raise RuntimeError(
            f"[SINGLE_WORKER_GUARD] gunicorn is configured for {workers} workers, but this "
            "app requires exactly 1 — the REST rate-limit lock, TrueData's one-session-per-"
            "login limit, the in-process APScheduler, and every FileBasedCache-backed "
            "cooldown/halt key all assume a single process. Set --workers 1 (or unset "
            "WEB_CONCURRENCY / set it to 1) and use --worker-class gthread --threads N for "
            "request concurrency instead."
        )


class StocksConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'stocks'

    def ready(self):
        # 0. Test-mode guard: `manage.py test` (and CI running the same command)
        # must never start the APScheduler background jobs or log into the live
        # TrueData session as a side effect of importing the app config.
        # Mirrors the same `'test' in sys.argv` check `config/settings.py` already
        # uses to swap DATABASES to a throwaway sqlite file under the test runner —
        # see that file's comment for why ad hoc side effects here are unsafe.
        if 'test' in sys.argv:
            return

        # 0b. Real-server-process guard (audit fix C8): every OTHER management
        # command — makemigrations, shell, dbshell, createsuperuser,
        # showmigrations, etc. — used to unconditionally log into the live
        # TrueData session as a side effect of Django starting up, tripping
        # TrueData's one-session-per-login limit and knocking the real feed
        # offline. Reproduced directly: a harmless
        # `python manage.py makemigrations --check --dry-run` run against a live
        # server produced `{'success': False, 'message': 'User Already
        # Connected', ...}` followed by "Connection to remote host was lost".
        # `gunicorn` covers both deploy targets (Render's start.sh and Oracle's
        # systemd unit both exec gunicorn directly — neither sets a shared env
        # marker, so detecting the process itself is the only signal that works
        # for both); `runserver` covers local dev, gated the same way
        # updater.start() already gates itself against the autoreloader's
        # watcher/child split.
        is_gunicorn = 'gunicorn' in sys.argv[0].lower()
        is_runserver = 'runserver' in sys.argv
        is_dev_main = os.environ.get('RUN_MAIN') == 'true'
        is_noreload = '--noreload' in sys.argv
        if not is_gunicorn and not (is_runserver and (is_dev_main or is_noreload)):
            return

        # 0c. Single-worker-process assertion (audit fix H1). The REST rate-limit lock
        # (a plain threading.Lock — coordinates threads within one process, nothing
        # across processes), the TrueData WS single-session-per-login limit, the
        # APScheduler dedup this file already relies on, and every FileBasedCache-backed
        # cooldown/halt key all silently assume exactly one worker process. That
        # assumption was previously enforced only by a comment next to `--workers 1` in
        # start.sh / the Oracle systemd unit — a config change with zero code-level
        # guard. Fail loudly and immediately instead of degrading silently (duplicate
        # scheduled scans, duplicate Telegram alerts, competing TrueData logins).
        if is_gunicorn:
            _assert_single_gunicorn_worker()

        # 1. Start the background scheduler (Always run, even on holidays)
        try:
            import stocks.updater as updater
            updater.start()
        except ImportError:
            pass

        # 2. Total Silence Check for Market Tasks (Static)
        from stocks.services.signal_utils import is_static_closed
        if is_static_closed("NSE"):
            # Skip heavy market initialization on holidays
            return

        # 2. Initialize TrueData
        try:
            from django.conf import settings
            from stocks.services import truedata_service

            if not truedata_service.is_truedata_ready():
                config = getattr(settings, "TRUEDATA", {})
                truedata_service.initialize_truedata(
                    username=config.get("USERNAME"),
                    password=config.get("PASSWORD"),
                )
        except Exception:
            pass
