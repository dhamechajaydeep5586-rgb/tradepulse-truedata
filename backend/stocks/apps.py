import sys

from django.apps import AppConfig


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
