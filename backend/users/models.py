from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


class CustomUser(AbstractUser):
    phone_number = models.CharField(max_length=15, blank=True)
    is_premium = models.BooleanField(default=False)
    is_temporary = models.BooleanField(default=False)
    first_login_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'users'
        ordering = ['-created_at']
        constraints = [
            # Audit fix L4: email is optional (blank=True, inherited from
            # AbstractUser) — many guest/username-only accounts have it blank, so
            # a plain unique=True would break the moment two blank emails
            # coexist. Unique only among non-blank emails, case-insensitive to
            # match users/backends.py's email__iexact login lookup. See migration
            # 0004 for the matching one-time dedupe of any pre-existing
            # collisions this constraint would otherwise reject.
            models.UniqueConstraint(
                Lower('email'),
                name='unique_non_blank_email_ci',
                condition=~Q(email=''),
            ),
        ]

    def __str__(self):
        return self.email or self.username
