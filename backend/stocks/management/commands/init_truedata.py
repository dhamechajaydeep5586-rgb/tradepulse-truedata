"""
Django management command to initialize the TrueData service.
Run on application startup.
"""

from django.core.management.base import BaseCommand
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Initialize TrueData service'

    def handle(self, *args, **options):
        try:
            from stocks.services import truedata_service

            config = settings.TRUEDATA
            success = truedata_service.initialize_truedata(
                username=config["USERNAME"],
                password=config["PASSWORD"],
            )

            if success:
                self.stdout.write(
                    self.style.SUCCESS('✓ TrueData service initialized successfully')
                )
            else:
                self.stdout.write(
                    self.style.WARNING('✗ Failed to initialize TrueData service')
                )

        except Exception as e:
            logger.error(f"Error initializing TrueData: {e}")
            self.stdout.write(
                self.style.ERROR(f'✗ Error: {e}')
            )
