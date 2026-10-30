"""SYNTH dependencies on newly created institutional-format originals.

These structural witnesses do not judge a political dossier or create a real
allocation, review, publication or provider call.
"""
from copy import deepcopy
from datetime import date
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings

from atelier.services import conclusion_dependencies as d
from atelier.services import proof_classification as c
from atelier.services import verification_preflight as f
from atelier.services import provenance as pr
from atelier.services import verification as v
from atelier.services import verification_counterevidence as counter
from atelier.services.canonical import sha256_digest
from atelier.tests import test_provenance as fixture_originals
from atelier.tests.test_verification_protocol import snapshot_fixture
from atelier.tests.test_proof_preflight_counterevidence import rights_fixture
from atelier.tests.support import AtelierTestCase
from atelier.tests import test_proof_classification_v2 as fixture_v2
from atelier import models as m
from atelier.tests import test_scrutiny_text_binding as binding_fixtures


class ConclusionDependenciesTests(SimpleTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dependencies-SYNTH-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        vault = override_settings(DV_EVIDENCE_VAULT_PATH=self.root)
        vault.enable()
        self.addCleanup(vault.disable)
        self.descriptors, refs = [], []
        for source_id, name, data, media in (
                ('SYNTH-AMO', 'amo.zip', fixture_originals.archive(end='2025-01-01'), 'application/zip'),
                ('SYNTH-VOTE', 'vote.html', fixture_originals.AN_HTML, 'text/html')):
            (self.root / name).write_bytes(data)
            ref = {'source_id': source_id, 'version_id': 'SYNTH-received-1',
                   'sha256': hashlib.sha256(data).hexdigest(), 'public_url': 'https://example.invalid/' + source_id}
            refs.append(ref)
            self.descriptors.append({**ref, 'original_path': name, 'media_type': media, 'byte_size': len(data)})
        self.amo, self.vote = refs
        snapshot = snapshot_fixture()
        snapshot['sources'] = [
            {'reference': ref, 'state': 'available', 'integrity': 'original_and_passages_checked',
             'passages': [{'id': index + 1, 'exact_text': fixture_originals.AN_HTML.decode() if index else 'SYNTH AMO received original.',
                 'locator': 'SYNTH original ' + str(index + 1), 'context': 'SYNTH complete received passage.'}]}
            for index, ref in enumerate(refs)]
        payload = {'title': 'SYNTH proposition limitée.', 'short_answer': 'SYNTH position au scrutin.',
            'main_caveat': 'SYNTH inconnus institutionnels conservés.', 'reasoning': ['SYNTH conclusion à vérifier.'],
            'evidence': [{'source_version': deepcopy(self.vote), 'exact_quote': 'SYNTH Alice', 'locator': 'SYNTH original 2'}],
            'acts': [{'scrutiny_id': 'AN:16:SCRUTIN:3289', 'included': True}],
            'actors': {'scope': 'personal', 'speaker': {'person_id': 'AN:PERSON:PA1'},
                'represented_entity': {'organization_id': None},
                'compared_actor': {'person_id': 'AN:PERSON:PA1', 'organization_id': None}},
            'catalogue': {
                'persons': [{'id': 'AN:PERSON:PA1', 'display_name': 'SYNTH Alice',
                             'institutional_ids': {'assemblee_nationale': 'PA1'}},
                    {'id': 'SYNTH:UNRELATED:PERSON', 'display_name': 'SYNTH contextual unknown'}],
                'organizations': [{'id': 'AN:ORG:PO1', 'name': 'SYNTH Groupe', 'kind': 'parliamentary_group'},
                    {'id': 'SYNTH:UNRELATED:GROUP', 'name': 'SYNTH contextual group', 'kind': 'parliamentary_group'}],
                'scrutinies': [{'scrutin_id': 'AN:16:SCRUTIN:3289', 'occurred_on': '2024-01-30',
                    'official_title': 'SYNTH whole vote', 'procedure': 'SYNTH ensemble',
                    'submitted_text_id': 'SYNTH:TEXT:1', 'text_reference_role': 'submitted', 'source_version': deepcopy(self.vote)}],
                'text_versions': [{'id': 'SYNTH:TEXT:1', 'source_version': deepcopy(self.vote)}],
                'vote_observations': [{'scrutiny_id': 'AN:16:SCRUTIN:3289', 'person_id': 'AN:PERSON:PA1',
                    'normalized_category': 'for', 'source_category': 'pours',
                    'affiliation_at_event_id': 'AN:ORG:PO1', 'source_version': deepcopy(self.vote)}],
                'affiliations': [{'person_id': 'AN:PERSON:PA1', 'organization_id': 'AN:ORG:PO1',
                    'status': 'observed_in_official_vote_list', 'temporal_kind': 'observed_at_scrutiny',
                    'starts_on': None, 'ends_on': None, 'observed_on': '2024-01-30', 'source_version': deepcopy(self.vote)}]}}
        snapshot['content']['public_payload'] = payload
        snapshot['content']['source_snapshot'] = deepcopy(refs)
        snapshot['claims'] = list(v._scalars(snapshot['content']))
        self.snapshot = snapshot

    def freeze(self, archetype=d.ARCHETYPES[0], **kwargs):
        self.snapshot.pop('conclusion', None)
        self.snapshot['claims'] = list(v._scalars(self.snapshot['content']))
        self.snapshot['conclusion'] = d.build_conclusion(self.snapshot, archetype=archetype, **kwargs)
        return self.snapshot

    def group(self):
        actors = self.snapshot['content']['public_payload']['actors']
        actors['scope'] = 'collective'
        actors['represented_entity']['organization_id'] = 'AN:ORG:PO1'
        actors['compared_actor']['organization_id'] = 'AN:ORG:PO1'

    def classify(self, snapshot=None, unknowns=()):
        snapshot = snapshot or self.snapshot
        report = pr.build_provenance_report(snapshot, source_root=self.root, received_sources=self.descriptors)
        return c.classify_snapshot(snapshot, provenance_report=report, unknowns=unknowns,
            source_root=self.root, received_sources=self.descriptors)

    def preflight(self, classification=None, **kwargs):
        classification = classification or self.classify()
        return f.build_preflight(self.snapshot, classification, rights_report=rights_fixture(self.snapshot),
            source_root=self.root, received_sources=self.descriptors, **kwargs)

    def necessity(self, classification, path):
        return next(n for n in classification['dependency_graph']['necessities'] if n['location'] == path)

    def test_six_closed_archetypes_and_complete_narrative_bindings(self):
        for archetype in d.ARCHETYPES:
            if archetype in {d.ARCHETYPES[1], d.ARCHETYPES[2], d.ARCHETYPES[5]}:
                self.group()
            kwargs = {'target_object_id': 'SYNTH:AMENDMENT:6', 'target_object_kind': 'amendment'} if archetype == d.ARCHETYPES[4] else {}
            declaration = self.freeze(archetype, **kwargs)['conclusion']
            self.assertEqual(d.validate_conclusion(self.snapshot, declaration), declaration)
            self.assertEqual(set(declaration), d.DECLARATION_FIELDS)
            self.assertTrue(declaration['narrative_bindings'])
            self.assertEqual(d.make_conclusion, d.build_conclusion)

    def test_personal_comparison_is_ready_with_unrelated_missing_group_visible(self):
        self.freeze(); classification = self.classify()
        path = '/public_payload/catalogue/organizations/1/id'
        record = next(x for x in classification['claims'] if x['location'] == path)
        self.assertEqual(record['provenance_status'], 'missing')
        self.assertFalse(record['necessary_for_conclusion'])
        report = self.preflight(classification)
        self.assertEqual(report['status'], 'ready', report['checks'])
        self.assertTrue(any(n['location'] == path for n in report['not_applicable_fields']))
        self.assertFalse(classification['dependency_graph']['summary']['absence_proven'])

    def test_four_comparison_archetypes_ready_documentarily_b_remains_required(self):
        base = deepcopy(self.snapshot)
        for archetype in (d.ARCHETYPES[0], d.ARCHETYPES[1], d.ARCHETYPES[2], d.ARCHETYPES[5]):
            self.snapshot = deepcopy(base)
            if archetype != d.ARCHETYPES[0]:
                self.group()
            self.freeze(archetype); classification = self.classify(); preflight = self.preflight(classification)
            with self.subTest(archetype=archetype):
                self.assertEqual(preflight['status'], 'ready', preflight['checks'])
                self.assertTrue(any(c['layer'] == 'semantic' and c['necessary_for_conclusion'] for c in classification['claims']))
                self.assertTrue(classification['dependency_graph']['summary']['independent_meaning_verification_required'])

    def test_same_dossier_group_and_period_require_different_dependencies(self):
        self.group(); self.freeze(d.ARCHETYPES[1]); event = self.classify()
        path = '/public_payload/catalogue/vote_observations/0/affiliation_at_event_id'
        self.assertTrue(self.necessity(event, path)['necessary_for_conclusion'])
        temporal = {'kind': 'period', 'scrutiny_id': 'AN:16:SCRUTIN:3289', 'starts_on': '2024-01-01', 'ends_on': '2024-02-01'}
        self.freeze(d.ARCHETYPES[1], temporal_scope=temporal)
        period = self.classify(); report = self.preflight(period)
        self.assertEqual(report['status'], 'hold')
        self.assertNotEqual(event['dependency_graph']['graph_digest'], period['dependency_graph']['graph_digest'])
        self.assertTrue(period['dependency_graph']['counterevidence_requirements']['affiliation_change']['applicable'])

    def test_unknown_bounds_not_required_for_exact_group_observation(self):
        self.group(); self.freeze(d.ARCHETYPES[1])
        annotation = {'location': '/public_payload/catalogue/affiliations/0/ends_on', 'unknown_kind': 'membership_end_unknown',
            'motif': 'SYNTH observation, aucune fin institutionnelle.', 'limits': ['SYNTH aucune période déduite.']}
        classification = self.classify(unknowns=[annotation])
        self.assertFalse(classification['unknowns'][0]['necessary_for_conclusion'])
        self.assertEqual(self.preflight(classification)['status'], 'ready')
        self.freeze(d.ARCHETYPES[1], narrative_facets={'/public_payload/main_caveat': ['group_period']})
        period = self.classify(unknowns=[annotation])
        self.assertTrue(period['unknowns'][0]['necessary_for_conclusion'])
        self.assertEqual(self.preflight(period)['status'], 'hold')

    def test_new_period_reserve_reactivates_dependency_and_old_binding_refuses(self):
        self.group(); self.freeze(d.ARCHETYPES[1]); old = deepcopy(self.snapshot['conclusion'])
        self.snapshot['content']['public_payload']['main_caveat'] += ' SYNTH revendication d’une période.'
        self.snapshot['claims'] = list(v._scalars(self.snapshot['content']))
        with self.assertRaisesRegex(ValidationError, 'narrative_binding_changed'):
            d.validate_conclusion(self.snapshot, old)
        self.freeze(d.ARCHETYPES[1], narrative_facets={'/public_payload/main_caveat': ['group_period']})
        graph = self.classify()['dependency_graph']
        self.assertTrue(graph['counterevidence_requirements']['affiliation_change']['applicable'])

    def test_client_necessary_flag_cannot_declassify_unknown_or_graph(self):
        self.group(); self.freeze(d.ARCHETYPES[1], narrative_facets={'/public_payload/main_caveat': ['group_period']})
        report = self.classify()
        graph = deepcopy(report['dependency_graph'])
        graph['necessities'][0]['necessary_for_conclusion'] = not graph['necessities'][0]['necessary_for_conclusion']
        graph['graph_digest'] = sha256_digest({k: v for k, v in graph.items() if k != 'graph_digest'})
        with self.assertRaisesRegex(ValidationError, 'dependency_graph_binding_or_rules_changed'):
            d.validate_dependency_graph(self.snapshot, report, graph, source_root=self.root, received_sources=self.descriptors)
        report['claims'][0]['necessary_for_conclusion'] = False
        report['classification_digest'] = sha256_digest({k: v for k, v in report.items() if k != 'classification_digest'})
        with self.assertRaisesRegex(ValidationError, 'classification_binding_or_routing_changed'):
            c.validate_classification(self.snapshot, report, source_root=self.root, received_sources=self.descriptors)

    def test_full_snapshot_conclusion_cannot_downgrade_to_legacy_profile(self):
        self.freeze(); report = self.classify()
        self.assertEqual(report['contract'], c.CONTRACT_V2)
        with self.assertRaisesRegex(ValidationError, 'classification_conclusion_not_in_full_snapshot'):
            c.classify_snapshot(self.snapshot, conclusion={})
        report['contract'] = c.CONTRACT
        with self.assertRaisesRegex(ValidationError, 'classification_contract_required'):
            c.validate_classification(self.snapshot, report)

    def test_closed_conclusion_refuses_free_needed_override_and_actor_alias(self):
        self.freeze(); declaration = deepcopy(self.snapshot['conclusion'])
        declaration['necessary_for_conclusion'] = False
        with self.assertRaisesRegex(ValidationError, 'closed_contract_required'):
            d.validate_conclusion(self.snapshot, declaration)
        declaration = deepcopy(self.snapshot['conclusion']); declaration['actors']['voter_person_id'] = 'AN:PERSON:OTHER'
        with self.assertRaisesRegex(ValidationError, 'actor_binding_changed'):
            d.validate_conclusion(self.snapshot, declaration)

    def test_wrong_person_right_group_cannot_supply_personal_vote(self):
        self.group()
        self.snapshot['content']['public_payload']['actors']['compared_actor']['person_id'] = 'AN:PERSON:OTHER'
        self.freeze(d.ARCHETYPES[2]); report = self.preflight()
        self.assertEqual(next(x for x in report['checks'] if x['code'] == f.REASONS[4])['status'], 'hold')

    def test_collective_speech_personal_vote_needs_only_exact_voter_not_whole_group(self):
        self.group()
        catalogue = self.snapshot['content']['public_payload']['catalogue']
        catalogue['vote_observations'].append({**deepcopy(catalogue['vote_observations'][0]),
            'person_id': 'SYNTH:UNRELATED:PERSON', 'normalized_category': 'against', 'source_category': 'contres'})
        self.freeze(d.ARCHETYPES[2]); classification = self.classify()
        for path in ('/public_payload/catalogue/vote_observations/1/person_id',
                     '/public_payload/catalogue/persons/1/id'):
            self.assertFalse(self.necessity(classification, path)['necessary_for_conclusion'])
        self.assertEqual(self.preflight(classification)['status'], 'ready')
        # A population distribution really does need this missing nominal row.
        self.freeze(d.ARCHETYPES[1]); distribution = self.classify()
        self.assertTrue(self.necessity(distribution, '/public_payload/catalogue/vote_observations/1/person_id')['necessary_for_conclusion'])
        self.assertEqual(self.preflight(distribution)['status'], 'hold')

    def test_collective_speech_without_exact_voter_stays_hold_without_group_substitute(self):
        self.group()
        self.snapshot['content']['public_payload']['actors']['compared_actor']['person_id'] = None
        self.freeze(d.ARCHETYPES[2]); classification = self.classify(); report = self.preflight(classification)
        self.assertFalse(any(n['necessary_for_conclusion'] for n in classification['dependency_graph']['necessities']
            if n['location'].startswith('/public_payload/catalogue/vote_observations/')))
        check = next(x for x in report['checks'] if x['code'] == f.REASONS[4])
        self.assertEqual(check['status'], 'hold')
        self.assertIn('/public_payload/actors/compared_actor/person_id', check['locations'])

    def test_collective_speech_personal_vote_still_requires_exact_event_group(self):
        self.group()
        self.snapshot['content']['public_payload']['catalogue']['vote_observations'][0]['affiliation_at_event_id'] = 'SYNTH:OTHER:GROUP'
        self.freeze(d.ARCHETYPES[2]); report = self.preflight()
        self.assertEqual(next(x for x in report['checks'] if x['code'] == f.REASONS[4])['status'], 'hold')

    def test_correct_group_wrong_scrutiny_or_wrong_date_refuses(self):
        self.group()
        row = self.snapshot['content']['public_payload']['catalogue']['vote_observations'][0]
        row['scrutiny_id'] = 'AN:16:SCRUTIN:9999'
        self.freeze(d.ARCHETYPES[1]); self.assertEqual(self.preflight()['status'], 'hold')
        row['scrutiny_id'] = 'AN:16:SCRUTIN:3289'
        self.snapshot['content']['public_payload']['catalogue']['scrutinies'][0]['occurred_on'] = '2024-01-31'
        self.freeze(d.ARCHETYPES[1]); report = self.preflight()
        self.assertEqual(report['status'], 'hold')
        self.assertIn('/public_payload/catalogue/scrutinies/0/occurred_on', next(x for x in report['checks'] if x['code'] == f.REASONS[7])['locations'])

    def test_parliamentary_group_cannot_certify_party_identity(self):
        self.group()
        self.freeze(d.ARCHETYPES[1], narrative_facets={'/public_payload/short_answer': ['party_identity']})
        self.assertEqual(self.preflight()['status'], 'hold')

    def test_absent_nominal_row_or_changed_position_never_means_opposition(self):
        positions = self.snapshot['content']['public_payload']['catalogue']['vote_observations']
        positions[0]['normalized_category'] = 'against'
        self.freeze()
        self.assertEqual(self.preflight()['status'], 'hold')
        positions.clear(); self.freeze()
        self.assertEqual(self.preflight()['status'], 'hold')

    def test_withdrawn_target_needs_explicit_primary_proof_without_fake_vote(self):
        self.freeze(d.ARCHETYPES[4], target_object_id='SYNTH:AMENDMENT:6', target_object_kind='amendment')
        report = self.preflight()
        self.assertEqual(report['status'], 'hold')
        for code in (f.REASONS[4], f.REASONS[5], f.REASONS[6]):
            self.assertEqual(next(x for x in report['checks'] if x['code'] == code)['status'], 'not_applicable')
        self.assertEqual(next(x for x in report['checks'] if x['code'] == f.REASONS[3])['status'], 'hold')

    def test_noncomparable_minister_needs_declared_capacity_not_fake_nominal_vote(self):
        self.snapshot['content']['public_payload']['actors']['scope'] = 'government'
        self.snapshot['content']['public_payload']['actors']['speaker']['person_id'] = 'SYNTH:UNRESOLVED:MINISTER'
        self.snapshot['content']['public_payload']['actors']['compared_actor']['person_id'] = None
        self.freeze(d.ARCHETYPES[3]); classification = self.classify(); report = self.preflight(classification)
        self.assertEqual(report['status'], 'hold')
        self.assertEqual(next(x for x in report['checks'] if x['code'] == f.REASONS[5])['status'], 'not_applicable')
        self.assertFalse(any(n['necessary_for_conclusion'] for n in classification['dependency_graph']['necessities'] if n['location'].startswith('/public_payload/catalogue/persons/')))

    def test_explicit_observed_denominator_no_unlisted_person_as_opposition(self):
        self.group(); self.freeze(d.ARCHETYPES[5]); declaration = self.snapshot['conclusion']
        self.assertEqual(declaration['denominator']['member_person_ids'], ['AN:PERSON:PA1'])
        self.assertEqual(declaration['denominator']['kind'], 'listed_vote_positions')
        self.assertEqual(self.preflight()['status'], 'ready')
        forged = deepcopy(declaration); forged['denominator']['member_person_ids'].append('SYNTH:UNLISTED')
        with self.assertRaisesRegex(ValidationError, 'denominator_not_exact_server_population'):
            d.validate_conclusion(self.snapshot, forged)

    def test_claiming_all_institutional_members_reactivates_exhaustiveness(self):
        self.group(); self.freeze(d.ARCHETYPES[5]); denominator = deepcopy(self.snapshot['conclusion']['denominator'])
        denominator['kind'] = 'observed_group_members'
        self.freeze(d.ARCHETYPES[5], denominator=denominator)
        graph = self.classify()['dependency_graph']
        self.assertTrue(graph['finding_applicability']['institutional_exhaustiveness_not_established']['required'])
        self.assertEqual(self.preflight()['status'], 'hold')

    def test_a3_source_authentic_but_selected_support_inadequate_is_never_exempt(self):
        self.freeze(); graph = self.classify()['dependency_graph']
        self.assertTrue(graph['finding_applicability']['expected_entity_not_in_selected_support']['required'])
        self.assertTrue(graph['counterevidence_requirements']['quote_out_of_context']['applicable'])
        self.assertFalse(graph['summary']['absence_proven'])

    def test_good_quote_inverse_context_is_retained_separately(self):
        self.freeze()
        source = self.snapshot['sources'][1]
        source['passages'][0]['exact_text'] = 'SYNTH Alice ne soutient pas ce vote. Elle annonce soutenir ce vote si la condition est remplie.'
        # Local scan witness only: it is not a source replacement certificate.
        claim = deepcopy(self.snapshot['claims'][0])
        claim.update(support=[{'passage_id': 2, 'quote': 'Elle annonce soutenir ce vote'}], against=[])
        first = {'fingerprint': sha256_digest(self.snapshot), 'claims': [claim]}
        report = counter.scan_counterevidence(self.snapshot, first)
        self.assertIn('negation_or_condition_outside_quote', {x['code'] for x in report['findings']})
        self.assertFalse(report['summary']['counterevidence_complete'])

    def test_source_alias_same_sha_is_not_an_original_version_binding(self):
        self.freeze()
        self.snapshot['content']['public_payload']['evidence'][0]['source_version']['source_id'] = 'SYNTH:ALIAS'
        self.freeze(); report = self.preflight()
        self.assertEqual(next(x for x in report['checks'] if x['code'] == f.REASONS[0])['status'], 'hold')

    def test_object_observation_for_other_target_cannot_bind_selected_amendment(self):
        payload = self.snapshot['content']['public_payload']
        payload['object_observations'] = [{'object_id': 'SYNTH:AMENDMENT:OTHER', 'object_kind': 'amendment',
            'status': 'withdrawn', 'official_title': 'SYNTH other amendment', 'source_version': self.vote,
            'locator': 'SYNTH original 2', 'exact_quote': 'SYNTH Alice'}]
        path = '/public_payload/object_observations/0/object_id'
        with self.assertRaisesRegex(ValidationError, 'observation_is_not_selected_target'):
            self.freeze(d.ARCHETYPES[4], target_object_id='SYNTH:AMENDMENT:6', target_object_kind='amendment',
                evidence_bindings=[{'predicate': 'target_object_identity', 'location': path,
                    'value_sha256': sha256_digest('SYNTH:AMENDMENT:OTHER')}])

    def test_original_graph_validator_recomputes_sources_not_only_json_status(self):
        self.freeze(); classification = self.classify()
        (self.root / 'vote.html').write_bytes(fixture_originals.AN_HTML + b'SYNTH changed')
        with self.assertRaises(ValidationError):
            d.validate_dependency_graph(self.snapshot, classification, classification['dependency_graph'],
                source_root=self.root, received_sources=self.descriptors)

    def test_hold_explains_required_dependency_and_resolution(self):
        self.freeze(d.ARCHETYPES[4], target_object_id='SYNTH:AMENDMENT:6', target_object_kind='amendment')
        report = self.preflight()
        keys = {'blocking_claim_ids', 'dependency_rule_id', 'why_required_for_this_conclusion',
            'what_evidence_would_resolve_it', 'can_resolve_without_model', 'can_resolve_without_new_source'}
        for check in report['checks']:
            if check['status'] == 'hold':
                self.assertTrue(keys <= set(check))

    def test_current_expiry_is_checked_after_historical_v2_receipt(self):
        self.freeze(); classification = self.classify()
        report = f.build_preflight(self.snapshot, classification,
            rights_report=rights_fixture(self.snapshot, expires_on='2000-01-02'), as_of=date(2000, 1, 1),
            source_root=self.root, received_sources=self.descriptors)
        self.assertEqual(report['status'], 'ready')
        artifact = SimpleNamespace(kind='preflight', revision_fingerprint=report['fingerprint'],
            payload_digest=sha256_digest(report), payload=report)
        with self.assertRaisesRegex(ValidationError, 'current_rights_unqualified_or_expired'):
            f.require_received_preflight(self.snapshot, classification, report, artifact,
                source_root=self.root, received_sources=self.descriptors)

    def test_cost_required_b_separate_from_full_coverage_and_zero_allocated(self):
        self.freeze(); classification = self.classify(); cost = self.preflight(classification)['cost']
        expected = sum(x['layer'] == 'semantic' and x['necessary_for_conclusion'] for x in classification['claims'])
        self.assertEqual(cost['semantic_assertions'], expected)
        self.assertGreaterEqual(cost['full_assertions_by_layer']['semantic'], expected)
        self.assertEqual(cost['authorization'], 'NOT_AUTHORIZED')
        self.assertEqual(cost['actual_phases_reserved'], 0)
        self.assertEqual(cost['actual_phases_launched'], 0)

    def test_legacy_profile_still_has_no_graph_or_conclusion_mutation(self):
        report = self.classify()
        self.assertEqual(report['contract'], c.CONTRACT)
        self.assertNotIn('dependency_graph', report)
        self.assertNotIn('conclusion', report)
        c.validate_classification(self.snapshot, report, source_root=self.root, received_sources=self.descriptors)


class ConclusionOriginalObservationsTests(AtelierTestCase):
    """Closed public0.7 plus actual new SYNTH original-format recomputation."""
    received = fixture_v2.ClassificationV2Tests.received
    bound_speech = fixture_v2.ClassificationV2Tests.bound_speech

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix='observations-SYNTH-')
        self.addCleanup(temp.cleanup); self.root = Path(temp.name)
        settings = override_settings(DV_EVIDENCE_VAULT_PATH=self.root)
        settings.enable(); self.addCleanup(settings.disable)
        self.amo = self.received('amo.zip', fixture_originals.archive(), 'application/zip')
        self.vote = self.received('vote.html', fixture_originals.AN_HTML, 'text/html')
        self.scrutiny = self.make_scrutiny(self.vote, source_identifier='3289')
        self.scrutiny.stable_id = 'AN:16:SCRUTIN:3289'; self.scrutiny.occurred_on = date(2024, 1, 30)
        self.scrutiny.save()
        self.person = m.Person.objects.create(stable_id='AN:PERSON:PA1', display_name='SYNTH Alice',
            institutional_ids={'assemblee_nationale': 'PA1'})
        self.group = m.Organization.objects.create(stable_id='AN:ORG:PO1', name='SYNTH Groupe', kind='parliamentary_group')
        m.Affiliation.objects.create(person=self.person, organization=self.group, status='official_group_mandate',
            starts_on=date(2022, 6, 29), source_document=self.amo)
        m.Affiliation.objects.create(person=self.person, organization=self.group, status='observed_in_official_vote_list',
            starts_on=self.scrutiny.occurred_on, ends_on=self.scrutiny.occurred_on,
            observed_on=self.scrutiny.occurred_on, source_document=self.vote)
        m.VoteObservation.objects.create(person=self.person, scrutiny=self.scrutiny, affiliation_at_event=self.group,
            source_category='pours', normalized_category='for', mapping_rule='SYNTH', source_document=self.vote)
        self.revision = self.bound_speech()
        self.snapshot = v.snapshot(self.revision)
        self.snapshot['scope'] = 'synthetic'
        self.snapshot['content']['public_payload']['public_contract_version'] = 'dv.content/0.7.0-conclusion-draft.1'
        self.snapshot['content']['public_payload']['catalogue']['projection_ruleset'] = 'dv.provenance-rules/2'
        self.snapshot['content']['public_payload']['object_observations'] = []

    def original(self, filename, data, url, quote, locator):
        document = self.received(filename, data, 'text/html')
        document.source.discovery_url = url; document.source.document_url = url
        document.source.save()
        ref = {'source_id': document.source.stable_id, 'version_id': document.version_id,
               'sha256': document.sha256, 'public_url': url}
        self.snapshot['sources'].append({'reference': ref, 'state': 'available',
            'integrity': 'synthetic_no_original', 'passages': [{'id': 900 + document.pk,
                'exact_text': quote, 'locator': locator, 'context': data.decode()}]})
        self.snapshot['content']['source_snapshot'].append(ref)
        return document, ref

    def report(self, archetype, **kwargs):
        from jsonschema import Draft202012Validator, FormatChecker
        from atelier.services.contracts import publication_schema_for
        schema = publication_schema_for('dv.content/0.7.0-conclusion-draft.1')
        errors = list(Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/speechVote'},
            format_checker=FormatChecker()).iter_errors(self.snapshot['content']['public_payload']))
        self.assertFalse(errors, [(list(e.path), e.message) for e in errors][:5])
        self.snapshot['claims'] = list(v._scalars(self.snapshot['content']))
        self.snapshot['conclusion'] = d.build_conclusion(self.snapshot, archetype=archetype, **kwargs)
        classification = c.classify_snapshot(self.snapshot, provenance_report=pr.build_provenance_report(self.snapshot))
        return classification, f.build_preflight(self.snapshot, classification, rights_report=rights_fixture(self.snapshot))

    def collective_personal_projection(self):
        """Use the actual producer plus the original-certified personal selector."""
        from atelier.services.catalogue import prepare_public_snapshot, source_ref
        comparison = self.revision.comparison
        declaration = comparison.declaration
        declaration.position_scope = 'collective'
        declaration.speaker_organization = self.group
        declaration.save()
        payload = deepcopy(self.snapshot['content']['public_payload'])
        for key in ('acts', 'evidence', 'attribution'):
            payload.pop(key, None)
        payload['personal_vote_selection'] = {'person_id': self.person.stable_id,
            'scrutiny_id': self.scrutiny.stable_id, 'source_version': source_ref(self.vote)}
        public, refs = prepare_public_snapshot('speech_vote', payload, [], comparison=comparison)
        self.revision.public_payload = public; self.revision.source_snapshot = refs; self.revision.save()
        self.snapshot = v.snapshot(self.revision); self.snapshot['scope'] = 'synthetic'
        self.snapshot['content']['main_caveat'] = 'SYNTH la distribution de groupe ne prouve pas une position personnelle.'
        self.snapshot['content']['reasoning'] = 'SYNTH conclusion individuelle soumise à un jugement indépendant.'

    def test_closed_group_machine_projection_is_context_only_for_individual_not_collective_or_majority(self):
        self.collective_personal_projection()
        for archetype in (d.ARCHETYPES[2], d.ARCHETYPES[1], d.ARCHETYPES[5]):
            with self.subTest(archetype=archetype):
                classification, _ = self.report(archetype)
                graph = classification['dependency_graph']
                self.assertEqual(graph['collective_context_projection']['status'], 'reconstructed')
                required = archetype != d.ARCHETYPES[2]
                for path in ('/public_payload/qualification_material/collective_vote_distribution/voices/0/person_id',
                             '/public_payload/qualification_material/collective_vote_distribution/distribution/for',
                             '/public_payload/collective_distribution/included_member_ids/0'):
                    record = next(n for n in graph['necessities'] if n['location'] == path)
                    self.assertEqual(record['necessary_for_conclusion'], required)
                    self.assertEqual(record['applicability'], 'required' if required else 'not_applicable')
                reserve = next(n for n in graph['necessities'] if n['location'].endswith('/collective_vote_distribution/reserve'))
                self.assertTrue(reserve['necessary_for_conclusion'])
                claim = next(x for x in classification['claims'] if x['location'] == reserve['location'])
                self.assertEqual(claim['layer'], 'semantic')
                for path in ('/main_caveat', '/reasoning'):
                    self.assertTrue(next(n for n in graph['necessities'] if n['location'] == path)['necessary_for_conclusion'])
        # A changed explicit denominator facet reactivates the table even on A3.
        classification, _ = self.report(d.ARCHETYPES[2], narrative_facets={
            '/reasoning': ['majority_denominator']})
        self.assertTrue(classification['dependency_graph']['collective_context_projection']['required_for_conclusion'])

    def test_group_projection_extensions_tampering_and_client_necessity_cannot_exempt_free_prose(self):
        self.collective_personal_projection()
        original = deepcopy(self.snapshot)
        for mutation in ('count', 'source_alias', 'free_prose'):
            with self.subTest(mutation=mutation):
                self.snapshot = deepcopy(original)
                projection = self.snapshot['content']['public_payload']['qualification_material']['collective_vote_distribution']
                if mutation == 'count':
                    projection['distribution']['for'] += 1
                elif mutation == 'source_alias':
                    projection['voices'][0]['sources'][0]['source_id'] = 'SYNTH:ALIAS:SAME-SHA'
                else:
                    projection['political_label'] = 'SYNTH le groupe vote unanimement comme son porte-parole.'
                classification, _ = self.report(d.ARCHETYPES[2])
                graph = classification['dependency_graph']
                self.assertEqual(graph['collective_context_projection']['status'], 'not_reconstructed')
                material = [n for n in graph['necessities'] if n['location'].startswith(
                    '/public_payload/qualification_material/collective_vote_distribution/')
                    and any(claim['claim_id'] == n['claim_id'] and claim['layer'] == 'semantic'
                            for claim in classification['claims'])]
                self.assertTrue(material and all(n['necessary_for_conclusion'] for n in material))
                if mutation == 'free_prose':
                    label = next(c for c in classification['claims'] if c['location'].endswith('/political_label'))
                    self.assertEqual(label['layer'], 'semantic')
                forged = deepcopy(classification)
                for claim in forged['claims']:
                    if claim['location'].startswith('/public_payload/qualification_material/collective_vote_distribution/'):
                        claim['necessary_for_conclusion'] = False
                forged.pop('classification_digest'); forged['classification_digest'] = sha256_digest(forged)
                with self.assertRaisesRegex(ValidationError, 'binding_or_routing_changed'):
                    c.validate_classification(self.snapshot, forged)

    def capacity(self, selected_label='SYNTH Ministre'):
        raw = b'<html><p id="par_1573"><span class="orateur_nom">M. SYNTH Ministre,</span> ministre. Meme avis.</p></html>'
        _, ref = self.original('capacity.html', raw,
            'https://www.senat.fr/seances/s202401/s20240130/s20240130013.html', 'Meme avis.', 'SYNTH capacity paragraph')
        payload = self.snapshot['content']['public_payload']
        actors = payload['actors']
        actors['scope'] = 'indeterminate'
        actors['speaker'].update(person_id=None, label=selected_label, capacity='ministre')
        actors['compared_actor'].update(person_id=None, organization_id=None)
        actors['represented_entity']['organization_id'] = None
        payload['attribution'].update(person_id=None, organization_id=None, speaker_label=selected_label, capacity='ministre')
        payload['compared_actor_scope'] = 'indeterminate'; payload['declaration_quote'] = 'Meme avis.'
        payload['declaration_source_url'] = ref['public_url']
        payload['evidence'] = [{'source_version': ref, 'locator': 'SYNTH capacity paragraph',
            'exact_quote': 'Meme avis.', 'transcription_status': 'source_text'}]
        payload['qualification_material']['actors'] = deepcopy(actors)
        payload['qualification_material']['declaration_source'].update(public_url=ref['public_url'],
            document_version=ref['version_id'], sha256=ref['sha256'], locator='SYNTH capacity paragraph')
        return [{'predicate': 'speaker_capacity', 'location': '/public_payload/actors/speaker/capacity',
                 'value_sha256': sha256_digest('ministre')}]

    def withdrawal(self, original_number='6', observed_quote='Retire', observed_locator='SYNTH amendment status'):
        raw = ('<html><p>(n° 298)</p><h1>AMENDEMENT</h1><p>N° ' + original_number
            + '</p><table><tr><td>Retiré</td></tr></table></html>').encode()
        _, ref = self.original('amendment.html', raw,
            'https://www.senat.fr/amendements/2025-2026/298/Amdt_6.html', 'Retiré', 'SYNTH amendment status')
        payload = self.snapshot['content']['public_payload']
        identifier = 'SENAT:2025-2026:AMENDMENT:298:6'
        payload['object_observations'] = [{'object_id': identifier, 'object_kind': 'amendment', 'status': 'withdrawn',
            'official_title': 'AMENDEMENT', 'source_version': ref, 'locator': observed_locator,
            'exact_quote': 'Retiré' if observed_quote == 'Retire' else observed_quote}]
        return {'target_object_id': identifier, 'target_object_kind': 'amendment',
            'evidence_bindings': [{'predicate': 'target_object_identity', 'location': '/public_payload/object_observations/0/object_id', 'value_sha256': sha256_digest(identifier)},
                {'predicate': 'withdrawal', 'location': '/public_payload/object_observations/0/status', 'value_sha256': sha256_digest('withdrawn')}]}

    def test_exact_original_declared_capacity_admits_documentary_noncomparability(self):
        bindings = self.capacity()
        classification, preflight = self.report(d.ARCHETYPES[3], evidence_bindings=bindings)
        capacity = next(c for c in classification['claims'] if c['location'] == '/public_payload/actors/speaker/capacity')
        self.assertEqual((capacity['layer'], capacity['provenance_status']), ('deterministic', 'proven'))
        self.assertEqual(preflight['status'], 'ready', preflight['checks'])
        self.assertEqual(next(x for x in preflight['checks'] if x['code'] == f.REASONS[5])['status'], 'not_applicable')

    def test_noncomparability_invalid_selected_text_binding_blocks_exact_object(self):
        bindings = self.capacity()
        payload = self.snapshot['content']['public_payload']
        payload['scrutiny_text_binding_refs'] = [{
            'scrutiny_id': payload['catalogue']['scrutinies'][0]['scrutin_id'],
            'version': 999, 'payload_digest': 'a' * 64}]
        classification, preflight = self.report(d.ARCHETYPES[3], evidence_bindings=bindings)
        self.assertEqual(classification['dependency_graph']['submitted_text_binding']['status'], 'invalid')
        self.assertEqual(preflight['status'], 'hold')
        self.assertEqual(next(x for x in preflight['checks'] if x['code'] == f.REASONS[3])['status'], 'hold')
        self.assertEqual(next(x for x in preflight['checks'] if x['code'] == f.REASONS[5])['status'], 'not_applicable')

    def test_capacity_of_other_named_speaker_is_not_selected_actor(self):
        bindings = self.capacity(selected_label='SYNTH Autre Ministre')
        _, report = self.report(d.ARCHETYPES[3], evidence_bindings=bindings)
        self.assertEqual(report['status'], 'hold')

    def test_source_capacity_does_not_resolve_uid_or_current_mandate(self):
        bindings = self.capacity(); classification, _ = self.report(d.ARCHETYPES[3], evidence_bindings=bindings)
        self.assertFalse(any(n['necessary_for_conclusion'] for n in classification['dependency_graph']['necessities']
            if n['location'].startswith('/public_payload/catalogue/persons/')))
        self.assertIsNone(self.snapshot['conclusion']['actors']['declarant_person_id'])

    def test_public07_cannot_omit_conclusion_and_use_legacy_gate(self):
        with self.assertRaisesRegex(ValidationError, 'conclusion_required_for_public_profile'):
            c.classify_snapshot(self.snapshot)

    def test_actual_original_withdrawal_identity_same_record_admits_restricted_conclusion(self):
        kwargs = self.withdrawal(); classification, preflight = self.report(d.ARCHETYPES[4], **kwargs)
        self.assertEqual(preflight['status'], 'ready', preflight['checks'])
        for field in ('object_id', 'status'):
            cert = next(c for c in classification['provenance_report']['certificates'] if c['location'] == '/public_payload/object_observations/0/' + field)
            self.assertEqual(cert['status'], 'proven')
        self.assertFalse(classification['dependency_graph']['finding_applicability']['submitted_version_unestablished']['required'])

    def test_whole_vote_does_not_replace_different_amendment_identity(self):
        kwargs = self.withdrawal(original_number='7'); _, report = self.report(d.ARCHETYPES[4], **kwargs)
        self.assertEqual(report['status'], 'hold')

    def test_original_withdrawal_status_with_wrong_selected_quote_or_locator_stays_hold(self):
        kwargs = self.withdrawal(observed_quote='SYNTH invented', observed_locator='SYNTH other passage')
        _, preflight = self.report(d.ARCHETYPES[4], **kwargs)
        self.assertEqual(preflight['status'], 'hold')


class ConclusionSelectedTextBindingTests(AtelierTestCase):
    """Fresh ORM binding readback; original sources, no old FK mutation."""
    original = binding_fixtures.ScrutinyTextBindingTests.original

    def setUp(self):
        super().setUp()
        fixture = ConclusionOriginalObservationsTests(methodName='runTest')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.root
        self.sequence = fixture.sequence + 100
        self.scrutiny = fixture.scrutiny
        self.revision = fixture.revision
        self.dossier_doc = self.original('https://www.assemblee-nationale.fr/dyn/16/dossiers/SYNTH-motion',
            '<html><h1>SYNTH dossier motion</h1></html>')
        self.literal_doc = self.original('https://www.assemblee-nationale.fr/dyn/16/textes/SYNTH-motion-literal',
            '<html><h1>SYNTH motion originale autonome</h1><p>SYNTH texte littéral de motion.</p></html>')
        self.dossier = m.LegislativeDossier.objects.create(stable_id='SYNTH:NEW:MOTION:DOSSIER',
            institution='Assemblée nationale', official_title='SYNTH dossier motion',
            official_url=self.dossier_doc.source.document_url, status='SYNTH')
        self.motion = m.TextVersion.objects.create(stable_id='SYNTH:NEW:MOTION:LITERAL', dossier=self.dossier,
            kind='motion', official_title='SYNTH motion originale autonome', version_label='SYNTH literal',
            stage='SYNTH', relation='submitted', occurred_on=self.scrutiny.occurred_on,
            official_url=self.literal_doc.source.document_url, source_document=self.literal_doc)

    def selected(self, literal_status='not_received'):
        from atelier.services.catalogue import source_ref, build_catalogue
        payload = {'contract': 'dv.scrutiny-text-binding/1', 'version': 1,
            'scrutiny_id': self.scrutiny.stable_id, 'submitted_text_id': self.motion.stable_id,
            'legislative_dossier_id': self.dossier.stable_id, 'source_version': source_ref(self.fixture.vote),
            'text_source_version': source_ref(self.literal_doc), 'dossier_source_version': source_ref(self.dossier_doc),
            'procedure': self.scrutiny.procedure, 'object_kind': 'motion', 'literal_text_status': literal_status,
            'source_locator': 'SYNTH original vote source', 'exact_source_quote': 'SYNTH Alice', 'actor_id': 1}
        binding = m.ScrutinyTextBinding.objects.create(scrutiny=self.scrutiny, submitted_text=self.motion,
            source_document=self.fixture.vote, version=1, procedure=self.scrutiny.procedure, actor_id=1,
            payload=payload, payload_digest=sha256_digest(payload))
        ref = {'scrutiny_id': self.scrutiny.stable_id, 'version': 1, 'payload_digest': binding.payload_digest}
        catalogue, refs = build_catalogue([self.scrutiny.stable_id], profile='dv.catalogue/2',
            provenance_ruleset='dv.provenance-rules/2', binding_refs=[ref])
        for retained in self.fixture.snapshot['content']['source_snapshot']:
            if retained not in refs:
                refs.append(deepcopy(retained))
                # The capacity fixture has a verified original plus an in-
                # memory passage. Materialize that SYNTH passage before the
                # server snapshot reloads this new ORM binding test case.
                source = next(s for s in self.fixture.snapshot['sources'] if s['reference'] == retained)
                document = m.DocumentVersion.objects.get(source__stable_id=retained['source_id'],
                    version_id=retained['version_id'], sha256=retained['sha256'])
                for passage in source.get('passages', []):
                    m.EvidencePassage.objects.get_or_create(document=document,
                        exact_text=passage['exact_text'], locator=passage['locator'],
                        defaults={'context': passage.get('context', ''), 'public_url': retained['public_url']})
        public = deepcopy(self.fixture.snapshot['content']['public_payload'])
        public['catalogue'] = catalogue; public['scrutiny_text_binding_refs'] = [ref]
        self.revision.public_payload = public; self.revision.source_snapshot = refs
        self.revision.snapshot_digest = sha256_digest(public); self.revision.save()
        self.snapshot = v.snapshot(self.revision); self.snapshot['scope'] = 'synthetic'
        return binding

    def report(self, archetype=d.ARCHETYPES[0], **kwargs):
        self.snapshot.pop('conclusion', None)
        self.snapshot['claims'] = list(v._scalars(self.snapshot['content']))
        self.snapshot['conclusion'] = d.build_conclusion(self.snapshot, archetype=archetype, **kwargs)
        classification = c.classify_snapshot(self.snapshot, provenance_report=pr.build_provenance_report(self.snapshot))
        return classification, f.build_preflight(self.snapshot, classification, rights_report=rights_fixture(self.snapshot))

    def text_check(self, preflight):
        return next(x for x in preflight['checks'] if x['code'] == f.REASONS[5])

    def test_received_procedural_binding_with_not_received_literal_stays_hold(self):
        old_fk = self.scrutiny.submitted_text_id
        self.selected(); classification, report = self.report()
        self.assertEqual(self.text_check(report)['status'], 'hold')
        self.assertFalse(self.text_check(report)['can_resolve_without_new_source'])
        readback = classification['dependency_graph']['submitted_text_binding']
        self.assertEqual((readback['status'], readback['literal_text_status']), ('validated', 'not_received'))
        self.assertIs(readback['literal_document_established'], False)
        self.scrutiny.refresh_from_db(); self.assertEqual(self.scrutiny.submitted_text_id, old_fk)

    def test_received_literal_original_and_exact_binding_establish_only_submitted_text_check(self):
        self.selected('received_literal_document'); classification, report = self.report()
        self.assertEqual(self.text_check(report)['status'], 'pass', self.text_check(report))
        self.assertIs(classification['dependency_graph']['submitted_text_binding']['literal_document_established'], True)
        self.assertTrue(classification['dependency_graph']['summary']['independent_meaning_verification_required'])

    def test_stale_selector_same_sha_source_alias_or_original_tamper_cannot_establish_text(self):
        self.selected('received_literal_document'); original = deepcopy(self.snapshot)
        for mutation in ('selector', 'source_alias', 'source_url', 'source_snapshot_url', 'original'):
            self.snapshot = deepcopy(original)
            if mutation == 'selector':
                self.snapshot['content']['public_payload']['scrutiny_text_binding_refs'][0]['payload_digest'] = 'a' * 64
            elif mutation == 'source_alias':
                self.snapshot['content']['public_payload']['catalogue']['text_versions'][0]['source_version']['source_id'] = 'SYNTH:SAME-SHA-ALIAS'
            elif mutation == 'source_url':
                row = next(x for x in self.snapshot['sources']
                    if x['reference']['source_id'] == self.literal_doc.source.stable_id)
                row['reference']['public_url'] = 'https://example.org/SYNTH-wrong-source-url'
            elif mutation == 'source_snapshot_url':
                row = next(x for x in self.snapshot['content']['source_snapshot']
                    if x['source_id'] == self.literal_doc.source.stable_id)
                row['public_url'] = 'https://example.org/SYNTH-wrong-source-url'
            else:
                Path(self.dossier_doc.original_path).write_bytes(b'SYNTH corrupted original')
            with self.subTest(mutation=mutation):
                classification, report = self.report()
                self.assertEqual(self.text_check(report)['status'], 'hold')
                self.assertEqual(classification['dependency_graph']['submitted_text_binding']['status'], 'invalid')

    def test_literal_not_received_is_not_applicable_for_withdrawal_claim_and_never_pass(self):
        self.selected(); classification, report = self.report(d.ARCHETYPES[4],
            target_object_id='SYNTH:OTHER:WITHDRAWN', target_object_kind='amendment')
        self.assertEqual(self.text_check(report)['status'], 'not_applicable')
        self.assertIs(classification['dependency_graph']['submitted_text_binding']['literal_document_established'], False)

    def test_valid_procedural_selection_keeps_noncomparability_ready_and_literal_not_applicable(self):
        bindings = self.fixture.capacity()
        self.selected(); classification, report = self.report(d.ARCHETYPES[3], evidence_bindings=bindings)
        self.assertEqual(report['status'], 'ready', [x for x in report['checks'] if x['status'] == 'hold'])
        self.assertEqual(classification['dependency_graph']['submitted_text_binding']['status'], 'validated')
        self.assertEqual(next(x for x in report['checks'] if x['code'] == f.REASONS[3])['status'], 'pass')
        self.assertEqual(self.text_check(report)['status'], 'not_applicable')

    def test_nonliteral_source_alias_selector_or_original_tamper_blocks_selected_object(self):
        bindings = self.fixture.capacity()
        self.selected(); original = deepcopy(self.snapshot)
        for mutation in ('source_alias', 'selector', 'original'):
            self.snapshot = deepcopy(original)
            if mutation == 'source_alias':
                self.snapshot['content']['public_payload']['catalogue']['text_versions'][0]['source_version']['source_id'] = 'SYNTH:SAME-SHA-ALIAS'
            elif mutation == 'selector':
                self.snapshot['content']['public_payload']['scrutiny_text_binding_refs'][0]['payload_digest'] = 'a' * 64
            else:
                Path(self.dossier_doc.original_path).write_bytes(b'SYNTH corrupted original')
            with self.subTest(mutation=mutation):
                classification, report = self.report(d.ARCHETYPES[3], evidence_bindings=bindings)
                self.assertEqual(classification['dependency_graph']['submitted_text_binding']['status'], 'invalid')
                self.assertEqual(next(x for x in report['checks'] if x['code'] == f.REASONS[3])['status'], 'hold')
                self.assertEqual(self.text_check(report)['status'], 'not_applicable')
                self.assertEqual(report['status'], 'hold')
