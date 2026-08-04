from datetime import timedelta
from unittest.mock import MagicMock

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
