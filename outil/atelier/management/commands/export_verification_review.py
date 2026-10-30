"""Offline read-only guide from an explicitly supplied review dataset."""
import hashlib
import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from atelier.services.verification_review import guide_markdown


class Command(BaseCommand):
    help = 'Export privé en Markdown ; aucun appel modèle et aucune écriture métier.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--input', required=True)
        parser.add_argument('--output', required=True)

    def handle(self, *args, **options):
        source, output = Path(options['input']).resolve(), Path(options['output']).resolve()
        if output.exists() or output == source:
            raise CommandError('output_already_exists')
        data = json.loads(source.read_text(encoding='utf-8-sig'))
        links = {}
        for row in data['revisions']:
            preview = (source.parent / row['preview']).resolve()
            if not preview.is_relative_to(source.parent) or not preview.is_file() or preview.is_symlink():
                raise CommandError('preview_missing_or_unsafe')
            if hashlib.sha256(preview.read_bytes()).hexdigest() != row['preview_sha256']:
                raise CommandError('preview_digest_changed')
            links[row['job_id']] = preview.as_posix()
        result = guide_markdown(data, preview_links=links, plain_senate_urls=True)
        if '[object Object]' in result:
            raise CommandError('unreadable_source_version')
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as stream:
            stream.write(result)
        self.stdout.write(f"Guide privé exporté : {len(data['revisions'])} dossiers ; aucune inférence.")
