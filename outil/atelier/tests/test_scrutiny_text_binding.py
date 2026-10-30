"""New SYNTH rows and original files; no old FK or revision repair."""
from copy import deepcopy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.exceptions import ValidationError
from django.test import override_settings

from atelier.models import LegislativeDossier, TextVersion, ScrutinyTextBinding
from atelier.services.canonical import sha256_digest
from atelier.services.catalogue import build_catalogue, source_ref
from atelier.services.catalogue_v2 import select_scrutiny_text_bindings
from .support import AtelierTestCase


class ScrutinyTextBindingTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        guard = override_settings(DV_EVIDENCE_VAULT_PATH=self.root)
        guard.enable()
        self.addCleanup(guard.disable)
        self.vote = self.original('https://www.assemblee-nationale.fr/dyn/16/scrutins/908',
            '<html><p>SYNTH motion effectivement observée.</p></html>')
        self.scrutiny = self.make_scrutiny(self.vote, source_identifier='908')
        self.scrutiny.stable_id = 'AN:16:SCRUTIN:908'
        self.scrutiny.save()
        self.old_text = self.scrutiny.submitted_text
        self.dossier_doc = self.original('https://www.assemblee-nationale.fr/dyn/16/dossiers/plrfss_pour_2023',
            '<html><h1>SYNTH dossier législatif distinct</h1></html>')
        self.motion_doc = self.original('https://www.assemblee-nationale.fr/dyn/16/comptes-rendus/seance/SYNTH/SYNTH',
            '<html><p>SYNTH motion effectivement observée.</p></html>')
        dossier = LegislativeDossier.objects.create(stable_id='SYNTH-NEW-LEG-DOSSIER',
            institution='Assemblée nationale', official_title='SYNTH dossier législatif distinct',
            official_url=self.dossier_doc.source.document_url, status='SYNTH unknown')
        self.motion = TextVersion.objects.create(stable_id='SYNTH-MOTION-OBSERVED', dossier=dossier,
            kind='motion', official_title='SYNTH motion effectivement observée.',
            version_label='SYNTH observation; literal document NOT_RECEIVED', stage='SYNTH',
            relation='submitted', occurred_on=self.scrutiny.occurred_on,
            official_url=self.motion_doc.source.document_url, source_document=self.motion_doc)
        self.payload = {'contract': 'dv.scrutiny-text-binding/1', 'version': 1,
            'scrutiny_id': self.scrutiny.stable_id, 'submitted_text_id': self.motion.stable_id,
            'legislative_dossier_id': dossier.stable_id, 'source_version': source_ref(self.vote),
            'text_source_version': source_ref(self.motion_doc), 'dossier_source_version': source_ref(self.dossier_doc),
            'procedure': 'SYNTH motion', 'object_kind': 'motion', 'literal_text_status': 'not_received',
            'source_locator': 'SYNTH p1', 'exact_source_quote': 'SYNTH motion effectivement observée.', 'actor_id': 1}

    def original(self, url, text):
        data = text.encode('utf-8')
        path = self.root / (hashlib.sha256(data).hexdigest() + '.html')
        path.write_bytes(data)
        document = self.make_document()
        document.source.document_url = url
        document.source.save(update_fields=['document_url'])
        document.sha256 = hashlib.sha256(data).hexdigest()
        document.media_type = 'text/html'
        document.byte_size = len(data)
        document.original_path = str(path)
        document.save()
        return document

    def binding(self, **changes):
        payload = {**deepcopy(self.payload), **changes}
        return ScrutinyTextBinding.objects.create(scrutiny=self.scrutiny, submitted_text=self.motion,
            source_document=self.vote, version=1, procedure='SYNTH motion', actor_id=1,
            payload=payload, payload_digest=sha256_digest(payload))

    def ref(self, binding):
        return {'scrutiny_id': self.scrutiny.stable_id, 'version': binding.version, 'payload_digest': binding.payload_digest}

    def test_explicit_projection_changes_only_loaded_objects_and_old_profile_is_identical(self):
        old, old_refs = build_catalogue([self.scrutiny.stable_id], profile='dv.catalogue/2')
        before = (self.scrutiny.submitted_text_id, self.scrutiny.procedure, self.scrutiny.source_document_id)
        binding = self.binding()
        projected, refs = build_catalogue([self.scrutiny.stable_id], profile='dv.catalogue/2',
            provenance_ruleset='dv.provenance-rules/2', binding_refs=[self.ref(binding)])
        self.assertEqual(projected['scrutinies'][0]['submitted_text_id'], self.motion.stable_id)
        self.assertEqual(projected['scrutinies'][0]['procedure'], 'SYNTH motion')
        self.assertEqual(projected['text_versions'][0]['dossier_id'], self.motion.dossier.stable_id)
        self.assertIn(source_ref(self.dossier_doc), refs)
        self.scrutiny.refresh_from_db()
        self.assertEqual(before, (self.scrutiny.submitted_text_id, self.scrutiny.procedure, self.scrutiny.source_document_id))
        self.assertEqual(build_catalogue([self.scrutiny.stable_id], profile='dv.catalogue/2'), (old, old_refs))
        self.assertNotEqual(self.old_text.pk, self.motion.pk)

    def test_selection_without_exact_version_and_digest_is_refused(self):
        binding = self.binding()
        for changes in ({'version': 2}, {'payload_digest': 'a' * 64}, {'scrutiny_id': 'SYNTH OTHER'}, {'latest': True}):
            ref = {**self.ref(binding), **changes}
            with self.assertRaises(ValidationError):
                select_scrutiny_text_bindings([self.scrutiny.stable_id], [ref])
        with self.assertRaisesMessage(ValidationError, 'scrutiny_text_bindings_require_conclusion_profile'):
            build_catalogue([self.scrutiny.stable_id], profile='dv.catalogue/2', binding_refs=[self.ref(binding)])

    def test_binding_is_append_only_at_instance_and_queryset(self):
        binding = self.binding()
        for operation in (lambda: binding.save(), lambda: binding.delete(),
                          lambda: ScrutinyTextBinding.objects.filter(pk=binding.pk).update(procedure='SYNTH forged'),
                          lambda: ScrutinyTextBinding.objects.filter(pk=binding.pk).delete()):
            with self.assertRaisesMessage(ValidationError, 'proof_record_immutable'):
                operation()

    def test_wrong_source_sha_or_version_or_dossier_cannot_bind(self):
        for key, value in [('source_version', {**source_ref(self.vote), 'sha256': 'a' * 64}),
                           ('text_source_version', {**source_ref(self.motion_doc), 'version_id': 'SYNTH wrong'}),
                           ('legislative_dossier_id', 'SYNTH different dossier'),
                           ('dossier_source_version', {**source_ref(self.dossier_doc), 'public_url': 'https://example.org/SYNTH'})]:
            with self.assertRaises(ValidationError):
                self.binding(**{key: value})

    def test_closed_binding_cannot_claim_literal_document_from_procedural_record(self):
        with self.assertRaisesMessage(ValidationError, 'procedural_record_is_not_literal_submitted_document'):
            self.binding(literal_text_status='received_literal_document')
        with self.assertRaisesMessage(ValidationError, 'scrutiny_text_binding_closed_payload_required'):
            self.binding(arbitrary_pass=True)
        with self.assertRaisesMessage(ValidationError, 'scrutiny_text_binding_version_actor_mismatch'):
            self.binding(actor_id=99)

    def test_original_tamper_and_fake_quote_are_refused(self):
        with self.assertRaisesMessage(ValidationError, 'scrutiny_text_binding_quote_not_in_original'):
            self.binding(exact_source_quote='SYNTH absent quote')
        binding = self.binding()
        Path(self.dossier_doc.original_path).write_text('SYNTH corrupted source', encoding='utf-8')
        with self.assertRaisesMessage(ValidationError, 'scrutiny_text_binding_original_integrity_failed'):
            binding.validate_binding(**self.ref(binding))
