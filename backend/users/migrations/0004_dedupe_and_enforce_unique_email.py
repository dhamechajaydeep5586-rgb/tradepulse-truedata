# Audit fix L4: CustomUser.email had no unique constraint. users/backends.py's
# EmailOrUsernameModelBackend does `User.objects.get(email__iexact=username)` on
# every login attempt with an '@' in the username — with duplicate emails in the
# table, that raises MultipleObjectsReturned and crashes the whole login request
# (uncaught 500) instead of a normal "wrong credentials" response.
#
# Two-step fix, both in this migration so they land atomically:
#   1. Deduplicate: for any set of accounts sharing the same email
#      (case-insensitive — matching the __iexact lookup backends.py actually
#      uses), keep the oldest account's email untouched and clear (blank) the
#      email on the newer duplicate(s). Clearing rather than guessing a "fixed"
#      email is the least presumptuous option — it doesn't fabricate a value,
#      it just removes an already-unusable-for-login ambiguous state (that
#      account's owner can re-set their email afterward).
#   2. Enforce going forward: email is optional (many guest/username-only
#      accounts have it blank), so a plain unique=True would break the first
#      time two blank emails coexist. Use a conditional, case-insensitive
#      unique constraint instead — unique only among NON-blank emails.
from django.db import migrations, models
from django.db.models import Q
from django.db.models.functions import Lower


def dedupe_emails(apps, schema_editor):
    CustomUser = apps.get_model('users', 'CustomUser')

    groups = {}
    for u in CustomUser.objects.exclude(email='').order_by('date_joined', 'id'):
        key = u.email.strip().lower()
        groups.setdefault(key, []).append(u)

    for email_lower, users in groups.items():
        if len(users) <= 1:
            continue
        _keeper, *dupes = users  # oldest account keeps the email as-is
        for dupe in dupes:
            dupe.email = ''
            dupe.save(update_fields=['email'])


def noop_reverse(apps, schema_editor):
    # Clearing duplicate emails is not meaningfully reversible (the original
    # duplicate value is discarded, not stored) — nothing to undo automatically.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0003_customuser_first_login_at'),
    ]

    operations = [
        migrations.RunPython(dedupe_emails, noop_reverse),
        migrations.AddConstraint(
            model_name='customuser',
            constraint=models.UniqueConstraint(
                Lower('email'),
                name='unique_non_blank_email_ci',
                condition=~Q(email=''),
            ),
        ),
    ]
