from django.core.management.base import BaseCommand

from atelier.services.explain import seed_explain_entries
from atelier.services.feeds.scheduler import ensure_feed_cursors


class Command(BaseCommand):
    help = "Synchronise le catalogue FEEDS-01 et les fiches EXPLAIN-01 sans réseau."

    def handle(self, *args, **options):
        feeds = ensure_feed_cursors()
        explain = seed_explain_entries()
        self.stdout.write(
            self.style.SUCCESS(
                f"build002_seed feeds_created={feeds} explain={explain} network_calls=0"
            )
        )
