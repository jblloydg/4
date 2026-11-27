from django.core.management.base import BaseCommand, CommandError

from core.backups import list_backups, restore_backup


class Command(BaseCommand):
    help = (
        "Replace the whole database and uploaded photos with a backup. "
        "A safety backup of the current state is made first. Stop the server before restoring."
    )

    def add_arguments(self, parser):
        parser.add_argument("name", nargs="?", help="Backup folder name (see: python manage.py backup_now --list).")
        parser.add_argument("--yes", action="store_true", help="Skip the confirmation question.")

    def handle(self, *args, **options):
        name = options.get("name")
        if not name:
            backups = list_backups()
            if not backups:
                raise CommandError("There are no backups to restore.")
            self.stdout.write("Available backups (newest first):")
            for info in backups:
                self.stdout.write(f"  {info['name']}  ({info.get('reason', '')})")
            raise CommandError("Give the backup name, e.g. python manage.py restore_backup " + backups[0]["name"])
        if not options["yes"]:
            answer = input(
                f"This REPLACES all current Find-IT data and photos with backup {name}.\n"
                "A safety backup is made first. Type RESTORE to continue: "
            )
            if answer.strip() != "RESTORE":
                raise CommandError("Restore cancelled.")
        try:
            safety = restore_backup(name)
        except FileNotFoundError as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(f"Restored {name}. The previous state was saved as {safety.name}."))
