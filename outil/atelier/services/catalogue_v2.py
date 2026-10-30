"""Opt-in typed temporal projection, always recomputed from pinned originals."""
from copy import deepcopy
import re
from django.core.exceptions import ValidationError
from atelier.models import AffiliationObservation, Organization, Scrutiny
from .canonical import sha256_digest
from .provenance import build_provenance_report, project_affiliations_v2, OBSERVATION_STATUSES


def _claims(value, path=''):
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _claims(value[key], path + '/' + str(key).replace('~', '~0').replace('/', '~1'))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _claims(item, path + '/' + str(index))
    elif value is not None and value != '':
        yield {'id': sha256_digest([path, value])[:24], 'location': path, 'value': value}


def _key(row):
    ref = row['source_version']
    return (row['person_id'], row['organization_id'], row.get('observed_on'), ref['source_id'], ref['version_id'])


def project_catalogue_v2(catalogue, refs, *, collective_group=None, provenance_ruleset='dv.provenance-rules/1', binding_refs=()):
    from .catalogue import source_ref
    raw = deepcopy(catalogue)
    submitted = dict(Scrutiny.objects.filter(stable_id__in=raw['scope_scrutiny_ids']).values_list(
        'stable_id', 'submitted_text__stable_id'))
    selected = select_scrutiny_text_bindings(raw['scope_scrutiny_ids'], binding_refs) if binding_refs else {}
    for identifier, binding in selected.items():
        submitted[identifier] = binding.submitted_text.stable_id
        ref = binding.payload['dossier_source_version']
        if not any(existing == ref for existing in refs):
            refs.append(deepcopy(ref))
    for scrutiny in raw['scrutinies']:
        text_id = submitted.get(scrutiny['scrutin_id'])
        if not text_id or not any(text['id'] == text_id and text['relation'] == 'submitted' for text in raw['text_versions']):
            raise ValidationError('catalogue_v2_submitted_text_binding_missing')
        scrutiny['submitted_text_id'] = text_id
    for person in raw['persons']:
        match = re.fullmatch(r'SENAT:PERSON:(\d{5}[A-Z])', person['id'])
        identifier = person.get('institutional_ids', {}).get('senat')
        # Lossless identifier casing only; the source extractor subsequently
        # verifies the namespace and exact institutional person independently.
        if match and isinstance(identifier, str) and identifier.upper() == match.group(1):
            person['institutional_ids']['senat'] = match.group(1)
    versions = {(ref['source_id'], ref['version_id']): deepcopy(ref) for ref in refs}
    people = {person['id'] for person in raw['persons']}
    event_dates = set(Scrutiny.objects.filter(stable_id__in=raw['scope_scrutiny_ids']).values_list('occurred_on', flat=True))
    query = AffiliationObservation.objects.filter(person__stable_id__in=people, observed_on__in=event_dates)
    if collective_group is not None:
        query = query.filter(organization=collective_group)
    observations = query.select_related('person', 'organization', 'source_document__source').order_by(
        'person__stable_id', 'organization__stable_id', 'observed_on', 'source_document_id', 'record_id')
    existing = {_key(row) for row in raw['affiliations'] if row.get('status') in OBSERVATION_STATUSES}
    known_at = {}
    for record in observations:
        record.clean()
        if record.known_at != record.source_document.captured_at:
            raise ValidationError('observation_receipt_time_mismatch')
        ref = source_ref(record.source_document)
        versions[(ref['source_id'], ref['version_id'])] = ref
        row = {'person_id': record.person.stable_id, 'organization_id': record.organization.stable_id,
            'status': 'observed_in_official_vote_list', 'starts_on': None, 'ends_on': None,
            'observed_on': record.observed_on.isoformat(), 'source_version': ref,
            'scrutiny_id': record.payload.get('scrutiny_id'),
            'ambiguity': 'Observed at this event; actual membership boundaries unknown.'}
        key = _key(row)
        if key not in existing:
            raw['affiliations'].append(row)
            existing.add(key)
        known_at[key] = record.known_at.isoformat()
    organization_ids = {row['organization_id'] for row in raw['affiliations']} - {row['id'] for row in raw['organizations']}
    for organization in Organization.objects.filter(stable_id__in=organization_ids).order_by('stable_id'):
        raw['organizations'].append({'id': organization.stable_id, 'name': organization.name,
            'kind': organization.kind, 'valid_from': organization.valid_from.isoformat() if organization.valid_from else None,
            'valid_to': organization.valid_to.isoformat() if organization.valid_to else None})
    raw['organizations'].sort(key=lambda row: row['id'])
    raw['affiliations'].sort(key=lambda row: (row['person_id'], row.get('starts_on') or '',
        row['organization_id'], row.get('observed_on') or '', row['source_version']['source_id'], row['source_version']['version_id']))
    refs = sorted(versions.values(), key=lambda ref: (ref['source_id'], ref['version_id']))
    content = {'public_payload': {'catalogue': raw}, 'source_snapshot': refs}
    snapshot = {'contract': 'dv.catalogue-projection-input/1', 'content': content,
        'sources': [{'reference': ref} for ref in refs], 'claims': list(_claims(content))}
    report = build_provenance_report(snapshot)
    projected = project_affiliations_v2(content['public_payload'], snapshot=snapshot, provenance_report=report)['catalogue']
    for row in projected['affiliations']:
        key = _key(row)
        if row['observed_on_state'] == 'known' and key in known_at:
            row['known_at'] = {'state': 'known', 'value': known_at[key], 'basis': 'received_at_not_membership_date'}
    projected['projection_ruleset'] = provenance_ruleset
    projected['exclusions'].append('Missing end dates do not attest ongoing membership. Unknown and absence remain distinct.')
    return projected, refs


def select_scrutiny_text_bindings(scrutiny_ids, binding_refs):
    from atelier.models import ScrutinyTextBinding
    if not isinstance(binding_refs, (list, tuple)) or len(binding_refs) > 25:
        raise ValidationError('scrutiny_text_binding_explicit_selection_required')
    result = {}
    for ref in binding_refs:
        if (not isinstance(ref, dict) or set(ref) != {'scrutiny_id', 'version', 'payload_digest'}
                or type(ref['version']) is not int or ref['version'] < 1
                or not isinstance(ref['payload_digest'], str) or not re.fullmatch(r'[0-9a-f]{64}', ref['payload_digest'])
                or ref['scrutiny_id'] not in scrutiny_ids or ref['scrutiny_id'] in result):
            raise ValidationError('scrutiny_text_binding_invalid_selection')
        matches = list(ScrutinyTextBinding.objects.filter(scrutiny__stable_id=ref['scrutiny_id'],
            version=ref['version'], payload_digest=ref['payload_digest']).select_related(
                'scrutiny', 'submitted_text__dossier', 'submitted_text__source_document__source', 'source_document__source'))
        if len(matches) != 1:
            raise ValidationError('scrutiny_text_binding_selected_version_missing')
        matches[0].validate_binding(**ref)
        result[ref['scrutiny_id']] = matches[0]
    return result


def documentary_observations_v07(public, comparison):
    """Recompute source-declared capacity/selected objects for new profile only.

    Object IDs and pinned references are selectors, never a caller's proof.
    Historical Declaration rows and older public profiles are not rewritten.
    """
    from django.conf import settings
    from .catalogue import source_ref
    from .provenance import _safe_original, _html_records
    from atelier.models import DocumentVersion
    result = deepcopy(public)

    def records(document):
        descriptor = {'sha256': document.sha256, 'byte_size': document.byte_size,
            'original_path': document.original_path, 'quarantined': document.quarantined}
        try:
            data = _safe_original(descriptor, settings.DV_EVIDENCE_VAULT_PATH)
            return _html_records(data, source_url=document.source.document_url)
        except (OSError, ValueError, UnicodeError):
            raise ValidationError('documentary_projection_original_unavailable') from None

    if comparison is not None:
        document = comparison.declaration.source_passage.document
        speaker = result.get('actors', {}).get('speaker', {})
        quote = result.get('declaration_quote')
        matching = [r for r in records(document) if r['kind'] == 'declared_actor_capacity'
                    and r['speaker_name'] == speaker.get('label') and isinstance(quote, str)
                    and quote and quote in r['paragraph_text']]
        values = {r['capacity'] for r in matching}
        if len(values) == 1:
            capacity = next(iter(values))
            result['actors']['speaker']['capacity'] = capacity
            result['attribution']['capacity'] = capacity
            # Duplicate documentary actor projections must use the same
            # observed label; this remains a declared, unresolved identity.
            result['qualification_material']['actors']['speaker']['capacity'] = capacity
    observations = result.get('object_observations', [])
    if not isinstance(observations, list):
        raise ValidationError('documentary_object_observations_required')
    derived, additional_refs = [], []
    for selected in observations:
        if not isinstance(selected, dict) or not isinstance(selected.get('source_version'), dict):
            raise ValidationError('documentary_object_source_selector_required')
        ref = selected['source_version']
        documents = list(DocumentVersion.objects.filter(source__stable_id=ref.get('source_id'),
            version_id=ref.get('version_id'), sha256=ref.get('sha256')).select_related('source'))
        if len(documents) != 1 or source_ref(documents[0]) != ref:
            raise ValidationError('documentary_object_exact_source_required')
        document = documents[0]
        matching = [r for r in records(document) if r['kind'] == 'amendment'
                    and r['id'] == selected.get('object_id')]
        states = {r['status'] for r in matching}
        if not matching or len(states) != 1 or next(iter(states)) != 'withdrawn':
            raise ValidationError('documentary_object_withdrawal_not_established')
        row = matching[0]
        locator = row['locator']['status_locator']
        if not locator or locator['end'] <= locator['start']:
            raise ValidationError('documentary_object_exact_locator_required')
        marker = 'html-codepoints/1:' + str(locator['start']) + ':' + str(locator['end']) + '; object=' + row['id'] + '; status'
        derived.append({'object_id': row['id'], 'object_kind': 'amendment', 'status': 'withdrawn',
            'official_title': row['official_title'], 'source_version': ref,
            'locator': marker, 'exact_quote': row['status_text']})
        additional_refs.append(ref)
    result['object_observations'] = derived
    if 'personal_vote_selection' in result:
        result, personal_ref = personal_vote_selection_v07(result)
        additional_refs.append(personal_ref)
    return result, additional_refs


def personal_vote_selection_v07(public):
    """Select a personally observed vote while retaining the declaration scope.

    The closed selector carries institutional IDs and a frozen original, never
    a name or a proof flag. ORM and original event records must agree.
    """
    from django.conf import settings
    from atelier.models import DocumentVersion, VoteObservation
    from .catalogue import source_ref
    from .provenance import _safe_original, _html_records
    if public.get('public_contract_version') != 'dv.content/0.7.0-conclusion-draft.1':
        raise ValidationError('personal_vote_selection_requires_conclusion_profile')
    selection = public.get('personal_vote_selection')
    if (not isinstance(selection, dict) or set(selection) != {'person_id', 'scrutiny_id', 'source_version'}
            or not isinstance(selection['person_id'], str) or not selection['person_id']
            or not isinstance(selection['scrutiny_id'], str) or not selection['scrutiny_id']
            or not isinstance(selection['source_version'], dict)):
        raise ValidationError('personal_vote_selection_closed_selector_required')
    ref = selection['source_version']
    documents = list(DocumentVersion.objects.filter(source__stable_id=ref.get('source_id'),
        version_id=ref.get('version_id'), sha256=ref.get('sha256')).select_related('source'))
    if len(documents) != 1 or source_ref(documents[0]) != ref:
        raise ValidationError('personal_vote_selection_exact_original_required')
    document = documents[0]
    votes = list(VoteObservation.objects.filter(person__stable_id=selection['person_id'],
        scrutiny__stable_id=selection['scrutiny_id'], source_document=document).select_related('person', 'scrutiny'))
    if len(votes) != 1:
        raise ValidationError('personal_vote_selection_observation_missing_or_ambiguous')
    vote = votes[0]
    if (vote.normalized_category not in {'for', 'against', 'abstention', 'non_voting'}
            or selection['scrutiny_id'] not in public.get('catalogue', {}).get('scope_scrutiny_ids', [])):
        raise ValidationError('personal_vote_selection_event_or_position_invalid')
    descriptor = {'sha256': document.sha256, 'byte_size': document.byte_size,
        'original_path': document.original_path, 'quarantined': document.quarantined}
    try:
        data = _safe_original(descriptor, settings.DV_EVIDENCE_VAULT_PATH)
        records = _html_records(data, source_url=document.source.document_url)
    except (OSError, ValueError, UnicodeError):
        raise ValidationError('personal_vote_selection_original_integrity_failed') from None
    matching = [r for r in records if r['kind'] == 'person' and r['id'] == selection['person_id']
        and r.get('scrutiny_id') == selection['scrutiny_id'] and r.get('category')]
    if (len(matching) != 1 or matching[0].get('group_conflict')
            or matching[0].get('category') != vote.normalized_category
            or matching[0].get('observed_on') != vote.scrutiny.occurred_on.isoformat()):
        raise ValidationError('personal_vote_selection_original_event_position_mismatch')
    projected = [r for r in public.get('catalogue', {}).get('vote_observations', [])
        if r.get('person_id') == selection['person_id'] and r.get('scrutiny_id') == selection['scrutiny_id']
        and r.get('source_version') == ref and r.get('normalized_category') == vote.normalized_category]
    if len(projected) != 1:
        raise ValidationError('personal_vote_selection_catalogue_observation_mismatch')
    result = deepcopy(public)
    result['actors']['compared_actor'] = {'person_id': vote.person.stable_id, 'organization_id': None,
        'label': vote.person.display_name}
    result['actors']['scope'] = 'personal'
    result['compared_actor_scope'] = 'personal'
    result['qualification_material']['actors'] = deepcopy(result['actors'])
    return result, source_ref(document)
