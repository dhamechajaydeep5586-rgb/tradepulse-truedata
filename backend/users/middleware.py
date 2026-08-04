import logging

from django.core.cache import cache
from django.http import HttpResponse

logger = logging.getLogger(__name__)

# Audit fix L5: Django admin login has no brute-force lockout, and it sits
# structurally outside DRF's throttle classes entirely — every other rate limit
# in this codebase (DEFAULT_THROTTLE_CLASSES in settings.py) only applies to DRF
# API views, not Django's own /admin/ login form. Cache-based per-IP lockout,
# mirroring the cooldown/halt pattern already used throughout this codebase
# (DAILY_HALT_CACHE_KEY, scan-rate guards, etc.) rather than pulling in a new
# third-party package with its own migrations and failure modes to verify.
ADMIN_LOGIN_PATH = "/admin/login/"
MAX_FAILED_ATTEMPTS = 5
FAILURE_WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 15 * 60


class AdminLoginRateLimitMiddleware:
    """Locks out an IP from /admin/login/ for LOCKOUT_SECONDS after
    MAX_FAILED_ATTEMPTS failed attempts within FAILURE_WINDOW_SECONDS.

    Self-expiring (cache TTL) rather than a permanent ban — a misconfigured or
    unlucky legitimate admin is never locked out for longer than the window, no
    manual unlock/DB row to clean up.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method != "POST" or not request.path.startswith("/admin/login"):
            return self.get_response(request)

        ip = self._client_ip(request)
        lock_key = f"admin_login_lockout_{ip}"
        if cache.get(lock_key):
            logger.warning("[ADMIN_LOCKOUT] Blocked login attempt from locked-out IP %s", ip)
            return HttpResponse(
                "Too many failed login attempts. Try again later.", status=429,
            )

        response = self.get_response(request)

        # Django's admin login view re-renders the form with a 200 on bad
        # credentials and redirects (302) on success — no other status is
        # expected from this view under normal operation.
        fail_key = f"admin_login_fails_{ip}"
        if response.status_code == 200:
            fails = cache.get(fail_key, 0) + 1
            cache.set(fail_key, fails, timeout=FAILURE_WINDOW_SECONDS)
            if fails >= MAX_FAILED_ATTEMPTS:
                cache.set(lock_key, True, timeout=LOCKOUT_SECONDS)
                logger.warning(
                    "[ADMIN_LOCKOUT] IP %s locked out for %ds after %d failed admin login attempts.",
                    ip, LOCKOUT_SECONDS, fails,
                )
        else:
            cache.delete(fail_key)

        return response

    @staticmethod
    def _client_ip(request) -> str:
        # Trusts X-Forwarded-For because every deploy target for this app sits
        # behind a real reverse proxy (Render's edge, or the Oracle nginx.conf in
        # deploy/oracle/) that sets it — matching the assumption Django's own
        # SECURE_PROXY_SSL_HEADER setting already makes elsewhere in this file.
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "unknown")
