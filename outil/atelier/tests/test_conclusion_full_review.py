"""Independent SYNTH review/export witnesses, no candidate or provider use.

Signature vectors are created solely in memory for these tests. Patches below
are named boundary isolation and never qualify an actual documentary gate.
"""
import base64
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import override_settings
from django.utils import timezone
from jsonschema import Draft202012Validator, FormatChecker

from atelier.models import (HumanReview, VerificationDelivery, UserProfile,
    VerificationJob, VerificationAttempt, VerificationReceipt)
from atelier.services import full_review as fr
from atelier.services import proof_architecture as a
from atelier.services import proof_architecture_export as pe
from atelier.services import verification as v
from atelier.services import verification_export as ve
from atelier.services import conclusion_dependencies as d
from atelier.services import detached_review as dr
from atelier.services.canonical import sha256_digest
from atelier.tests.support import AtelierTestCase
from atelier.tests import test_conclusion_dependencies as fixtures


@override_settings(DV_ENV='test', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ConclusionFullReviewTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        # Compose the already-qualified fixture without inheriting its tests.
        fixture = fixtures.ConclusionOriginalObservationsTests(methodName='runTest')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.revision = fixture.revision
        self.revision.dossier.data_scope = 'synthetic'
        self.revision.dossier.current_revision = self.revision.revision
        self.revision.dossier.save()
        self.revision.public_payload = deepcopy(fixture.snapshot['content']['public_payload'])
        self.revision.method_version = 'SYNTH independent export qualification'
        self.revision.title = 'SYNTH independent conclusion'
        self.revision.short_answer = 'SYNTH answer with no documentary approval'
        self.revision.main_caveat = 'SYNTH boundary qualification only'
        self.revision.reasoning = 'SYNTH reasoning, actual negative assessment retained'
        self.revision.result = self.revision.public_payload['result']
        self.revision.public_payload['diffusion_selection'] = {
            'contract': 'dv.diffusion-selection/1',
            'passages': [{'evidence_index': 0,
                'exact_quote': self.revision.public_payload['declaration_quote'],
                'attribution': 'SYNTH Alice, fictional source for test only'}],
            'public_note': 'SYNTH bounded selection, no actual publication'}
        self.revision.snapshot_digest = sha256_digest(self.revision.public_payload)
        self.revision.verification_required = True
        # snapshot() reloads the persisted server revision, by design.
        self.revision.save()
        self.revision.conclusion_contract = d.build_conclusion(v.snapshot(self.revision), archetype=d.ARCHETYPES[0])
        self.revision.save()
        from django.contrib.auth import get_user_model
        self.verifier = get_user_model().objects.create_user('SYNTH-review-export-verifier', password='SYNTH-only-test-password')
        UserProfile.objects.create(user=self.verifier, role='verifier', is_test_identity=True)
        self.reviewer = get_user_model().objects.create_user('SYNTH-review-export-holder', password='SYNTH-only-test-password')
        UserProfile.objects.create(user=self.reviewer, role='reviewer', is_test_identity=True)
        self.artifact = a.receive_local_assessment(self.revision)

    def boundary_receipt(self):
        """Fictional receipt for export boundaries, never assessed as native proof."""
        value = v.snapshot(self.revision)
        job = VerificationJob.objects.create(revision=self.revision, key=sha256_digest({'SYNTH': value}),
            fingerprint=sha256_digest(value), policy=v.POLICY, input_snapshot=value,
            state='completed')
        attempt = VerificationAttempt.objects.create(job=job, actor=self.verifier, number=1,
            execution_id='SYNTH:serializer-boundary-no-provider', provider='synthetic',
            expires_at=timezone.now() + timedelta(minutes=2), finished_at=timezone.now())
        return VerificationReceipt.objects.create(job=job, attempt=attempt,
            fingerprint=job.fingerprint, result_digest=sha256_digest({'SYNTH': 'fixture'}),
            decision='supported')

    def boundary_assessment(self, receipt):
        """Copied formatting vector; original received negative stays immutable."""
        # Inserting the fictional receipt makes real readback stale, as it
        # should. Use only the saved negative formatting vector at this seam.
        report = deepcopy(self.artifact.payload)
        report['assessment_digest'] = self.artifact.payload_digest
        report['documentary_status'] = 'supported'
        report['native_semantic'] = {'receipt_id': receipt.pk}
        for gate in report['gates'].values():
            gate['status'] = 'complete'
        return report

    def mapping(self):
        return {'sources': {ref['source_id']: {k: ref[k] for k in ('source_id', 'version_id', 'sha256')}
                            for ref in self.revision.source_snapshot}}

    @staticmethod
    def synth_packet_signature(packet):
        # Format-only vector: deliberately no real signing key or network send.
        packet.update(key_id='SYNTH-independent-export', signature_algorithm='HMAC-SHA256', signature='f' * 64)
        return packet

    def bundle(self):
        return fr.build_review_bundle(self.revision, environment='test', destination='dv-site:test')

    def save_vector(self, name, value):
        if not os.environ.get('DV_SYNTH_REVIEW_VECTORS'):
            return
        target = Path(os.environ['DV_SYNTH_REVIEW_VECTORS']).resolve()
        root = Path(__file__).resolve().parents[4]
        self.assertTrue(target.is_relative_to(root))
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

    def signature(self, bundle):
        private = Ed25519PrivateKey.generate()
        public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        statement = {'algorithm': 'ed25519', 'key_id': 'test-independent-conclusion',
            'bundle_sha256': sha256_digest(bundle), 'record_id': bundle['record_id'],
            'record_revision': bundle['record_revision'], 'decision': 'approve', 'reviewed_at': timezone.now().isoformat()}
        proof = {'statement': statement,
                 'signature': base64.b64encode(private.sign(fr.approval_bytes(bundle, statement))).decode()}
        return proof, {statement['key_id']: public}

    def verify(self, bundle, proof, keys):
        return fr.verify_detached_review(bundle, proof, trusted_public_keys=keys,
            environment='test', destination='dv-site:test', allow_test_fixtures=True)

    def synthetically_supported_bundle(self):
        # Cryptographic seam only. This mutation cannot change received ORM
        # proof: real import compares the reconstructed negative bundle first.
        bundle = self.bundle()
        bundle['proof_architecture']['documentary_status'] = 'supported'
        return bundle

    def test_current_negative_bundle_format04_binding2_and_closed_schema(self):
        bundle = self.bundle()
        self.assertEqual(bundle['format'], fr.DEPENDENCY_FORMAT)
        proof = bundle['proof_architecture']
        self.assertEqual(proof['schema'], 'dv.proof-architecture-review-binding/2')
        self.assertEqual(proof['documentary_status'], self.artifact.payload['documentary_status'])
        self.assertNotIn(proof['documentary_status'], {'supported', 'not_comparable'})
        self.assertEqual(proof['revision_fingerprint'], sha256_digest(v.snapshot(self.revision)))
        graph = self.artifact.payload['dependency_graph']
        self.assertEqual(proof['dependency_graph_sha256'], sha256_digest(graph))
        schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts' / fr.full_review_schema_filename(bundle)).read_text())
        errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(bundle))
        self.assertFalse(errors, [(list(e.path), e.message) for e in errors][:3])
        self.assertEqual(HumanReview.objects.count(), 0); self.assertEqual(VerificationDelivery.objects.count(), 0)
        self.save_vector('negative-current-review-v04-SYNTH.json', bundle)

    def test_negative_historical_documentary_state_cannot_receive_approval(self):
        bundle = self.bundle(); proof, keys = self.signature(bundle)
        with self.assertRaisesRegex(ValidationError, 'proof_architecture_unresolved'):
            self.verify(bundle, proof, keys)
        self.assertEqual(HumanReview.objects.count(), 0)

    def test_signed_stale_graph_fingerprint_or_source_alias_fails_crypto_binding(self):
        bundle = self.synthetically_supported_bundle(); proof, keys = self.signature(bundle)
        mutations = (
            lambda b: b['proof_architecture'].update(dependency_graph_sha256='b' * 64),
            lambda b: b['proof_architecture'].update(revision_fingerprint='b' * 64),
            lambda b: b['source_versions'][0].update(source_id='SYNTH:ALIAS-SAME-SHA'),
            lambda b: b['source_versions'][0].update(version_id='SYNTH:ALIAS-SAME-SHA'),
        )
        for mutation in mutations:
            altered = deepcopy(bundle); mutation(altered)
            with self.assertRaisesRegex(ValidationError, 'binding_mismatch'):
                self.verify(altered, proof, keys)

    def test_cryptographic_signature_does_not_authorize_review_publication_or_send(self):
        bundle = self.synthetically_supported_bundle(); proof, keys = self.signature(bundle)
        verified = self.verify(bundle, proof, keys)
        self.assertTrue(verified['verified_detached_signature'])
        self.assertFalse(verified['publication_authorized']); self.assertFalse(verified['send_authorized'])
        self.assertEqual(HumanReview.objects.count(), 0)

    def test_resigned_promoted_negative_bundle_cannot_enter_real_import(self):
        bundle = self.synthetically_supported_bundle(); proof, keys = self.signature(bundle)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys):
            with self.assertRaisesRegex(ValidationError, 'detached_bundle_not_current'):
                dr.accept_detached_review(dossier_id=self.revision.dossier_id, expected_revision=self.revision.revision,
                    bundle=bundle, proof=proof, user=self.reviewer, environment='test', destination='dv-site:test')
        self.assertEqual(HumanReview.objects.count(), 0)

    def test_downgraded_resigned_format_cannot_import_profile07(self):
        bundle = self.synthetically_supported_bundle()
        bundle['format'] = fr.RACC001_FORMAT; bundle.pop('proof_architecture')
        proof, keys = self.signature(bundle)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys):
            with self.assertRaisesRegex(ValidationError, 'detached_bundle_not_current'):
                dr.accept_detached_review(dossier_id=self.revision.dossier_id, expected_revision=self.revision.revision,
                    bundle=bundle, proof=proof, user=self.reviewer, environment='test', destination='dv-site:test')

    def test_real_identity_path_stays_disabled_before_any_human_row(self):
        bundle = self.bundle(); proof, _ = self.signature(bundle)
        with self.assertRaisesRegex(PermissionDenied, 'real_identity_not_activated'):
            dr.accept_detached_review(dossier_id=self.revision.dossier_id, expected_revision=self.revision.revision,
                bundle=bundle, proof=proof, user=self.reviewer, environment='private', destination='dv-site:private')
        self.assertEqual(HumanReview.objects.count(), 0)

    def test_source_binding_false_never_builds_bundle(self):
        with patch('atelier.services.catalogue.binding_check', return_value=(False, 'SYNTH refused binding')):
            with self.assertRaisesRegex(ValidationError, 'source_binding_failed'):
                self.bundle()

    def test_missing_or_stale_received_architecture_never_builds_bundle(self):
        with patch.object(a, 'assessment_for_revision', return_value=None):
            with self.assertRaisesRegex(ValidationError, 'architecture_missing_or_stale'):
                self.bundle()

    def test_blocked_gates_prevent_projection_and_export_before_signature(self):
        architecture = a.assessment_for_revision(self.revision)
        with self.assertRaisesRegex(ValidationError, 'not_publishable'):
            pe.public_summary(architecture, 'b' * 64)
        with patch.object(ve, '_sign_packet') as sign:
            with self.assertRaisesRegex(ValidationError, 'site_export_refused'):
                ve.build_site_packet(self.revision, user=self.verifier, environment='test', sequence=1, mapping={})
            sign.assert_not_called()
        self.assertEqual(VerificationDelivery.objects.count(), 0)

    def test_export_source_or_version_alias_same_sha_refused_before_signature(self):
        receipt = self.boundary_receipt(); architecture = self.boundary_assessment(receipt)
        ref = self.revision.source_snapshot[0]
        for field in ('source_id', 'version_id'):
            mapping = self.mapping()
            mapping['sources'][ref['source_id']][field] = 'SYNTH:SAME-SHA-ALIAS'
            with self.subTest(field=field), \
                 patch.object(ve, 'evaluate_revision', return_value=SimpleNamespace(allowed=True, reason='SYNTH boundary only')), \
                 patch.object(a, 'assessment_for_revision', return_value=architecture), \
                 patch.object(ve, '_sign_packet') as sign:
                with self.assertRaisesRegex(ValidationError, 'proof_architecture_source_alias_refused'):
                    ve.build_site_packet(self.revision, user=self.verifier, environment='test', sequence=1, mapping=mapping)
                sign.assert_not_called()
        self.assertEqual(VerificationDelivery.objects.count(), 0); self.assertEqual(HumanReview.objects.count(), 0)

    def test_export_v5_closed_schema_and_hashes_are_exact_in_formatting_vector(self):
        receipt = self.boundary_receipt(); architecture = self.boundary_assessment(receipt)
        # Normative shape vector only: no received evidence or approval is
        # altered. Necessary unknowns in the actual negative remain received.
        architecture['unknowns'] = []
        with patch.object(ve, 'evaluate_revision', return_value=SimpleNamespace(allowed=True, reason='SYNTH boundary only')), \
             patch.object(a, 'assessment_for_revision', return_value=architecture), \
             patch.object(ve, '_sign_packet', side_effect=self.synth_packet_signature):
            packet = ve.build_site_packet(self.revision, user=self.verifier, environment='test', sequence=1, mapping=self.mapping())
        self.assertEqual(packet['schema'], 'dv.verification-site/5')
        proof = packet['payload']['diffusion_snapshot']['content']['proof_architecture']
        self.assertEqual(proof['revision_fingerprint'], receipt.fingerprint)
        self.assertEqual(proof['conclusion']['dependency_graph_sha256'], sha256_digest(architecture['dependency_graph']))
        self.assertEqual(proof['assessment_digest'], architecture['assessment_digest'])
        self.assertEqual(proof['source_binding_sha256'], sha256_digest(packet['payload']['source_versions']))
        self.assertEqual(packet['verification']['diffusion_sha256'], sha256_digest(packet['payload']['diffusion_snapshot']))
        self.assertEqual(packet['payload_sha256'], sha256_digest(packet['payload']))
        self.assertNotIn('qualification_material', json.dumps(packet))
        self.assertFalse(HumanReview.objects.exists())
        path = Path(__file__).resolve().parents[2] / 'contracts' / 'dv-verification-site-0.5.0-conclusion-dependencies-draft.1.schema.json'
        validator = Draft202012Validator(json.loads(path.read_text()), format_checker=FormatChecker())
        errors = list(validator.iter_errors(packet))
        self.assertFalse(errors, [(list(e.path), e.message) for e in errors][:5])
        for mutation in ('rights_na', 'human_missing', 'graph_missing', 'unknown_needed', 'extra_field'):
            candidate = deepcopy(packet)
            projection = candidate['payload']['diffusion_snapshot']['content']['proof_architecture']
            if mutation == 'rights_na': projection['gates']['rights_qualified']['status'] = 'not_applicable'
            elif mutation == 'human_missing': projection['gates'].pop('human_reviewed_exact_revision')
            elif mutation == 'graph_missing': projection['conclusion'].pop('dependency_graph_sha256')
            elif mutation == 'unknown_needed': projection['necessary_unknowns'].append({
                'location': '/SYNTH/unknown', 'state': 'unknown', 'explanation': 'SYNTH indispensable fact unavailable'})
            else: projection['free_client_approval'] = True
            with self.subTest(mutation=mutation):
                self.assertTrue(list(validator.iter_errors(candidate)))
        self.save_vector('formatting-only-site-v5-SYNTH.json', packet)

    def test_existing_delivery_cannot_replay_a_different_current_assessment(self):
        receipt = self.boundary_receipt(); architecture = self.boundary_assessment(receipt)
        with patch.object(ve, 'evaluate_revision', return_value=SimpleNamespace(allowed=True, reason='SYNTH boundary only')), \
             patch.object(a, 'assessment_for_revision', return_value=architecture), \
             patch.object(ve, '_sign_packet', side_effect=self.synth_packet_signature):
            initial = ve.build_site_packet(self.revision, user=self.verifier, environment='test', sequence=1, mapping=self.mapping())
            # Exact replay retains the already signed packet.
            self.assertEqual(ve.build_site_packet(self.revision, user=self.verifier, environment='test', sequence=1,
                mapping=self.mapping()), initial)
        changed = deepcopy(architecture); changed['assessment_digest'] = 'b' * 64
        self.assertNotEqual(initial['payload']['diffusion_snapshot']['content']['proof_architecture']['assessment_digest'],
                            changed['assessment_digest'])
        # A fresh assessment on the same snapshot and same first receipt must
        # not silently replay the signed evidence binding of an older one.
        with patch.object(ve, 'evaluate_revision', return_value=SimpleNamespace(allowed=True, reason='SYNTH boundary only')), \
             patch.object(a, 'assessment_for_revision', return_value=changed), \
             patch.object(ve, '_sign_packet') as sign:
            with self.assertRaises(ValidationError):
                ve.build_site_packet(self.revision, user=self.verifier, environment='test', sequence=1, mapping=self.mapping())
            sign.assert_not_called()
        self.assertEqual(HumanReview.objects.count(), 0)

    def test_every_blocked_gate_refuses_public_summary_even_with_synth_supported_status(self):
        architecture = a.assessment_for_revision(self.revision)
        architecture['documentary_status'] = 'supported'
        for gate in a.GATES:
            candidate = deepcopy(architecture)
            for row in candidate['gates'].values():
                row['status'] = 'complete'
            candidate['gates'][gate]['status'] = 'blocked'
            with self.subTest(gate=gate), self.assertRaisesRegex(ValidationError, 'not_publishable'):
                pe.public_summary(candidate, 'b' * 64)

    def test_noncomparable_outcome_never_becomes_supported_comparison_in_summary(self):
        architecture = a.assessment_for_revision(self.revision)
        architecture['documentary_status'] = 'not_comparable'
        architecture['conclusion']['archetype'] = d.ARCHETYPES[3]
        architecture['summary']['result'] = 'not_comparable'
        for row in architecture['gates'].values():
            row['status'] = 'complete'
        summary = pe.public_summary(architecture, 'b' * 64)
        self.assertEqual(summary['documentary_status'], 'not_comparable')
        self.assertFalse(summary['conclusion']['comparison_claimed'])
        self.assertEqual(summary['schema'], 'dv.proof-architecture-public/2')

    def test_unsigned_wrong_target_unknown_key_or_changed_statement_rejected(self):
        bundle = self.synthetically_supported_bundle(); proof, keys = self.signature(bundle)
        for changed in ('statement', 'key', 'target'):
            candidate = deepcopy(proof)
            if changed == 'statement':
                candidate['statement']['reviewed_at'] = (timezone.now() - timedelta(days=3)).isoformat()
                code = 'signature_invalid'
            elif changed == 'key':
                candidate['statement']['key_id'] = 'test-unknown'; code = 'key_unknown'
            else:
                bundle = deepcopy(bundle); bundle['destination'] = 'dv-site:other'; code = 'target_mismatch'
            with self.subTest(changed=changed), self.assertRaisesRegex(ValidationError, code):
                self.verify(bundle, candidate, keys)
