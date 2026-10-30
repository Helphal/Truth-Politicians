"""Source/version/usage receipts rechecked against received primary conditions.

This is a closed technical qualification for a declared text renderer, not a
general licence of an institution, privacy clearance or publication approval.
No contact, provider, network request or document mutation occurs here.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from .canonical import sha256_digest
from .provenance import _safe_original

SOURCE_CONTRACT = 'dv.rights-source-qualification/1'
REPORT_CONTRACT = 'dv.rights-preflight/1'
RULESET = 'dv.source-usage-rules/20261004.1'
USAGES = ('official_link', 'short_quote', 'private_excerpt', 'public_excerpt',
          'long_reproduction', 'local_evidence_copy', 'capture')
MODES = {'private_review': ('private_excerpt', 'local_evidence_copy'),
         'public_excerpt': ('public_excerpt', 'official_link'),
         'public_long_reproduction': ('long_reproduction', 'official_link'),
         'official_link': ('official_link',)}
STATUSES = {'qualified', 'restricted', 'unknown', 'requires_authorization'}
_SHA = re.compile(r'[0-9a-f]{64}')
_BASENAME = re.compile(r'[0-9a-f]{64}\.(?:html|pdf)')
# These hashes pin the actual primary conditions received on 2026-10-04. A
# future changed text cannot inherit this rule by merely retaining its URL.
_APPROVED_CONDITIONS = {
    'an_text_conditions': ('https://www.assemblee-nationale.fr/dyn/info-site',
        '395c3d1a566a1712e7baa0fc3ff836800e287f9d30efc78a1eff947a9a20f378'),
    'senate_text_conditions': ('https://www.senat.fr/mentions-legales.html',
        '08b5b248ae61f2c6ee1dd4715b4e633ce7e410f5e59bc6a95b3b06888ee161e7'),
    'an_open_data_conditions': ('https://data.assemblee-nationale.fr/licence-ouverte-open-licence',
        '4fdf8b76454b2d5cc3b7fd6c1ce54f443084a8179b627280020c929273221f82'),
    'an_open_data_license': ('https://data.assemblee-nationale.fr/content/download/28755/file/Licence_Ouverte.pdf',
        '44ff23ae2b5cce0d729d1b53b20d17aae625189b131c2dd9cb98d1fe0d0a2a90'),
    'senate_open_data_conditions': ('https://data.senat.fr/licence/',
        '49c623949243c7e5c8ef4e64cf73b5bfceabce5ab51b00a985b1c98bd1aca0fa'),
}
_PRIVATE_REQUIREMENTS = ['attribution_visible', 'exact_text_and_context',
    'free_noncommercial_private_review', 'no_graphics_audio_video_or_logo',
    'no_script_execution_or_remote_asset_loading', 'senate_urls_plain_text_only']
_CONDITION_RULES = {
    'an_text_conditions': {'section': 'Droits de reproduction / liens hypertextes',
        'short_quote': 'du respect de l’intégrité des documents reproduits',
        'rule_summary': 'Textes parlementaires : gratuité, intégrité, auteur/source et référence originale ; médias et contexte de lien séparés.'},
    'senate_text_conditions': {'section': 'Liens / reproduction des textes parlementaires',
        'short_quote': "du respect de l'intégrité des documents (aucune modification ni altération)",
        'rule_summary': 'Textes parlementaires : gratuité, intégrité et attribution ; lien actif après information préalable du webmaster.'},
    'an_open_data_conditions': {'section': 'Licence Ouverte / Open Licence',
        'short_quote': 'la réutilisation la plus large',
        'rule_summary': 'Le portail renvoie à la Licence Ouverte ; seuls les jeux effectivement publiés sous cette licence sont couverts.'},
    'an_open_data_license': {'section': 'Licence Ouverte 1.0, octobre 2011 ; paternité et données personnelles',
        'short_quote': 'le nom du « Producteur »',
        'rule_summary': 'Réutilisation avec producteur et dernière mise à jour ; la licence ne supprime pas les exigences sur les données personnelles.'},
    'senate_open_data_conditions': {'section': 'Formats ouverts publiés sur data.senat.fr',
        'short_quote': 'formats ouverts',
        'rule_summary': 'La licence des jeux ouverts de data.senat.fr ne qualifie pas les pages HTML de senat.fr.'},
}


def _day(value):
    if value is None:
        return timezone.localdate()
    if isinstance(value, datetime):
        return value.date()
    if type(value) is date:
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError('rights_invalid_as_of') from None


def _evidence_root(source_root=None):
    configured = getattr(settings, 'DV_RIGHTS_EVIDENCE_DIR', None)
    if configured:
        raw = Path(configured)
    elif source_root is not None:
        raw = Path(source_root).parent.parent / 'droits' / 'conditions'
    else:
        runtime = getattr(settings, 'DV_RUNTIME_DIR', None) or getattr(settings, 'RUNTIME_DIR', None)
        if runtime is None:
            raise ValidationError('rights_conditions_runtime_required')
        raw = Path(runtime).parent / 'droits' / 'conditions'
    if raw.exists():
        info = raw.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
            raise ValidationError('rights_conditions_root_link_refused')
    return raw.resolve()


def _read_file(root, name, expected_sha=None):
    if name != 'RECU-COLLECTE-CONDITIONS.json' and not _BASENAME.fullmatch(name or ''):
        raise ValidationError('rights_condition_filename_refused')
    raw = root / name
    if not raw.is_file() or not raw.resolve().is_relative_to(root):
        raise ValidationError('rights_condition_file_missing')
    info = raw.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
        raise ValidationError('rights_condition_file_link_refused')
    if info.st_size > 10 * 1024 * 1024:
        raise ValidationError('rights_condition_file_too_large')
    data = raw.read_bytes()
    if expected_sha is not None and hashlib.sha256(data).hexdigest() != expected_sha:
        raise ValidationError('rights_conditions_sha_changed')
    return data


def _conditions(keys, source_root, day):
    root = _evidence_root(source_root)
    try:
        manifest = json.loads(_read_file(root, 'RECU-COLLECTE-CONDITIONS.json'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValidationError('rights_conditions_receipt_invalid') from None
    if (manifest.get('contract') != 'dv.primary-conditions-collection/1'
            or manifest.get('credentials_used') != 0 or manifest.get('cookies_saved') != 0):
        raise ValidationError('rights_primary_conditions_receipt_required')
    result = []
    for key in keys:
        matches = [r for r in manifest.get('rows', []) if isinstance(r, dict) and r.get('key') == key]
        if len(matches) != 1:
            raise ValidationError('rights_primary_conditions_binding_missing')
        row = matches[0]
        expected_url, expected_sha = _APPROVED_CONDITIONS[key]
        if (row.get('status') != 'received' or row.get('http_status') != 200
                or row.get('requested_url') != expected_url or row.get('url') != expected_url
                or row.get('sha256') != expected_sha
                or not str(row.get('filename', '')).startswith(expected_sha + '.')):
            raise ValidationError('rights_primary_conditions_not_approved')
        data = _read_file(root, row['filename'], expected_sha)
        if len(data) != row.get('byte_size'):
            raise ValidationError('rights_conditions_size_changed')
        try:
            captured = datetime.fromisoformat(row['captured_at'])
            if captured.tzinfo is None:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValidationError('rights_conditions_date_invalid') from None
        if captured.date() > day:
            raise ValidationError('rights_conditions_not_yet_received')
        result.append({**{field: row[field] for field in ('key', 'url', 'captured_at',
            'sha256', 'filename', 'byte_size', 'media_type')}, **_CONDITION_RULES[key]})
    return result


def _sources(snapshot):
    rows = snapshot.get('sources')
    if not isinstance(rows, list) or not rows:
        raise ValidationError('rights_exact_sources_required')
    result = []
    seen = set()
    for row in rows:
        ref = row.get('reference') if isinstance(row, dict) else None
        if (not isinstance(ref, dict) or any(type(ref.get(f)) is not str or not ref[f].strip()
                for f in ('source_id', 'version_id', 'sha256', 'public_url'))
                or not _SHA.fullmatch(ref['sha256']) or row.get('state') != 'available'):
            raise ValidationError('rights_exact_available_source_required')
        identity = tuple(ref[f] for f in ('source_id', 'version_id', 'sha256'))
        if identity in seen:
            raise ValidationError('rights_duplicate_source_binding')
        seen.add(identity)
        result.append((ref, row))
    return result


def _original(ref, source_root):
    from atelier.models import DocumentVersion
    rows = list(DocumentVersion.objects.filter(source__stable_id=ref['source_id'],
        version_id=ref['version_id'], sha256=ref['sha256']).values(
            'original_path', 'sha256', 'byte_size', 'media_type', 'quarantined',
            'source__document_url'))
    if len(rows) != 1 or rows[0]['source__document_url'] != ref['public_url']:
        raise ValidationError('rights_received_source_identity_changed')
    row = rows[0]
    root = source_root if source_root is not None else settings.DV_EVIDENCE_VAULT_PATH
    try:
        data = _safe_original(row, root)
    except (ValueError, OSError):
        raise ValidationError('rights_received_source_integrity_failed') from None
    return data, row['media_type']


def _resource(ref, media):
    media = str(media).split(';', 1)[0].strip().casefold()
    url = urlsplit(ref['public_url'])
    if url.scheme != 'https' or url.username or url.password or url.query or url.fragment:
        return 'unknown', (), 'unknown'
    host, path = url.hostname, url.path
    if host in {'www.assemblee-nationale.fr', 'questions.assemblee-nationale.fr'} and (
            re.fullmatch(r'/dyn/\d+/(?:scrutins/\d+|comptes-rendus/seance/[^/]+/[^/]+|dossiers/[a-z0-9_-]+)', path)
            or re.fullmatch(r'/dyn/docs/[A-Z0-9]+\.raw', path)) and media in {'text/html', 'text/plain'}:
        return 'parliamentary_text', ('an_text_conditions',), 'Assemblée nationale'
    if host == 'www.senat.fr' and (
            re.fullmatch(r'/scrutin-public/\d+/scr\d+-\d+\.html', path)
            or re.fullmatch(r'/seances/s\d+/s\d+/s\d+\.html', path)
            or re.fullmatch(r'/leg/(?:ppl|pjl|tas)\d+-\d+\.html', path)
            or re.fullmatch(r'/amendements/\d+-\d+/\d+/(?:Amdt_\d+|liste_adoptes_ordre_discussion)\.html', path)) and media == 'text/html':
        return 'parliamentary_text', ('senate_text_conditions',), 'Sénat'
    if host == 'data.assemblee-nationale.fr' and re.fullmatch(
            r'/static/openData/repository/\d+/amo/acteurs_mandats_organes_divises/AMO50_acteurs_mandats_organes_divises\.json\.zip', path) and media in {'application/zip', 'application/x-zip-compressed'}:
        return 'institutional_open_data_archive', ('an_open_data_conditions', 'an_open_data_license'), 'Assemblée nationale'
    return 'unknown', (), 'unknown'


def _usage(usage, resource, producer):
    limits = ['exact_source_version_only', 'source_author_and_original_url_attribution',
              'no_official_endorsement', 'no_general_privacy_or_publication_clearance']
    status, rule = 'unknown', 'no_closed_primary_rule'
    if resource == 'parliamentary_text':
        limits += ['text_only_no_images_logos_audio_video', 'free_noncommercial',
                   'verbatim_with_context', 'no_document_alteration']
        status, rule = 'restricted', 'parliamentary_text_reproduction_conditions'
        if usage == 'private_excerpt':
            status, rule = 'qualified', 'private_text_reader_exact_excerpt'
            limits += ['private_text_reader/1', 'no_remote_assets', 'no_script_execution']
        elif usage == 'local_evidence_copy':
            limits += ['private_evidence_retention_only', 'no_site_republication',
                       'markup_not_executed_no_remote_assets']
        elif usage == 'capture':
            status, rule = 'requires_authorization', 'images_and_layout_not_qualified_by_text_rule'
        elif usage == 'official_link':
            if producer == 'Sénat':
                status, rule = 'requires_authorization', 'senate_prior_webmaster_information_missing'
                limits += ['no_notification_sent', 'plain_text_url_attribution_remains_allowed']
            else:
                status, rule = 'restricted', 'an_link_context_restrictions_not_assessed'
        elif usage in {'short_quote', 'public_excerpt', 'long_reproduction'}:
            limits += ['public_context_and_integrity_review_required', 'public_render_not_approved']
    elif resource == 'institutional_open_data_archive':
        limits += ['licence_ouverte_1.0_october_2011', 'producer_and_last_update_attribution',
                   'personal_data_reuse_basis_not_assessed', 'archive_contains_more_than_selected_public_facts']
        rule = 'open_data_license_not_general_personal_data_clearance'
        # The actual AMO archive contains personal records; no legal basis is
        # manufactured from the open licence. Separate qualification is needed.
        if usage == 'capture':
            status, rule = 'requires_authorization', 'capture_not_open_data_information'
    return {'usage': usage, 'status': status, 'rule': rule, 'limits': limits}


def build_source_qualification(snapshot, reference, *, source_root=None, as_of=None):
    day = _day(as_of)
    matches = [(ref, row) for ref, row in _sources(snapshot) if ref == reference]
    if len(matches) != 1:
        raise ValidationError('rights_qualification_exact_snapshot_source_required')
    ref, row = matches[0]
    _, media = _original(ref, source_root)
    resource, condition_keys, producer = _resource(ref, media)
    conditions = _conditions(condition_keys, source_root, day) if condition_keys else []
    captured = min((datetime.fromisoformat(c['captured_at']).date() for c in conditions), default=day)
    expires = captured + timedelta(days=7) if conditions else None
    qualification = {'contract': SOURCE_CONTRACT, 'ruleset': RULESET,
        'source_id': ref['source_id'], 'version_id': ref['version_id'],
        'source_sha256': ref['sha256'], 'public_url': ref['public_url'],
        'resource_type': resource, 'media_type': media, 'producer': producer,
        'conditions': conditions, 'qualification_as_of': captured.isoformat(),
        'refresh_due_on': expires.isoformat() if expires else None,
        'refresh_policy': 'seven_day_technical_freshness_not_legal_expiry',
        'attribution': {'producer': producer, 'source_id': ref['source_id'],
            'version_id': ref['version_id'], 'original_url_text': ref['public_url'],
            'captured_at': row.get('captured_at'), 'document_date': row.get('document_date')},
        'usages': [_usage(usage, resource, producer) for usage in USAGES]}
    qualification['qualification_digest'] = sha256_digest(qualification)
    return qualification


def validate_source_qualification(snapshot, qualification, *, source_root=None, as_of=None):
    if not isinstance(qualification, dict):
        raise ValidationError('rights_source_qualification_required')
    refs = [ref for ref, _ in _sources(snapshot) if tuple(ref[f] for f in ('source_id', 'version_id', 'sha256')) == (
        qualification.get('source_id'), qualification.get('version_id'), qualification.get('source_sha256'))]
    if len(refs) != 1:
        raise ValidationError('rights_source_qualification_binding_changed')
    rebuilt = build_source_qualification(snapshot, refs[0], source_root=source_root, as_of=as_of)
    if rebuilt != qualification:
        raise ValidationError('rights_source_qualification_not_recomputed')
    expires = qualification['refresh_due_on']
    if expires is not None and date.fromisoformat(expires) < _day(as_of):
        raise ValidationError('rights_source_qualification_expired')
    return deepcopy(rebuilt)


def _aggregate(qualification, mode):
    usages = {item['usage']: item for item in qualification['usages']}
    statuses = [usages[usage]['status'] for usage in MODES[mode]]
    # Restricted original markup may only be retained by the declared private
    # text renderer; it is never licensed for public site reproduction here.
    allowed = mode == 'private_review' and qualification['resource_type'] == 'parliamentary_text' and statuses == ['qualified', 'restricted']
    return 'compatible' if allowed or all(x == 'qualified' for x in statuses) else 'unqualified'


def build_rights_report(snapshot, qualifications, *, render_mode='private_review', as_of=None):
    if render_mode not in MODES or not isinstance(qualifications, (list, tuple)):
        raise ValidationError('rights_render_mode_and_qualifications_required')
    qualified = [validate_source_qualification(snapshot, item, as_of=as_of) for item in qualifications]
    return _report(snapshot, qualified, render_mode)


def _report(snapshot, qualifications, mode):
    refs = [ref for ref, _ in _sources(snapshot)]
    lookup = {(q['source_id'], q['version_id'], q['source_sha256']): q for q in qualifications}
    if len(lookup) != len(qualifications) or set(lookup) != {
            tuple(ref[f] for f in ('source_id', 'version_id', 'sha256')) for ref in refs}:
        raise ValidationError('rights_all_exact_source_qualifications_required')
    entries = []
    ordered = []
    for ref in refs:
        q = lookup[tuple(ref[f] for f in ('source_id', 'version_id', 'sha256'))]
        ordered.append(deepcopy(q))
        entries.append({'source_id': q['source_id'], 'version_id': q['version_id'],
            'source_sha256': q['source_sha256'], 'usage': mode,
            'status': _aggregate(q, mode), 'qualification_reference': SOURCE_CONTRACT + ':' + q['qualification_digest'],
            'expires_on': q['refresh_due_on']})
    report = {'contract': REPORT_CONTRACT, 'fingerprint': sha256_digest(snapshot),
        'render_mode': mode, 'usages': entries, 'source_qualifications': ordered,
        'render_profile': 'private_text_reader/1' if mode == 'private_review' else 'public_render_unqualified/1',
        'render_requirements': deepcopy(_PRIVATE_REQUIREMENTS) if mode == 'private_review' else ['separate_public_usage_qualification_required'],
        'usage_mapping': list(MODES[mode]), 'ruleset': RULESET}
    report['rights_digest'] = sha256_digest(report)
    return report


def validate_rights_report(snapshot, report, *, source_root=None, as_of=None):
    if (not isinstance(report, dict) or report.get('contract') != REPORT_CONTRACT
            or report.get('render_mode') not in MODES or not isinstance(report.get('source_qualifications'), list)):
        raise ValidationError('rights_recomputed_source_qualifications_required')
    qualified = [validate_source_qualification(snapshot, item, source_root=source_root,
        as_of=as_of) for item in report['source_qualifications']]
    rebuilt = _report(snapshot, qualified, report['render_mode'])
    if rebuilt != report:
        raise ValidationError('rights_report_not_recomputed')
    return deepcopy(rebuilt)
