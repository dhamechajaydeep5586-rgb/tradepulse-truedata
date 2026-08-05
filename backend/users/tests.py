from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken


class GuestSessionCutoffTests(TestCase):
    """
    Audit fix M3: the 60-second temporary/guest session cutoff used to be enforced
    on ProfileView only — every other endpoint accepted the same JWT for its full
    30-minute access-token lifetime. Now enforced centrally in
    GuestSessionAwareJWTAuthentication (DEFAULT_AUTHENTICATION_CLASSES), so it
    applies to every DRF view, not just ProfileView.
    """

    def _make_guest(self, first_login_at):
        User = get_user_model()
        user = User.objects.create_user(username="guest1", password="x", is_temporary=True)
        User.objects.filter(pk=user.pk).update(first_login_at=first_login_at)
        user.refresh_from_db()
        return user

    def _bearer_header(self, user):
        token = RefreshToken.for_user(user)
        return {"HTTP_AUTHORIZATION": f"Bearer {token.access_token}"}

    def test_expired_guest_session_rejected_on_non_profile_endpoint(self):
        """The whole point of the fix: an endpoint OTHER than /profile/ must also
        reject an expired guest session, not just accept the JWT for its full
        30-minute lifetime."""
        expired_login = timezone.now() - timedelta(minutes=5)
        user = self._make_guest(expired_login)
        headers = self._bearer_header(user)

        client = APIClient()
        response = client.get("/api/stocks/live-signals/", **headers)

        self.assertEqual(response.status_code, 401)

    def test_expired_guest_session_deletes_the_user_row(self):
        expired_login = timezone.now() - timedelta(minutes=5)
        user = self._make_guest(expired_login)
        headers = self._bearer_header(user)
        user_pk = user.pk

        client = APIClient()
        client.get("/api/auth/profile/", **headers)

        User = get_user_model()
        self.assertFalse(User.objects.filter(pk=user_pk).exists())

    def test_fresh_guest_session_still_works(self):
        """Sanity check: a guest well within the 60s window must not be rejected."""
        fresh_login = timezone.now() - timedelta(seconds=5)
        user = self._make_guest(fresh_login)
        headers = self._bearer_header(user)

        client = APIClient()
        response = client.get("/api/auth/profile/", **headers)

        self.assertEqual(response.status_code, 200)

    def test_non_temporary_account_never_cut_off(self):
        User = get_user_model()
        user = User.objects.create_user(username="realuser1", password="x", is_temporary=False)
        headers = self._bearer_header(user)

        client = APIClient()
        response = client.get("/api/auth/profile/", **headers)
        self.assertEqual(response.status_code, 200)


class CreateOwnerCommandTests(TestCase):
    """
    Audit fix L6: create_owner used to require --password as a plaintext CLI
    argument (visible via `ps`/shell history). Now prompts interactively via
    getpass, matching Django's own createsuperuser command.
    """

    def test_password_no_longer_accepted_as_cli_flag(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from io import StringIO

        with self.assertRaises(CommandError):
            call_command(
                "create_owner", "--username=owner1", "--email=o@example.com",
                "--password=irrelevant", stdout=StringIO(), stderr=StringIO(),
            )

    def test_prompts_interactively_and_creates_superuser(self):
        from django.core.management import call_command
        from io import StringIO

        with patch("getpass.getpass", side_effect=["s3cret-pass", "s3cret-pass"]):
            call_command(
                "create_owner", "--username=owner2", "--email=o2@example.com",
                stdout=StringIO(),
            )

        User = get_user_model()
        user = User.objects.get(username="owner2")
        self.assertTrue(user.is_superuser)
        self.assertFalse(user.is_temporary)
        self.assertTrue(user.check_password("s3cret-pass"))

    def test_mismatched_passwords_aborts(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from io import StringIO

        with patch("getpass.getpass", side_effect=["pw-one", "pw-two"]):
            with self.assertRaises(CommandError):
                call_command(
                    "create_owner", "--username=owner3", "--email=o3@example.com",
                    stdout=StringIO(),
                )

        User = get_user_model()
        self.assertFalse(User.objects.filter(username="owner3").exists())


class DuplicateEmailAuthTests(TestCase):
    """
    Audit fix L4: EmailOrUsernameModelBackend.authenticate() did
    User.objects.get(email__iexact=username) with no unique constraint on
    CustomUser.email — two accounts sharing an email (case-insensitively) raised
    MultipleObjectsReturned, an uncaught exception that crashed the whole login
    request (500) instead of a normal auth failure. Fixed with (1) a conditional,
    case-insensitive unique constraint on non-blank emails (migration 0004,
    which also deduplicates any pre-existing collisions) and (2) defensive
    exception handling in the backend itself as a second layer.
    """

    def test_authenticate_does_not_crash_on_multiple_objects_returned(self):
        from users.backends import EmailOrUsernameModelBackend

        backend = EmailOrUsernameModelBackend()
        with patch(
            "users.backends.User.objects.get",
            side_effect=get_user_model().MultipleObjectsReturned,
        ):
            result = backend.authenticate(None, username="dupe@example.com", password="whatever")

        self.assertIsNone(result, "Ambiguous credential resolution must fail closed, not crash")

    def test_constraint_rejects_case_insensitive_duplicate_email(self):
        from django.db import IntegrityError, transaction

        User = get_user_model()
        User.objects.create_user(username="userone", email="Same@Example.com", password="x")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create_user(username="usertwo", email="same@example.com", password="x")

    def test_constraint_allows_multiple_blank_emails(self):
        """Most guest/username-only accounts never set an email — the constraint
        must not treat blank as a colliding value."""
        User = get_user_model()
        User.objects.create_user(username="guestone", email="", password="x")
        User.objects.create_user(username="guesttwo", email="", password="x")
        self.assertEqual(User.objects.filter(email="").count(), 2)

    def test_dedupe_migration_keeps_oldest_and_blanks_newer_duplicates(self):
        """Exercise the actual migration function's grouping/dedup logic against
        mocked rows — the real CustomUser table already has migration 0004's
        constraint applied in this test DB, so a genuine duplicate-email pair
        can't be persisted here to set up the scenario; this only needs to
        verify dedupe_emails() picks the right rows to keep/blank, not the DB
        write path itself (already covered by the constraint tests above)."""
        import importlib

        migration_module = importlib.import_module(
            "users.migrations.0004_dedupe_and_enforce_unique_email"
        )

        older = MagicMock(email="dup@example.com", date_joined="2026-01-01", id=1)
        newer1 = MagicMock(email="DUP@example.com", date_joined="2026-02-01", id=2)
        newer2 = MagicMock(email="Dup@Example.com", date_joined="2026-03-01", id=3)
        unrelated = MagicMock(email="someone-else@example.com", date_joined="2026-01-15", id=4)

        mock_queryset = MagicMock()
        mock_queryset.exclude.return_value.order_by.return_value = [older, newer1, newer2, unrelated]

        class _FakeModel:
            objects = mock_queryset

        class _FakeApps:
            @staticmethod
            def get_model(app_label, model_name):
                return _FakeModel

        migration_module.dedupe_emails(_FakeApps(), None)

        self.assertEqual(older.email, "dup@example.com")
        older.save.assert_not_called()
        self.assertEqual(newer1.email, "")
        newer1.save.assert_called_once_with(update_fields=['email'])
        self.assertEqual(newer2.email, "")
        newer2.save.assert_called_once_with(update_fields=['email'])
        unrelated.save.assert_not_called()


class AdminLoginRateLimitTests(TestCase):
    """
    Audit fix L5: /admin/login/ had no brute-force lockout and sits outside DRF's
    throttle classes entirely. Cache-based per-IP lockout after 5 failed attempts.
    """

    def setUp(self):
        from django.core.cache import cache
        self.cache = cache
        for ip in ("127.0.0.1", "203.0.113.9", "198.51.100.7"):
            self.cache.delete(f"admin_login_fails_{ip}")
            self.cache.delete(f"admin_login_lockout_{ip}")

    def tearDown(self):
        for ip in ("127.0.0.1", "203.0.113.9", "198.51.100.7"):
            self.cache.delete(f"admin_login_fails_{ip}")
            self.cache.delete(f"admin_login_lockout_{ip}")

    def test_locks_out_after_max_failed_attempts(self):
        from django.test import Client
        client = Client(REMOTE_ADDR="127.0.0.1")

        for _ in range(5):
            response = client.post("/admin/login/", {"username": "nope", "password": "wrong"})
            self.assertEqual(response.status_code, 200)  # form re-rendered with error

        # The 6th attempt (even with correct-looking data) must be blocked outright.
        locked_response = client.post("/admin/login/", {"username": "nope", "password": "wrong"})
        self.assertEqual(locked_response.status_code, 429)

    def test_successful_login_clears_failure_count(self):
        from django.contrib.auth import get_user_model
        from django.test import Client

        User = get_user_model()
        User.objects.create_superuser(username="admin_test", password="realpass123", email="a@example.com")

        client = Client(REMOTE_ADDR="127.0.0.1")
        client.post("/admin/login/", {"username": "admin_test", "password": "wrong"})
        client.post("/admin/login/", {"username": "admin_test", "password": "wrong"})

        response = client.post(
            "/admin/login/",
            {"username": "admin_test", "password": "realpass123",
             "next": "/admin/"},
        )
        self.assertEqual(response.status_code, 302)  # successful login redirects

        self.assertIsNone(self.cache.get("admin_login_fails_127.0.0.1"))

    def test_non_admin_login_paths_are_unaffected(self):
        from rest_framework.test import APIClient
        client = APIClient(REMOTE_ADDR="127.0.0.1")
        for _ in range(10):
            response = client.post("/api/auth/login/", {"username": "x", "password": "y"})
            self.assertNotEqual(response.status_code, 429)

    def test_spoofed_first_xff_hop_cannot_evade_lockout(self):
        """Reproduces a bypass a follow-up audit found and confirmed live: the
        middleware used to trust the FIRST X-Forwarded-For entry, which is
        exactly the part of the header a client controls. An attacker sending a
        different fake first hop on every request made each attempt look like
        it came from a different IP, defeating the lockout entirely. The real
        client IP (REMOTE_ADDR, what a single trusted proxy hop appends as the
        LAST entry) must be what's actually tracked."""
        from django.test import Client
        client = Client(REMOTE_ADDR="203.0.113.9")

        for i in range(5):
            response = client.post(
                "/admin/login/", {"username": "nope", "password": "wrong"},
                HTTP_X_FORWARDED_FOR=f"10.0.0.{i}, 203.0.113.9",
            )
            self.assertEqual(response.status_code, 200)

        locked_response = client.post(
            "/admin/login/", {"username": "nope", "password": "wrong"},
            HTTP_X_FORWARDED_FOR="10.0.0.99, 203.0.113.9",
        )
        self.assertEqual(
            locked_response.status_code, 429,
            "A spoofed first XFF hop must not let the attacker evade the lockout",
        )

    def test_spoofed_xff_cannot_frame_a_different_ip_for_lockout(self):
        """The other direction of the same bug: an attacker who knows a real
        admin's IP could put it first in a spoofed XFF, burn 5 failed attempts,
        and lock the real admin out — a targeted DoS. The attacker's own
        connecting IP (last hop) must be what's tracked, not any earlier entry
        they can put in the header themselves."""
        from django.test import Client
        attacker_client = Client(REMOTE_ADDR="198.51.100.66")  # attacker's real IP
        victim_ip = "203.0.113.9"

        for _ in range(5):
            response = attacker_client.post(
                "/admin/login/", {"username": "nope", "password": "wrong"},
                HTTP_X_FORWARDED_FOR=f"{victim_ip}, 198.51.100.66",
            )
            self.assertEqual(response.status_code, 200)

        # The victim's IP must NOT be locked out — only the attacker's real IP is.
        self.assertIsNone(self.cache.get(f"admin_login_lockout_{victim_ip}"))

        victim_client = Client(REMOTE_ADDR=victim_ip)
        victim_response = victim_client.post(
            "/admin/login/", {"username": "real_admin", "password": "correct-ish"},
            HTTP_X_FORWARDED_FOR=victim_ip,
        )
        self.assertNotEqual(
            victim_response.status_code, 429,
            "The real admin (connecting from the spoofed-about IP) must not be locked out",
        )

    def test_x_real_ip_preferred_over_xff_when_present(self):
        """X-Real-IP is set directly by nginx from $remote_addr and can never be
        client-influenced (unlike X-Forwarded-For, which a client can always
        send at least one entry of) — it should be preferred when present."""
        from users.middleware import AdminLoginRateLimitMiddleware
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.post(
            "/admin/login/",
            HTTP_X_REAL_IP="203.0.113.9",
            HTTP_X_FORWARDED_FOR="10.0.0.1, 203.0.113.9",
        )
        self.assertEqual(AdminLoginRateLimitMiddleware._client_ip(request), "203.0.113.9")
