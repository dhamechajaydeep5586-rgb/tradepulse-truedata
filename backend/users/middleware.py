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
        # Fix for a bypass found in a follow-up audit: this used to trust the
        # FIRST entry in X-Forwarded-For, which is exactly the part of the header
        # a client controls — a request sent with `X-Forwarded-For: <anything>`
        # made every attempt look like it came from a different IP, defeating the
        # lockout entirely (reproduced: 8+ failed attempts, never a 429). Worse,
        # spoofing a real admin's IP as the first entry let an attacker burn that
        # admin's IP through the failure count and lock them out on purpose.
        #
        # Both deploy targets (Oracle's nginx.conf, Render's edge) sit as exactly
        # ONE trusted proxy hop in front of this app and APPEND the real
        # connecting IP as the last entry of X-Forwarded-For (nginx.conf now sets
        # it to $remote_addr directly, i.e. always a single, real entry) — so the
        # real client IP is always the LAST hop, never the first, regardless of
        # what a client puts in its own request. X-Real-IP (nginx-only, set
        # directly from $remote_addr, never client-influenced) is preferred when
        # present since it can't be spoofed even in a multi-hop chain.
        real_ip = request.META.get("HTTP_X_REAL_IP")
        if real_ip:
            return real_ip.strip()
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            hops = [h.strip() for h in forwarded.split(",") if h.strip()]
            if hops:
                return hops[-1]
        return request.META.get("REMOTE_ADDR", "unknown")
