from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

User = get_user_model()

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

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
