from django.core.management.base import BaseCommand

from builder.catalog_seed import seed_all


class Command(BaseCommand):
    help = "Idempotently upsert the builder catalog: shapes, sidebar, dashboard."

    def handle(self, *_args, **_kwargs) -> None:
        counts = seed_all()
        self.stdout.write(self.style.SUCCESS(
            f"Builder catalog seeded: " + ", ".join(f"{k}={v}" for k, v in counts.items())
        ))
