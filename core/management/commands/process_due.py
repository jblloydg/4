import time

from django.core.management.base import BaseCommand

from core.backups import run_daily_backup_if_due
from core.services import process_due_actions


class Command(BaseCommand):
    help = "Run inactivity, guard-house, archive-purge actions and the daily backup."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Check every 60 seconds while the local server runs.")
        parser.add_argument("--interval", type=int, default=60)

    def handle(self, *args, **options):
        interval = max(10, options["interval"])
        while True:
            process_due_actions()
            run_daily_backup_if_due()
            if not options["loop"]:
                break
            time.sleep(interval)
