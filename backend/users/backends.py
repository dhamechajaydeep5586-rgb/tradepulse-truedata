import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

User = get_user_model()
logger = logging.getLogger(__name__)

class EmailOrUsernameModelBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)

        if not username:
            return None

        try:
            if '@' in username:
                user = User.objects.get(email__iexact=username)
            else:
                user = User.objects.get(username__iexact=username)
        except User.DoesNotExist:
            # Audit fix L4: Django's own ModelBackend runs the password hasher once
            # here specifically to close a timing side-channel (#20760) — without
            # this, a nonexistent username returns instantly while a real one takes
            # as long as check_password()'s hashing, letting a timing attack
            # enumerate valid usernames/emails.
            User().set_password(password)
            return None
        except User.MultipleObjectsReturned:
            # Bug fix (found in a follow-up audit): CustomUser.email had no unique
            # constraint (see the accompanying migration, which both deduplicates
            # any existing collisions and adds one going forward), so this
            # email__iexact lookup could raise MultipleObjectsReturned and crash
            # the whole login request with an uncaught 500 instead of a normal
            # "wrong credentials" response. Even with the constraint now enforced
            # for new rows, fail closed defensively rather than guessing which
            # account the caller meant — ambiguous credential resolution should
            # never silently pick one.
            logger.error(
                "[AUTH] Multiple users share email %r — refusing to authenticate "
                "ambiguously. Run the email-uniqueness migration / audit duplicate "
                "accounts.", username,
            )
            User().set_password(password)  # keep the same timing-safety behavior as DoesNotExist
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
