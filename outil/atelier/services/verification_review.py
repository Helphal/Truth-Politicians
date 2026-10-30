"""Read-only presentation of verification. No receipt, approval or repair here."""
from collections import OrderedDict
import json
import hashlib
import re
from pathlib import Path, PureWindowsPath, PurePosixPath
from urllib.parse import urlsplit
from django.conf import settings
from .verification import structural_metadata

REVIEW_SCHEMA = 'dv.verification-review/1'
ARTIFACT_SCHEMA = 'dv.verification-review-artifact/1'
MAX_REVIEW_ARTIFACT_BYTES = 8 * 1024 * 1024


def _bounded_read(path, root):
    root, path = Path(root), Path(path)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise ValueError('review_artifact_path_escape')
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('review_artifact_link_forbidden')
        if part == root:
            break
    if not path.is_file() or path.stat().st_size > MAX_REVIEW_ARTIFACT_BYTES:
        raise ValueError('review_artifact_size_or_type')
    with path.open('rb') as stream:
        data = stream.read(MAX_REVIEW_ARTIFACT_BYTES + 1)
    if len(data) > MAX_REVIEW_ARTIFACT_BYTES:
        raise ValueError('review_artifact_size_or_type')
    return data


def historical_review(job):
    """Private local evidence only, explicitly configured and bound to one snapshot."""
    root = getattr(settings, 'DV_VERIFICATION_REVIEW_ARTIFACT_ROOT', None)
    if not root:
        return {'available': False, 'reason': 'historical_artifacts_not_configured'}
    try:
        folder = Path(root) / f'job-{job.pk}'
        data = json.loads(_bounded_read(folder / 'review.json', root))
        if (data.get('contract') != ARTIFACT_SCHEMA or type(data.get('job_id')) is not int
                or type(data.get('revision_pk')) is not int or type(data.get('attempt_id')) is not int
                or data['attempt_id'] < 1 or data.get('job_id') != job.pk
                or data.get('revision_pk') != job.revision.pk or data.get('fingerprint') != job.fingerprint):
            raise ValueError('historical_binding_mismatch')
        if not job.attempts.filter(pk=data['attempt_id']).exists():
            raise ValueError('historical_attempt_binding_mismatch')
        name = data.get('raw_file', '')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.json', name):
            raise ValueError('historical_raw_path_invalid')
        raw = _bounded_read(folder / name, root)
        if hashlib.sha256(raw).hexdigest() != data.get('raw_sha256'):
            raise ValueError('historical_raw_digest_mismatch')
        for key in ('expected_count', 'present_count', 'missing_count'):
            if type(data.get(key)) is not int or data[key] < 0:
                raise ValueError('historical_coverage_invalid')
        if data['present_count'] + data['missing_count'] != data['expected_count']:
            raise ValueError('historical_coverage_invalid')
        semantic = [c for c in job.input_snapshot['claims'] if not structural_metadata(c, job.input_snapshot)]
        if data['expected_count'] != len(semantic):
            raise ValueError('historical_expected_count_mismatch')
        if not isinstance(data.get('objections'), list):
            raise ValueError('historical_objections_invalid')
        parsed = json.loads(raw)
        if parsed.get('fingerprint') != job.fingerprint or not isinstance(parsed.get('groups'), list):
            raise ValueError('historical_raw_binding_mismatch')
        all_indices = [index for group in parsed['groups'] for index in group.get('claim_indices', [])]
        valid_indices = [index for index in all_indices if type(index) is int and 0 <= index < data['expected_count']]
        seen = set(valid_indices)
        if len(seen) != data['present_count']:
            raise ValueError('historical_coverage_mismatch')
        negative_indices = {i for i, group in enumerate(parsed['groups']) if group.get('verdict') != 'ETAYE_DANS_PERIMETRE'}
        supplied = [o.get('raw_group_index') for o in data['objections']]
        if (any(type(i) is not int for i in supplied) or len(set(supplied)) != len(supplied)
                or set(supplied) != negative_indices):
            raise ValueError('historical_objection_coverage_mismatch')
        for objection in data['objections']:
            if objection.get('model_group') != parsed['groups'][objection['raw_group_index']]:
                raise ValueError('historical_group_changed')
            indices = objection['model_group']['claim_indices']
            assertions = objection.get('claims')
            if not isinstance(assertions, list) or len(assertions) != len(indices):
                raise ValueError('historical_assertion_coverage_changed')
            for index, claim in zip(indices, assertions):
                if type(index) is not int or not 0 <= index < len(semantic) or claim.get('claim_index') != index:
                    raise ValueError('historical_assertion_index_changed')
                original = semantic[index]
                if any(type(claim.get(k)) is not type(original[k]) or claim.get(k) != original[k] for k in ('id', 'location', 'value')):
                    raise ValueError('historical_assertion_binding_changed')
                context = _context(job.input_snapshot, original['location'])
                expected_context = json.loads(context)
                presented_context = claim.get('context')
                if isinstance(presented_context, str):
                    try:
                        presented_context = json.loads(presented_context)
                    except ValueError:
                        pass
                if _text(presented_context) != _text(expected_context):
                    raise ValueError('historical_assertion_context_changed')
                claim['context'] = context
                claim['context_location'] = original['location'].rsplit('/', 1)[0] or '/'
        # Returned data remains untrusted text rendered through Django autoescape.
        return {**data, 'available': True, 'raw_text': raw.decode('utf-8'),
                'duplicates': len(valid_indices) - len(seen), 'invalid_indices': len(all_indices) - len(valid_indices),
                'provenance': 'Réponse fournisseur historique refusée ; aucun reçu nouveau, aucune qualification favorable.'}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        return {'available': False, 'reason': 'historical_artifact_rejected_or_missing'}


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _context(snapshot, location):
    node = snapshot.get('content', {})
    parts = location.strip('/').split('/')[:-1]
    try:
        for part in parts:
            part = part.replace('~1', '/').replace('~0', '~')
            node = node[int(part)] if isinstance(node, list) else node[part]
        return _text(node)
    except (KeyError, IndexError, ValueError, TypeError):
        return 'Contexte non résolu ; consulter l’instantané exact.'


def build_review(job, attempts, sources=None, eligibility=(False, 'not_evaluated')):
    """Keep execution, documentary finding and diffusion independent and fail closed.

    Counts describe persisted canonical assertions, not an unparsed provider output.
    Rejected raw output is never interpreted as a successful canonical result.
    """
    snapshot = job.input_snapshot
    expected = {c['id']: c for c in snapshot.get('claims', [])}
    source_rows = sources if sources is not None else snapshot.get('sources', [])
    passages = {str(p['id']): {**p, 'reference': s.get('reference', {})}
                for s in source_rows for p in s.get('passages', [])}
    reviews = []
    for attempt in sorted(attempts, key=lambda a: a.number, reverse=True):
        result = attempt.result if isinstance(attempt.result, dict) else {}
        rows = result.get('claims', [])
        rows = rows if isinstance(rows, list) else []
        groups, seen, invalid, duplicates = OrderedDict(), set(), 0, 0
        negative = 0
        for n, row in enumerate(rows):
            if not isinstance(row, dict):
                invalid += 1
                continue
            identity = row.get('id')
            original = expected.get(identity) if isinstance(identity, str) else None
            if original is None or any(row.get(k) != original[k] for k in ('location', 'value')):
                invalid += 1
                continue
            duplicates += row['id'] in seen
            seen.add(row['id'])
            verdict = row.get('verdict', 'INDETERMINE')
            negative += verdict != 'ETAYE_DANS_PERIMETRE' and not structural_metadata(original, snapshot)
            parent = original['location'].rsplit('/', 1)[0] or '/'
            key = (parent, verdict)
            group = groups.setdefault(key, {'context_path': parent, 'verdict': verdict,
                                            'context': _context(snapshot, original['location']), 'claims': []})
            item = {**row, 'anchor': f'assertion-{attempt.pk}-{n}', 'proofs': []}
            for kind in ('support', 'against'):
                proofs = row.get(kind, [])
                for proof in proofs if isinstance(proofs, list) else []:
                    if not isinstance(proof, dict):
                        continue
                    passage = passages.get(str(proof.get('passage_id')), {})
                    item['proofs'].append({'kind': kind, **proof, 'passage': passage,
                                          'anchor': 'passage-' + str(proof.get('passage_id'))})
            group['claims'].append(item)
        missing = [c for identity, c in expected.items() if identity not in seen]
        complete = not missing and not invalid and not duplicates and bool(expected)
        if attempt.error_code:
            execution = 'Refus de format ou exécution interrompue'
        elif not complete:
            execution = 'Couverture incomplète'
        else:
            execution = 'Couverture canonique complète'
        reviews.append({'attempt': attempt, 'groups': list(groups.values()), 'missing': missing,
                        'expected': len(expected), 'covered': len(seen), 'invalid': invalid,
                        'duplicates': duplicates, 'negative': negative, 'execution': execution,
                        'raw_json': json.dumps(result, ensure_ascii=False, indent=2),
                        'raw_note': 'Résultat canonique persisté ; un brut fournisseur refusé reste dans les preuves de campagne.'})
    latest = reviews[0] if reviews else None
    stale = job.state == 'stale' or job.revision.revision != job.revision.dossier.current_revision
    rights_unknown = any(s.get('rights') not in {'open', 'excerpt_allowed'} for s in source_rows) or not source_rows
    axes = {
        'execution': 'Vérification périmée' if stale else (latest['execution'] if latest else 'Non exécutée'),
        'documentary': ('Résultat documentaire non favorable' if latest and latest['negative'] else
                        'Étaye dans le périmètre vérifié' if eligibility[0] else 'Non qualifié'),
        'diffusion': 'Droits non qualifiés' if rights_unknown else 'Revue personnelle et autorisation de diffusion à contrôler',
        'gate_reason': eligibility[1],
    }
    return {'schema': REVIEW_SCHEMA, 'axes': axes, 'attempts': reviews, 'rights_unknown': rights_unknown,
            'historical': historical_review(job),
            'stale': stale, 'human_review': 'La revue personnelle du responsable éditorial reste un contrôle distinct, sur la révision exacte.'}


def summary_review(job, attempts, eligibility=(False, 'not_evaluated')):
    return build_review(job, attempts, eligibility=eligibility)['axes']


def guide_markdown(data, *, preview_links, plain_senate_urls=False):
    """Export explicit version fields; never stringify a source-reference object."""
    def md(value):
        return _text(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('[', '\\[').replace(']', '\\]')
    current = any('review_axes' in row for row in data['revisions'])
    introduction = ('Réponses proposées, réserves et états reçus. Aucun nouveau verdict, avis personnel ou droit de diffusion.'
                    if current else 'Réponses proposées et réserves historiques. Aucun nouveau verdict, avis personnel ou droit de diffusion.')
    lines = ['# Revue privée des six dossiers', '', f'Contrat de présentation : `{REVIEW_SCHEMA}`.',
             '', introduction, '']
    for row in data['revisions']:
        axes = row.get('review_axes')
        if 'review_axes' in row and (not isinstance(axes, dict)
                or set(axes) != {'execution', 'documentary', 'diffusion', 'gate_reason'}
                or any(not isinstance(value, str) or not value.strip() for value in axes.values())):
            raise ValueError('current_review_axes_incomplete')
        preview_label = 'Aperçu privé actuel' if axes is not None else 'Aperçu privé historique'
        ref = row['source_version']
        if not isinstance(ref, dict) or not all(isinstance(ref.get(k), str) and ref[k] for k in ('source_id', 'version_id', 'sha256', 'public_url')):
            raise ValueError('source_reference_incomplete')
        url = ref['public_url']
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in {'www.assemblee-nationale.fr', 'www.senat.fr'} or parsed.username or parsed.password or any(c in url for c in '\n\r<>'):
            raise ValueError('source_link_not_admissible')
        preview = preview_links[row['job_id']]
        if (not isinstance(preview, str) or any(c in preview for c in '\r\n<>?#\x00')
                or '..' in PureWindowsPath(preview).parts
                or not (PureWindowsPath(preview).is_absolute() or PurePosixPath(preview).is_absolute())
                or preview.startswith(('//', '\\\\'))
                or (':' in preview and not re.match(r'^[A-Za-z]:[/\\]', preview))):
            raise ValueError('private_preview_link_not_admissible')
        if '`' in url:
            raise ValueError('source_link_not_admissible')
        source_label = ('Référence institutionnelle (texte) : `' + md(url) + '`'
                        if plain_senate_urls and parsed.hostname == 'www.senat.fr'
                        else f'[Source institutionnelle](<{url}>)')
        lines += [f"## {md(row['title'])}", '', f"Révision {row['revision']} (PK {row['revision_pk']}) · job {row['job_id']}.",
                  '', '**Réponse proposée :** ' + md(row['short_answer']), '', '**Réserve :** ' + md(row['main_caveat']),
                  '', md(row['attribution']), '', 'Date : ' + md(row['date']),
                  '', 'Source : ' + md(ref['source_id']), '', 'Version de source : ' + md(ref['version_id']),
                  '', 'Empreinte : `' + md(ref['sha256']) + '`', '', 'Repère : ' + md(row['normalized_locator']),
                  '', source_label + f' · [{preview_label}](<{preview}>)', '']
        if axes is None:
            lines += ['Exécution/complétude : voir le refus historique et le plan courant. Résultat documentaire : non qualifié. Diffusion : fermée ; droits et revue personnelle distincts.', '']
        else:
            lines += ['**Exécution/complétude :** ' + md(axes['execution']),
                      '', '**Résultat documentaire :** ' + md(axes['documentary']),
                      '', '**Diffusion :** ' + md(axes['diffusion']),
                      '', '**Motif du contrôle :** ' + md(axes['gate_reason']),
                      '', 'Ces états sont ceux reçus par l’application ; la revue personnelle et l’autorisation de diffusion restent des contrôles distincts.', '']
    return '\n'.join(lines)
