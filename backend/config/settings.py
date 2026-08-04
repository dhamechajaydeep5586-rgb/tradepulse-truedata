"""
Django settings for config project (TradePulse AI).
"""

import os
import sys
import dj_database_url
from pathlib import Path
from datetime import timedelta
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# ──────────────────────────────────────────────
# CACHE
# ──────────────────────────────────────────────
# File-based so cached data (FII/DII, free-swing scans, etc.) survives a
# gunicorn worker restart — the default LocMemCache is per-process and goes
# cold every time a worker is killed/re-spawned, forcing an immediate re-hit
# of slow/rate-limited third-party APIs (NSE).
# Tests get an isolated in-memory cache instead (same 'test' in sys.argv
# technique as DATABASES below) — otherwise a test run reads/writes the same
# on-disk cache directory as a real dev/prod process, so results can depend on
# stale market data left over from an actual run.
if 'test' in sys.argv:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
else:
    # Audit fix M14 (documentation): every cooldown/halt/lock cache key in this
    # codebase (scan-rate guards, DAILY_HALT_CACHE_KEY, OPTION_BUYING_DAILY_HALT_
    # CACHE_KEY, specialist scanner throttles, etc.) depends on this single
    # on-disk FileBasedCache with zero cross-host coordination. That's correct and
    # intentional for the current single-instance deploy (see H1's single-worker
    # guard in stocks/apps.py — this app is deliberately one process on one host).
    # The moment a second host is ever added for redundancy, halt state and scan
    # cooldowns stop being shared between them — each host would independently
    # believe it owns the only copy of every lock/cooldown. Move this to Redis
    # (or another shared cache backend) BEFORE horizontally scaling past one host;
    # do not treat this as a drop-in swap without re-auditing every cache-key user
    # for the same single-instance assumption.
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": os.getenv("DJANGO_CACHE_DIR", str(BASE_DIR / "django_cache")),
        }
    }

# ──────────────────────────────────────────────
# SECURITY
# ──────────────────────────────────────────────
DEBUG = os.getenv('DEBUG', 'False').lower() in ('true', '1', 'yes')

# No hardcoded fallback in production (same fail-closed pattern as
# CRON_SECRET_TOKEN in stocks/views.py): a source-committed default here would
# let anyone who has seen this repo forge session/CSRF/JWT tokens (SIMPLE_JWT
# below has no explicit SIGNING_KEY, so it signs with SECRET_KEY too) for any
# missing-env-var deploy mistake. Dev/test keep the fallback so local runs and
# CI without SECRET_KEY set don't break.
if os.getenv('SECRET_KEY'):
    SECRET_KEY = os.environ['SECRET_KEY']
elif DEBUG or 'test' in sys.argv:
    SECRET_KEY = 'change-me-in-production'
else:
    raise RuntimeError(
        "SECRET_KEY environment variable is not set. Refusing to start with a "
        "known, source-committed fallback in production — see CRON_SECRET_TOKEN "
        "in stocks/views.py for the same fail-closed pattern applied elsewhere."
    )

ALLOWED_HOSTS = os.getenv(
    'ALLOWED_HOSTS',
    'localhost,127.0.0.1',
).split(',')

# ──────────────────────────────────────────────
# APPLICATION DEFINITION
# ──────────────────────────────────────────────
INSTALLED_APPS = [
    # Django core
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',

    # Local apps
    'users.apps.UsersConfig',
    'stocks.apps.StocksConfig',
    'global_market.apps.GlobalMarketConfig',
    'insights.apps.InsightsConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',     # WhiteNoise — must be here
    'corsheaders.middleware.CorsMiddleware',          # CORS — must be before CommonMiddleware
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# ──────────────────────────────────────────────
# DATABASE — PostgreSQL
# ──────────────────────────────────────────────
# Standard Django DB Config
# Uses dj-database-url so a single DATABASE_URL env var configures the connection anywhere.
# `manage.py test` creates/drops a `test_<name>` database on whatever `default` points
# at. If DATABASE_URL is ever pointed at a shared/production DB, Django's TEST dict
# can rename that database but cannot change its ENGINE, so it would still issue
# CREATE DATABASE / DROP DATABASE straight at it. Swapping the whole `default`
# connection to a throwaway local sqlite file whenever the test runner is active is the
# only way to keep TestCase-based suites (e.g. tests_swing_v2.py) from ever touching prod.
if 'test' in sys.argv:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': str(BASE_DIR / 'test_db.sqlite3'),
        }
    }
else:
    DATABASES = {
        'default': dj_database_url.config(
            default=os.getenv('DATABASE_URL', f"postgres://{os.getenv('DB_USER','postgres')}:{os.getenv('DB_PASSWORD','postgres')}@{os.getenv('DB_HOST','localhost')}:{os.getenv('DB_PORT','5432')}/{os.getenv('DB_NAME','tradepulse_db')}"),
            conn_max_age=600
        )
    }

# ──────────────────────────────────────────────
# AUTH — Custom user model
# ──────────────────────────────────────────────
AUTH_USER_MODEL = 'users.CustomUser'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ──────────────────────────────────────────────
# DJANGO REST FRAMEWORK
# ──────────────────────────────────────────────
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        # Audit fix M3: wraps JWTAuthentication to enforce the temporary/guest
        # 60-second session cutoff on every endpoint, not just ProfileView —
        # see users/authentication.py.
        'users.authentication.GuestSessionAwareJWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    # Backstop against a misbehaving/compromised client hammering an endpoint
    # directly — everything else in this codebase's rate defense (scan cooldowns,
    # cutoffs) is hand-rolled cache logic inside view/service code, not enforced
    # at the framework level. See doc/AUDIT_REMEDIATION_PLAN.md 3.2.1.
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.UserRateThrottle',
        'rest_framework.throttling.AnonRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'user': '120/min',
        'anon': '30/min',
        # Stricter scope for LiveSignalView's ?force=true path, which is
        # designed to bypass intraday_service's own 5-min scan-rate cooldown.
        'force_scan': '2/min',
    },
}

# ──────────────────────────────────────────────
# SIMPLE JWT
# ──────────────────────────────────────────────
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# ──────────────────────────────────────────────
# AUTHENTICATION
# ──────────────────────────────────────────────
AUTHENTICATION_BACKENDS = [
    'users.backends.EmailOrUsernameModelBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# ──────────────────────────────────────────────
# CORS
# ──────────────────────────────────────────────
raw_origins = os.getenv(
    'CORS_ALLOWED_ORIGINS',
    'http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173'
)
CORS_ALLOWED_ORIGINS = [o.strip().rstrip('/') for o in raw_origins.split(',') if o.strip()]

CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = [
    'accept',
    'authorization',
    'content-type',
    'origin',
    'x-requested-with',
]
CORS_ALLOW_METHODS = [
    'DELETE',
    'GET',
    'OPTIONS',
    'PATCH',
    'POST',
    'PUT',
]

CSRF_TRUSTED_ORIGINS = os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if os.getenv('CSRF_TRUSTED_ORIGINS') else []

# ──────────────────────────────────────────────
# INTERNATIONALIZATION
# ──────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# ──────────────────────────────────────────────
# STATIC & MEDIA FILES
# ──────────────────────────────────────────────
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# WhiteNoise compression and caching
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# ──────────────────────────────────────────────
# DEFAULT PRIMARY KEY
# ──────────────────────────────────────────────
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ──────────────────────────────────────────────
# TRADEPULSE SERVICE CONFIG
# ──────────────────────────────────────────────

# NSE bhavcopy URL template  (date filled at runtime as YYYYMMDD)
NSE_BHAVCOPY_URL = (
    'https://nsearchives.nseindia.com/content/cm/'
    'BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip'
)
NSE_BASE_URL = 'https://www.nseindia.com'
NSE_DELIVERY_URL = (
    'https://nsearchives.nseindia.com/products/content/'
    'sec_bhavdata_full_{ddmmyyyy}.csv'
)
NSE_FO_BHAVCOPY_URL = (
    'https://nsearchives.nseindia.com/content/fo/'
    'BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip'
)

# Volume spike
VOLUME_SPIKE_LOOKBACK_DAYS = 20

MARKET_BIAS_BULLISH_THRESHOLD = 0.25
MARKET_BIAS_BEARISH_THRESHOLD = -0.25

# Signal scoring weights  (must sum to 1.0)
SIGNAL_WEIGHTS = {
    'price_change': 0.25,
    'volume_spike': 0.25,
    'oi_change': 0.20,
    'delivery_pct': 0.15,
    'market_bias': 0.15,
}
SIGNAL_BUY_THRESHOLD = 60
SIGNAL_SELL_THRESHOLD = 40

# Factor normalisation ranges  (min, max) for linear interpolation to 0-100
SIGNAL_FACTOR_RANGES = {
    'price_change': (-5.0, 5.0),       # percent
    'volume_spike': (0.0, 5.0),         # ratio
    'oi_change': (-1_000_000, 1_000_000),
    'delivery_pct': (0.0, 100.0),       # already 0-100
}

# Claude API  (for AI insight generation)
CLAUDE_API_KEY = os.getenv('CLAUDE_API_KEY', '')

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
# Production (Render): stdout only — captured by Render's log aggregator.
# Local development: set LOG_TO_FILE=true in .env to enable rotating file logs
# stored in BASE_DIR/logs/ (max 10 MB per file, 5 backups = 50 MB ceiling).
import logging.handlers as _lh

_LOG_TO_FILE = os.getenv('LOG_TO_FILE', 'false').lower() in ('true', '1', 'yes')
_LOGS_DIR = BASE_DIR / 'logs'

_handlers = {'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'}}
_handler_names = ['console']

if _LOG_TO_FILE:
    _LOGS_DIR.mkdir(exist_ok=True)
    _handlers['rotating_file'] = {
        'class': 'logging.handlers.RotatingFileHandler',
        'filename': str(_LOGS_DIR / 'tradepulse.log'),
        'maxBytes': 10 * 1024 * 1024,   # 10 MB per file
        'backupCount': 5,                 # Keep 5 backups → max 50 MB total
        'formatter': 'verbose',
        'encoding': 'utf-8',
    }
    _handler_names = ['console', 'rotating_file']

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} {name}: {message}',
            'style': '{',
        },
    },
    'handlers': _handlers,
    'loggers': {
        'stocks.services': {
            'handlers': _handler_names,
            'level': 'INFO',
            'propagate': False,
        },
        'global_market.services': {
            'handlers': _handler_names,
            'level': 'INFO',
            'propagate': False,
        },
        'insights.services': {
            'handlers': _handler_names,
            'level': 'INFO',
            'propagate': False,
        },
    },
}

# ──────────────────────────────────────────────
# PRODUCTION SECURITY HARDENING
# ──────────────────────────────────────────────
# `manage.py test` uses Django's plain-HTTP test client (see the DATABASES override
# above for the same 'test' in sys.argv technique) — with SECURE_SSL_REDIRECT on,
# SecurityMiddleware 301-redirects every test request to https before the view ever
# runs, failing any test that asserts on the response. CI sets DEBUG=False to mirror
# production, so `not DEBUG` alone isn't enough to detect "this is a test run".
if not DEBUG and 'test' not in sys.argv:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    X_FRAME_OPTIONS = 'DENY'
    SECURE_SSL_REDIRECT = True

# ──────────────────────────────────────────────
# TRUEDATA MARKET DATA API CONFIGURATION
# ──────────────────────────────────────────────
TRUEDATA = {
    "USERNAME": os.getenv("TRUEDATA_USERNAME", "").strip(),
    "PASSWORD": os.getenv("TRUEDATA_PASSWORD", "").strip(),
    # 8084 = production real-time port, 8086 = sandbox (see Market Data API doc 4.a.iii).
    "WS_PORT": os.getenv("TRUEDATA_WS_PORT", "8084").strip(),
}

# ──────────────────────────────────────────────
# TELEGRAM BOT ALERTS (FREE — Telegram Bot API)
# ──────────────────────────────────────────────
TELEGRAM_ALERTS = {
    "ENABLED": os.getenv("TELEGRAM_ALERTS_ENABLED", "false").strip(),
    "BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
    "CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    "SHORT_TERM_CHAT_ID": os.getenv("TELEGRAM_SHORT_TERM_CHAT_ID", "").strip(),
    "DAILY_SUMMARY_ENABLED": os.getenv("TELEGRAM_DAILY_SUMMARY", "true").strip(),
}

# ──────────────────────────────────────────────
# LONG-TERM ENGINE
# ──────────────────────────────────────────────
# Re-enabled 2026-07-28 at the account owner's explicit request, after being disabled
# 2026-07-26 (see doc/LONG_TERM_ENGINE_V2_ARCHITECTURE.md §0). The fabricated-fundamentals
# bug (hardcoded roe/debt_to_equity/profit_growth literals) is NOT reintroduced — those
# fields are gone for good; scan_long_term_stocks() only returns real, computed fields.
# What IS still true: ranking is momentum_100d only (no fundamentals feed exists), so
# correlated names can still dominate the top 5 — the documented concentration risk this
# flag was originally created to gate. Flip back to False if that recurs in practice.
LONG_TERM_GENERATION_ENABLED = os.getenv("LONG_TERM_GENERATION_ENABLED", "true").strip().lower() in ("true", "1", "yes")

SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# ──────────────────────────────────────────────
# ERROR TRACKING (audit fix M22)
# ──────────────────────────────────────────────
# An unhandled exception in the 15-min scanner or the 2:30 PM option-buying
# force-close path used to fail silently — nobody paged, positions could sit open
# bleeding theta with no operator ever finding out. This is a no-op unless
# SENTRY_DSN is set in the deployment env: no DSN, no import attempt beyond the
# guarded try/except below, zero behavior change for any environment that hasn't
# opted in. Set SENTRY_DSN (and optionally SENTRY_ENVIRONMENT) to activate.
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if SENTRY_DSN and 'test' not in sys.argv:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=os.getenv("SENTRY_ENVIRONMENT", "production" if not DEBUG else "development"),
            integrations=[
                DjangoIntegration(),
                # Every logger.error(...) call already sprinkled through the
                # scanner/audit/force-close code paths becomes a Sentry event too,
                # not just an unhandled exception — most of those call sites catch
                # and log rather than let the exception propagate, so relying on
                # Sentry's default unhandled-exception capture alone would miss them.
                LoggingIntegration(level=None, event_level="ERROR"),
            ],
            traces_sample_rate=0.0,  # error tracking only, no perf tracing overhead
            send_default_pii=False,
        )
    except Exception:
        # Never let error-tracking setup itself become a reason the app fails to
        # start (e.g. sentry-sdk not yet installed in some environment).
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "SENTRY_DSN is set but sentry_sdk could not be initialized — "
            "error tracking is inactive this run.", exc_info=True,
        )
