from django.core.management.base import BaseCommand
from django.apps import apps
from django.db import connection
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Scan all registered models and recreate any missing database tables dynamically."

    def handle(self, *args, **options):
        self.stdout.write("==============================================")
        self.stdout.write("   CHECKING & RECREATING ALL MISSING TABLES   ")
        self.stdout.write("==============================================")

        try:
            # Fetch all tables currently in the database schema
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT table_name 
                    FROM information_schema.tables 
                    WHERE table_schema = 'public';
                """)
                existing_tables = {row[0] for row in cursor.fetchall()}

            all_models = apps.get_models()
            created_count = 0

            self.stdout.write(f"Loaded {len(all_models)} models. Verifying database tables...")

            with connection.schema_editor() as schema_editor:
                for model in all_models:
                    # Skip proxy models as they don't have separate tables
                    if model._meta.proxy:
                        continue
                        
                    table_name = model._meta.db_table
                    if table_name not in existing_tables:
                        self.stdout.write(self.style.WARNING(f"[!] Missing Table: '{table_name}' (Model: {model.__name__}). Creating..."))
                        try:
                            schema_editor.create_model(model)
                            self.stdout.write(self.style.SUCCESS(f" -> SUCCESS: Recreated table '{table_name}'"))
                            created_count += 1
                        except Exception as e:
                            self.stdout.write(self.style.ERROR(f" -> ERROR: Failed to create table '{table_name}': {e}"))
                            logger.error(f"Failed to create table {table_name}: {e}")

            self.stdout.write("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            self.stdout.write(self.style.SUCCESS(f"Audit complete. Recreated {created_count} missing tables."))
            self.stdout.write("==============================================")

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Critical error during table recreation check: {e}"))
            logger.exception("Critical error during recreate_missing_tables")
