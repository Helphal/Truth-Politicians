"""Reprise additive de brouillons documentaires, sans qualification ni diffusion."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
import shutil

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from atelier.models import (Source, DocumentVersion, EvidencePassage, EditorialDossier,
                            EditorialRevision, Theme, ThemeAssignment)
from atelier.services.canonical import sha256_digest
from atelier.services.verification import enqueue


class Command(BaseCommand):
    help = 'Importer les candidats réels en privé. Nécessite une copie locale dédiée.'

    def add_arguments(self, parser):
        parser.add_argument('batch', type=Path)
        parser.add_argument('--receipt', type=Path, required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        if settings.DV_ENV not in {'local', 'test'}: raise CommandError('isolated_local_copy_required')
        root = options['batch'].resolve().parent
        batch = json.loads(options['batch'].read_text(encoding='utf-8'))
        manifest = json.loads((root / 'source-manifest.json').read_text(encoding='utf-8'))
        vault = Path(settings.DV_EVIDENCE_VAULT_PATH) / 'suite-totale-20260930'
        vault.mkdir(parents=True, exist_ok=True)
        documents = {}; source_refs = {}
        for item in manifest:
            if item.get('status') != 200: continue
            raw = (root / item['path']).resolve(); textfile = (root / item['normalized_text_path']).resolve()
            if not raw.is_relative_to(root) or not textfile.is_relative_to(root): raise CommandError('unsafe_source_path')
            if hashlib.sha256(raw.read_bytes()).hexdigest() != item['sha256'] or hashlib.sha256(textfile.read_bytes()).hexdigest() != item['normalized_text_sha256']:
                raise CommandError('source_integrity_mismatch')
            sid = 'DV-SOURCE:' + item['id']; version = item['sha256'][:24]
            source, _ = Source.objects.get_or_create(stable_id=sid, defaults={
                'producer': 'Assemblée nationale' if item['id'].startswith('an-') else 'Sénat',
                'family': 'institutional_document', 'discovery_url': item['url'], 'document_url': item['final_url'],
                'access_status': 'active', 'rights_status': 'unknown', 'operational_enabled': False,
                'qualification_notes': 'Pièce reçue et empreintée ; réutilisation à qualifier séparément.'})
            raw_target = vault / (item['sha256'] + '.html')
            text_target = vault / (item['normalized_text_sha256'] + '.txt')
            for src, dst in [(raw, raw_target), (textfile, text_target)]:
                if not dst.exists(): shutil.copyfile(src, dst)
                elif hashlib.sha256(dst.read_bytes()).digest() != hashlib.sha256(src.read_bytes()).digest(): raise CommandError('vault_existing_bytes_mismatch')
            doc, _ = DocumentVersion.objects.get_or_create(source=source, version_id=version, defaults={
                'sha256': item['sha256'], 'captured_at': datetime.fromisoformat(item['collected_at']),
                'media_type': item['media_type'], 'byte_size': item['bytes'], 'original_path': str(raw_target),
                'parser_version': item['normalizer'], 'public_copy_allowed': False,
                'collection_receipt': {'normalized_text_file': str(text_target.relative_to(settings.DV_EVIDENCE_VAULT_PATH)),
                                      'normalized_text_sha256': item['normalized_text_sha256'], 'status': 200}})
            text = textfile.read_text(encoding='utf-8')
            # L'intégralité est disponible au vérificateur ; découpage borné sans suppression.
            for start in range(0, len(text), 8000):
                EvidencePassage.objects.get_or_create(document=doc, locator=f'texte normalisé caractères {start}:{min(start+8000,len(text))}',
                    defaults={'exact_text': text[start:start+8000], 'context': 'Pièce primaire complète, segments contigus.',
                              'public_url': item['final_url']})
            documents[item['id']] = doc
            source_refs[item['id']] = {'source_id': sid, 'version_id': version, 'sha256': item['sha256'], 'public_url': item['final_url']}
        for theme in batch['themes']:
            Theme.objects.get_or_create(stable_id=theme['stable_id'], defaults={'label': theme['label'], 'definition': theme['definition']})
        rows = []
        for candidate in batch['dossiers']:
            dossier, created = EditorialDossier.objects.get_or_create(stable_id=candidate['stable_id'], defaults={
                'content_kind': 'speech_vote', 'established_kind': 'speech_vote', 'data_scope': 'real'})
            dossier = EditorialDossier.objects.select_for_update().get(pk=dossier.pk)
            old = dossier.revisions.filter(revision=dossier.current_revision).first()
            refs = [source_refs[sid] for sid in candidate['source_ids']]
            source_quote = source_refs[candidate['declaration']['source_id']]
            text_id = next((sid for sid in candidate['source_ids'] if 'text-' in sid and 'adopted' not in sid), candidate['declaration']['source_id'])
            payload = {
                'title': candidate['title'], 'short_answer': candidate['short_answer'], 'main_caveat': candidate['main_caveat'],
                'reasoning': '\n'.join(candidate['supporting_elements'] + [candidate['procedure_and_submitted_version']]),
                'result': candidate['political_classification_candidate'], 'declaration_quote': candidate['declaration']['quote'],
                'declaration_date': candidate['period'], 'declaration_source_url': source_quote['public_url'],
                'official_vote_url': source_refs[candidate['official_vote_source_id']]['public_url'],
                'official_text_url': source_refs[text_id]['public_url'], 'scope': candidate['question'],
                'attribution': candidate['actor'], 'themes': [candidate['theme_id']],
                'counterevidence': [{'statement': x, 'effect': 'limite à conserver'} for x in candidate['counter_elements_and_limits']],
                'method_explanation': {'procedure': candidate['procedure_and_submitted_version'], 'candidate_only': True},
                'coverage': {'statement': candidate['documentary_scope'], 'period_start': candidate['period'], 'period_end': candidate['period']},
                'evidence': [{'source_id': r['source_id'], 'version_id': r['version_id'], 'url': r['public_url']} for r in refs]}
            if candidate.get('calculation'): payload['method_explanation']['calculation'] = candidate['calculation']
            digest = sha256_digest(payload)
            revision = dossier.revisions.filter(snapshot_digest=digest).first()
            if revision is None:
                if old and old.snapshot_digest != candidate.get('base_snapshot_digest'):
                    raise CommandError('base_revision_changed:' + dossier.stable_id)
                revision = EditorialRevision.objects.create(dossier=dossier, revision=dossier.current_revision+1,
                    title=payload['title'], short_answer=payload['short_answer'], main_caveat=payload['main_caveat'],
                    reasoning=payload['reasoning'], result=payload['result'], method_version='DV-M0.1-candidate-20260930',
                    public_payload=payload, source_snapshot=refs, snapshot_digest=digest, verification_required=True,
                    change_summary=candidate['change_summary'], comparison=old.comparison if old else None)
                dossier.current_revision = revision.revision
                dossier.state = 'needs_evidence'; dossier.save(update_fields=['current_revision', 'state', 'updated_at'])
            job = enqueue(revision)
            theme = Theme.objects.get(stable_id=candidate['theme_id'])
            ThemeAssignment.objects.get_or_create(theme=theme, target_type='dossier', target_stable_id=dossier.stable_id,
                locator='lot candidat 20260930', defaults={'origin': 'proposed', 'status': 'proposed'})
            rows.append({'object': dossier.stable_id, 'revision': revision.revision, 'revision_pk': revision.pk,
                         'job_id': job.pk, 'fingerprint': job.fingerprint, 'state': 'needs_evidence', 'published': False})
        receipt = {'contract': batch['schema'], 'imported': rows, 'sources': len(documents),
                   'human_review_created': False, 'publication_created': False}
        options['receipt'].write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        self.stdout.write(f'{len(rows)} révisions candidates ; aucun événement de diffusion.')
