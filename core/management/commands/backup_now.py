from django.core.management.base import BaseCommand

from core.backups import create_backup, list_backups


class Command(BaseCommand):
    help = "Make a backup now (database as portable JSON + uploaded photos) in the backups folder."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="List existing backups instead of making one.")

    def handle(self, *args, **options):
        if options["list"]:
            backups = list_backups()
            if not backups:
                self.stdout.write("No backups yet.")
            for info in backups:
                self.stdout.write(
                    f"{info['name']}  {info.get('reason', '')}  rows={info.get('row_count')}  "
                    f"photos={info.get('media_files')}  {info['size_bytes'] // 1024} KB"
                )
            return
        folder = create_backup("manual")
        self.stdout.write(self.style.SUCCESS(f"Backup created: {folder}"))
