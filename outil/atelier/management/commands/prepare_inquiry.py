import json
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from atelier.services.inquiry import prepare_inquiry


class Command(BaseCommand):
    help = "Recherche documentaire locale bornée sur une entrée inédite ; ne crée ni publication ni revue."

    def add_arguments(self, parser):
        parser.add_argument("--query", required=True)
        parser.add_argument("--actor-id", default="")
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--scan-limit", type=int, default=1000)
        parser.add_argument("--declaration-offset", type=int, default=0)
        parser.add_argument("--scrutiny-offset", type=int, default=0)

    def handle(self, *args, **options):
        try:
            result = prepare_inquiry(query=options["query"], actor_id=options["actor_id"], limit=options["limit"],
                scan_limit=options["scan_limit"], declaration_offset=options["declaration_offset"], scrutiny_offset=options["scrutiny_offset"])
        except ValidationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
