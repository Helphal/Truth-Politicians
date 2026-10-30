"""Actual local layered service path on SYNTH originals, no model or real review."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import hashlib, tempfile
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from atelier.models import ProofArtifact, HumanReview, VerificationDelivery
from atelier.services import verification as v, verification_plan_executor as x
from atelier.services import proof_architecture as a
from atelier.services.canonical import sha256_digest
from atelier.services.proof_classification import classify_snapshot
from atelier.services.provenance import build_provenance_report
from atelier.services.verification_preflight import build_preflight
from atelier.services.verification_export import build_site_packet
from atelier.services.publication import Eligibility
from atelier.tests import test_verification as fixture_v
from atelier.tests.test_verification_plan_executor import SynthRunner
from atelier.tests.test_provenance import AN_HTML
from atelier.tests.test_proof_preflight_counterevidence import rights_fixture

@override_settings(DV_ENV='test', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ProofArchitectureTests(TestCase):
    def setUp(self):
        fixture_v.VerificationTests.setUp(self)
        temp = tempfile.TemporaryDirectory(prefix='dv-architecture-SYNTH-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        settings = override_settings(DV_EVIDENCE_VAULT_PATH=self.root,
            DV_VERIFICATION_PLAN_ROOT=self.root / 'plans')
        settings.enable(); self.addCleanup(settings.disable)
        raw = AN_HTML.replace(b'</html>', b'<p>SYNTH fait limite.</p></html>')
        text = raw.decode()
        (self.root / 'original.html').write_bytes(raw)
        (self.root / 'normalized.txt').write_text(text, encoding='utf8')
        self.doc.original_path = 'original.html'
        self.doc.media_type = 'text/html'; self.doc.byte_size = len(raw)
        self.doc.sha256 = hashlib.sha256(raw).hexdigest()
        self.doc.collection_receipt = {'normalized_text_file': 'normalized.txt',
            'normalized_text_sha256': hashlib.sha256(text.encode()).hexdigest()}
        self.doc.save()
        self.passage.exact_text = 'SYNTH fait limite.'; self.passage.save()
        ref = {'source_id': self.source.stable_id, 'version_id': self.doc.version_id,
               'sha256': self.doc.sha256, 'public_url': self.source.discovery_url}
        payload = {'title': 'SYNTH fait limite.',
            'evidence': [{'source_version': ref, 'exact_quote': self.passage.exact_text, 'locator': self.passage.locator}],
            'acts': [{'scrutiny_id': 'AN:16:SCRUTIN:3289', 'included': True, 'source_version': ref}],
            'actors': {'scope': 'personal', 'speaker': {'person_id': 'AN:PERSON:PA1'},
                       'compared_actor': {'person_id': 'AN:PERSON:PA1'}},
            'catalogue': {'scrutinies': [{'scrutin_id': 'AN:16:SCRUTIN:3289', 'occurred_on': '2024-01-30',
                'official_title': 'SYNTH vote', 'procedure': 'SYNTH ensemble', 'submitted_text_id': 'SYNTH-TEXT',
                'text_reference_role': 'submitted', 'source_version': ref}],
                'text_versions': [{'id': 'SYNTH-TEXT', 'source_version': ref}],
                'persons': [{'id': 'AN:PERSON:PA1', 'display_name': 'SYNTH Alice'}],
                'organizations': [], 'affiliations': [],
                'vote_observations': [{'scrutiny_id': 'AN:16:SCRUTIN:3289', 'person_id': 'AN:PERSON:PA1',
                    'normalized_category': 'for', 'source_category': 'pours',
                    'affiliation_at_event_id': 'AN:ORG:PO1', 'source_version': ref}]}}
        self.revision.public_payload = payload; self.revision.snapshot_digest = sha256_digest(payload)
        self.revision.source_snapshot = [ref]; self.revision.save()
        self.job = v.enqueue(self.revision)

    def prepare(self):
        snapshot = self.job.input_snapshot
        classification = classify_snapshot(snapshot, provenance_report=build_provenance_report(snapshot))
        preflight = build_preflight(snapshot, classification, rights_report=rights_fixture(snapshot))
        self.assertEqual(preflight['status'], 'ready', preflight['checks'])
        path, plan = x.prepare_plan(self.job.pk, user=self.user, model='SYNTH', effort='none',
            classification=classification, preflight=preflight)
        allocation = {'contract': x.ALLOCATION, 'allocation_id': 'SYNTH:architecture',
            'authorization': 'explicit_dv_verite', 'authority_reference': 'SYNTH structural fixture only',
            'owner_id': self.user.pk, 'plan_digests': [plan['plan_digest']], 'fingerprints': [plan['fingerprint']],
            'job_ids': [self.job.pk], 'model': 'SYNTH', 'effort': 'none',
            'max_phases': plan['allocation_proposal']['proposed_ceiling'],
            'expires_at': (timezone.now()+timedelta(hours=1)).isoformat()}
        allocation_path = self.root / 'plans' / 'allocation.json'
        x._write(allocation_path, allocation)
        return path, plan, allocation_path

    def test_layered_native_service_closes_and_replays_without_promoting_or_calling_twice(self):
        path, plan, allocation = self.prepare()
        runner = SynthRunner(plan)
        receipt = x.execute_plan(path, user=self.user, allocation_path=allocation,
            execution_id='SYNTH:architecture', runner=runner)
        self.assertEqual(receipt.decision, 'not_supported')
        self.assertEqual(receipt.attempt.result['contract'], 'dv.layered-result/1')
        self.assertEqual(len(runner.calls), plan['allocation_proposal']['first_pass_minimum_new_phases'])
        self.assertTrue(all(c['layer'] == 'semantic' for b in plan['batches'] if b['stage'] == 'claims'
            for c in plan['proof_architecture']['classification']['claims'] if c['claim_id'] in
            {r['claim_id'] for r in plan['assertions'] if r['assertion_id'] in b['assertion_ids']}))
        replay_runner = SynthRunner(plan)
        replay = x.execute_plan(path, user=self.user, allocation_path=allocation,
            execution_id='SYNTH:architecture', runner=replay_runner)
        self.assertEqual(replay.pk, receipt.pk); self.assertEqual(replay_runner.calls, [])
        self.assertEqual(HumanReview.objects.count(), 0); self.assertEqual(VerificationDelivery.objects.count(), 0)

    def test_missing_received_preflight_is_refused_before_allocation_or_constructor(self):
        path, plan, allocation = self.prepare()
        # Corruption injection is confined to this fresh SYNTH test database.
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute('DELETE FROM atelier_proofartifact WHERE schema = %s', ['dv.verification-preflight/1'])
        with patch.object(x, '_allocation') as reserve, patch.object(x, 'ProtocolCLI') as cli:
            with self.assertRaisesRegex(ValidationError, 'preflight_not_received'):
                x.execute_plan(path, user=self.user, allocation_path=allocation,
                    execution_id='SYNTH:refused', runner=SynthRunner(plan))
            reserve.assert_not_called(); cli.assert_not_called()
        self.assertEqual(self.job.attempts.count(), 0)

    def test_persisted_four_layers_read_only_and_stale_after_source_change(self):
        artifact = a.receive_local_assessment(self.revision)
        count = ProofArtifact.objects.count()
        report = a.assessment_for_revision(self.revision)
        self.assertIsNotNone(report)
        self.assertEqual(set(report['layers']), {'provenance', 'semantic', 'editorial', 'unknowns'})
        self.assertEqual(report['assessment_digest'], artifact.payload_digest)
        self.assertEqual(report['gates']['publishable']['status'], 'blocked')
        self.assertEqual(ProofArtifact.objects.count(), count)
        self.doc.sha256 = 'f'*64; self.doc.save()
        self.assertIsNone(a.assessment_for_revision(self.revision))

    def test_assessment_and_certificates_are_immutable_and_self_declared_pass_fails_readback(self):
        artifact = a.receive_local_assessment(self.revision)
        with self.assertRaisesRegex(ValidationError, 'immutable'):
            ProofArtifact.objects.filter(pk=artifact.pk).update(payload={})
        with self.assertRaisesRegex(ValidationError, 'immutable'):
            artifact.delete()
        with self.assertRaisesRegex(ValidationError, 'digest_mismatch'):
            ProofArtifact.objects.create(revision=self.revision, revision_fingerprint=artifact.revision_fingerprint,
                kind='assessment', schema=a.SCHEMA, payload={'documentary_status': 'supported'}, payload_digest='0'*64)

    def test_full_export_entry_point_blocks_negative_without_signature_or_delivery(self):
        path, plan, allocation = self.prepare()
        x.execute_plan(path, user=self.user, allocation_path=allocation,
            execution_id='SYNTH:architecture', runner=SynthRunner(plan, verdict='CONTREDIT'))
        a.receive_local_assessment(self.revision, rights_report=rights_fixture(v.snapshot(self.revision)))
        with patch('atelier.services.verification_export._sign_packet') as sign:
            with self.assertRaisesRegex(ValidationError, 'site_export_refused:'):
                build_site_packet(self.revision, user=self.user, environment='test', sequence=1, mapping={})
            sign.assert_not_called()
        self.assertEqual(HumanReview.objects.count(), 0); self.assertEqual(VerificationDelivery.objects.count(), 0)

    def test_architecture_export_refuses_source_or_version_alias_even_with_same_sha(self):
        path, plan, allocation = self.prepare()
        x.execute_plan(path, user=self.user, allocation_path=allocation,
            execution_id='SYNTH:architecture-alias', runner=SynthRunner(plan))
        a.receive_local_assessment(self.revision, rights_report=rights_fixture(v.snapshot(self.revision)))
        ref = self.revision.source_snapshot[0]
        for field in ('source_id', 'version_id'):
            target = {k: ref[k] for k in ('source_id', 'version_id', 'sha256')}
            target[field] = 'SYNTH:ALIAS'
            # Isolate the source-binding boundary after a fictitious favorable
            # gate. The genuine negative gate is tested above; this cannot
            # create an approval, signature or delivery in the fresh fixture.
            with patch('atelier.services.verification_export.evaluate_revision',
                       return_value=Eligibility(True, 'SYNTH-boundary-isolation')), \
                 patch('atelier.services.verification_export._sign_packet') as sign:
                with self.assertRaisesRegex(ValidationError, 'proof_architecture_source_alias_refused'):
                    build_site_packet(self.revision, user=self.user, environment='test', sequence=1,
                        mapping={'sources': {ref['source_id']: target}})
                sign.assert_not_called()
        self.assertEqual(HumanReview.objects.count(), 0)
        self.assertEqual(VerificationDelivery.objects.count(), 0)
