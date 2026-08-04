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


class AdminLoginRateLimitTests(TestCase):
    """
    Audit fix L5: /admin/login/ had no brute-force lockout and sits outside DRF's
    throttle classes entirely. Cache-based per-IP lockout after 5 failed attempts.
    """

    def setUp(self):
        from django.core.cache import cache
        self.cache = cache
        self.cache.delete("admin_login_fails_127.0.0.1")
        self.cache.delete("admin_login_lockout_127.0.0.1")

    def tearDown(self):
        self.cache.delete("admin_login_fails_127.0.0.1")
        self.cache.delete("admin_login_lockout_127.0.0.1")

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
