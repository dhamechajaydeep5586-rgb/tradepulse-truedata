import getpass

from django.core.management.base import BaseCommand, CommandError
from users.models import CustomUser

class Command(BaseCommand):
    help = "Creates a permanent owner account (Superuser) that is ignored by the expiration policy."

    def add_arguments(self, parser):
        parser.add_argument("--username", type=str, required=True)
        parser.add_argument("--email", type=str, required=True)

    def handle(self, *args, **options):
        username = options["username"]
        email = options["email"]
        # Audit fix L6: --password used to be a required CLI flag — a superuser
        # password passed as a plaintext argument is visible via process listing
        # (`ps`) and shell history to anyone with access to the host. Prompt
        # interactively instead, matching Django's own createsuperuser command.
        password = getpass.getpass("Password: ")
        password_confirm = getpass.getpass("Password (again): ")
        if password != password_confirm:
            raise CommandError("Passwords did not match.")
        if not password:
            raise CommandError("Password cannot be empty.")

        if CustomUser.objects.filter(username=username).exists():
            self.stdout.write(self.style.WARNING(f"User '{username}' already exists."))
            return

        user = CustomUser.objects.create_superuser(
            username=username,
            email=email,
            password=password
        )
        # Mark as permanent
        user.is_temporary = False
        user.save()
        
        self.stdout.write(self.style.SUCCESS(f"Successfully created permanent owner account: {username}"))
