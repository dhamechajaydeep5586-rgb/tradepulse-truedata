from django.core.management.base import BaseCommand
from users.models import CustomUser

class Command(BaseCommand):
    help = "Creates a permanent owner account (Superuser) that is ignored by the expiration policy."

    def add_arguments(self, parser):
        parser.add_argument("--username", type=str, required=True)
        parser.add_argument("--email", type=str, required=True)
        parser.add_argument("--password", type=str, required=True)

    def handle(self, *args, **options):
        username = options["username"]
        email = options["email"]
        password = options["password"]

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
