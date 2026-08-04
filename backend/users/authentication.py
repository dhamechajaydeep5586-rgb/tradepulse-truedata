from datetime import timedelta

from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

# Audit fix M3: the 60-second temporary/guest session cutoff was enforced on
# ProfileView only — every other API accepted the JWT for its full 30-minute
# access-token lifetime, letting a guest credential pull live trading data
# ~30x longer than intended (including via a captured token replayed outside
# the frontend). DEFAULT_AUTHENTICATION_CLASSES runs this on every DRF
# request regardless of view, so the cutoff is now enforced at exactly one
# place instead of needing to be remembered per-endpoint.
GUEST_SESSION_LIFETIME = timedelta(minutes=1)


class GuestSessionAwareJWTAuthentication(JWTAuthentication):
    """Same as JWTAuthentication, but a temporary/guest account's session is
    rejected once GUEST_SESSION_LIFETIME has elapsed since first_login_at —
    everywhere, not just on the one endpoint that used to check it.

    Also deletes the expired guest row, same as the cleanup ProfileView used to
    do on its own — moved here so it still happens now that this class (not
    ProfileView) is the one actually rejecting the request first. `.delete()`
    on a row a concurrent request already deleted is a safe no-op (0 rows
    affected), so this is safe under concurrent requests hitting the same
    expired session.
    """

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        user, validated_token = result
        if getattr(user, "is_temporary", False) and user.first_login_at:
            if timezone.now() > user.first_login_at + GUEST_SESSION_LIFETIME:
                user.delete()
                raise AuthenticationFailed("Session expired.")

        return user, validated_token
