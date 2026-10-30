"""Read-only, versioned presentation of persisted proof architecture.

This module does not classify facts, qualify rights, create reviews or infer a
favourable verdict. Missing or mismatched assessments stay visibly unreceived.
"""
import json
import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator

from .publication import _validate_public_url


SCHEMA = 'dv.verification-architecture-review/2'
ASSESSMENT_SCHEMA = 'dv.proof-architecture/1'
DEPENDENCY_ASSESSMENT_SCHEMA = 'dv.proof-architecture/2'
LAYER_LABELS = {
    'provenance': 'A — Provenance déterministe',
    'semantic': 'B — Vérification du sens',
    'editorial': 'C — Décisions éditoriales',
    'unknowns': 'D — Inconnus et ambiguïtés',
}
FILTERS = {
    'all': 'Tous les éléments', 'provenance': 'Provenance',
    'semantic': 'Sens et citations', 'objection': 'Objections',
    'counterevidence': 'Contre-preuves', 'editorial': 'Sélection éditoriale',
    'unknowns': 'Inconnus et bornes', 'rights': 'Droits',
    'dependencies': 'Preuves nécessaires à cette conclusion',
}
GATE_LABELS = {
    'deterministic_provenance_complete': 'Provenance déterministe complète',
    'semantic_verification_complete': 'Vérification sémantique complète',
    'counterevidence_complete': 'Recherche de contre-preuves complète',
    'eligible_for_human_review': 'Recevable pour la revue personnelle',
    'human_reviewed_exact_revision': 'Revue personnelle de la révision exacte',
    'rights_qualified': 'Droits qualifiés pour les usages prévus',
    'publishable': 'Publication recevable',
}
LABELS = {
    'status': 'État', 'reason': 'Motif', 'summary': 'Résumé',
    'short_answer': 'Réponse proposée', 'main_caveat': 'Réserve principale',
    'essential_proofs': 'Preuves essentielles', 'counter_elements': 'Principaux contre-éléments',
    'uncertainty': 'Incertitudes', 'counts': 'Nombre d’éléments par famille',
    'necessary_for_conclusion': 'Nécessaire à la conclusion',
    'archetype': 'Type exact de conclusion',
    'dependency_rule_id': 'Règle qui exige cette preuve',
    'blocking_claim_ids': 'Assertions qui bloquent cette conclusion',
    'why_required_for_this_conclusion': 'Pourquoi cette preuve est nécessaire ici',
    'what_evidence_would_resolve_it': 'Pièce précise à obtenir',
    'can_resolve_without_model': 'Résolution possible sans modèle',
    'can_resolve_without_new_source': 'Résolution possible avec les sources reçues',
    'temporal_scope': 'Périmètre temporel de la conclusion',
    'dependency_graph': 'Dépendances calculées par le serveur',
    'qualification_reference': 'Pièce de qualification', 'expires_on': 'Validité jusqu’au',
    'qualified_at': 'Qualification datée', 'terms_url': 'Conditions applicables',
    'rights_status': 'État des droits reçu', 'reference': 'Référence exacte',
    'render_mode': 'Usage et rendu prévus', 'rights_report': 'Qualification par usage',
    'claim_id': 'Assertion', 'assertion_id': 'Assertion', 'location': 'Champ concerné',
    'field': 'Champ concerné', 'fact_type': 'Type de fait', 'source_id': 'Source',
    'version_id': 'Version de source', 'sha256': 'Empreinte source',
    'source_sha256': 'Empreinte source', 'raw_value': 'Valeur reçue',
    'locator': 'Repère exact', 'record_locator': 'Enregistrement d’origine',
    'record_id': 'Identifiant d’origine', 'extraction_rule': 'Règle d’extraction',
    'extraction_version': 'Version de la règle', 'transformation': 'Transformation',
    'result_digest': 'Empreinte du résultat', 'selection_rule_id': 'Règle de sélection',
    'included': 'Objets inclus', 'excluded': 'Objets exclus', 'limits': 'Limites',
    'counter_cases': 'Contre-cas examinés', 'observed_on': 'Observé à cette date',
    'known_at': 'Connu à cette date', 'starts_on': 'Ancienne borne de début',
    'ends_on': 'Ancienne borne de fin', 'bounds': 'Bornes établies ou inconnues',
    'code': 'Contrôle', 'locations': 'Champs concernés', 'checks': 'Contrôles locaux',
    'verdict': 'Verdict reçu', 'against': 'Contre-éléments', 'support': 'Soutiens reçus',
    'quote': 'Citation exacte', 'exact_quote': 'Citation exacte', 'explanation': 'Explication',
    'absence_is_not_proven': 'L’absence de contre-élément n’est pas établie',
    'no_result_is_not_absence_proof': 'Sans résultat ne signifie pas sans contre-élément',
    'deterministic_assertions': 'Assertions déterministes', 'semantic_assertions': 'Assertions sémantiques',
    'editorial_assertions': 'Décisions éditoriales', 'unknown_assertions': 'Inconnus',
    'source_readings': 'Lectures de sources prévues', 'admissible_reuses': 'Réutilisations admissibles',
    'minimum_model_phases': 'Phases modèle minimales', 'proposed_ceiling': 'Plafond proposé',
    'actual_phases_reserved': 'Phases réellement réservées', 'actual_phases_launched': 'Phases réellement lancées',
}
STATE_LABELS = {
    'complete': 'Complet', 'incomplete': 'Incomplet', 'pending': 'En attente',
    'blocked': 'Bloqué', 'ready': 'Recevable', 'hold': 'À compléter',
    'rejected': 'Refusé', 'proven': 'Établi par provenance', 'conflict': 'Conflit',
    'missing': 'Preuve manquante', 'ambiguous': 'Ambigu', 'unknown': 'Inconnu',
    'observed': 'État observé à une date', 'editorial_selection': 'Choix éditorial explicite',
    'supported': 'Soutenu dans le périmètre', 'not_supported': 'Non suffisamment soutenu',
    'mixed': 'Résultat mixte', 'insufficient_evidence': 'Preuves insuffisantes',
    'contradicted': 'Contredit', 'not_comparable': 'Non comparable',
    'not_applicable': 'Non requis pour cette conclusion',
    'at_scrutiny': 'À ce scrutin précis', 'period': 'Sur une période établie',
    'ready_for_semantic_verification': 'Prêt pour la vérification du sens',
    'personal_declaration_personal_vote': 'Déclaration personnelle et vote personnel',
    'collective_declaration_group_distribution': 'Déclaration collective et répartition du groupe',
    'collective_declaration_personal_vote': 'Déclaration collective et vote personnel',
    'not_comparable_different_actor_or_capacity': 'Non-comparabilité documentée des acteurs ou fonctions',
    'withdrawn_or_not_voted_object': 'Objet retiré ou non soumis au vote',
    'observed_vote_majority': 'Majorité observée au scrutin',
}
PRIVATE_KEYS = {'password', 'token', 'cookie', 'credentials', 'secret', 'private_key',
                'auth', 'prompt', 'system_prompt', 'local_path', 'vault_path'}
LOCAL_PATH = re.compile(r'(?i)(?:(?<![a-z0-9])[a-z]:[\\/]|\\\\|file://|(?:^|\s)/(?:home|users|tmp|etc|var)/)')


def _text(value):
    if value is None:
        return 'Inconnu / non établi'
    if type(value) is bool:
        return 'Oui' if value else 'Non'
    if isinstance(value, str):
        return value or 'Non renseigné'
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _url(value):
    if not isinstance(value, str):
        return ''
    try:
        _validate_public_url(value)
        parts = urlsplit(value)
        if parts.scheme != 'https' or parts.username or parts.password:
            return ''
        if parts.hostname == 'senat.fr' or (parts.hostname or '').endswith('.senat.fr'):
            return ''
        return value
    except (ValidationError, ValueError):
        return ''


def fields(value, prefix=()):
    """Human labels for every transmitted leaf; no object stringification or HTML."""
    if isinstance(value, dict):
        if not value:
            yield {'label': ' › '.join(prefix) or 'Détail', 'text': 'Non renseigné', 'url': ''}
        for key, child in value.items():
            if str(key).lower() in PRIVATE_KEYS or str(key).lower().startswith(('private_', 'credential_', 'prompt_')):
                yield {'label': 'Champ exclu de la présentation', 'text': 'Champ privé non affiché', 'url': ''}
                continue
            yield from fields(child, prefix + (LABELS.get(key, str(key).replace('_', ' ').capitalize()),))
    elif isinstance(value, list):
        if not value:
            yield {'label': ' › '.join(prefix) or 'Détail', 'text': 'Aucun élément transmis ; absence non démontrée', 'url': ''}
        for index, child in enumerate(value):
            yield from fields(child, prefix + (str(index + 1),))
    else:
        text = _text(value)
        if LOCAL_PATH.search(text):
            text = 'Référence locale non affichée'
        url = _url(text) if text.lower().startswith(('https:', 'http:', 'javascript:', 'file:', 'data:')) else ''
        senate_text = False
        if text.startswith('https://'):
            try:
                host = urlsplit(text).hostname or ''
                senate_text = host == 'senat.fr' or host.endswith('.senat.fr')
            except ValueError:
                pass
        if text.lower().startswith(('https:', 'http:', 'javascript:', 'file:', 'data:')) and not url and not senate_text:
            text = 'Lien non admissible'
        yield {'label': ' › '.join(prefix) or 'Détail', 'text': STATE_LABELS.get(text, text), 'url': url}


def _rows(value):
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return [] if value is None else [value]
    for key in ('dependencies', 'certificates', 'assertions', 'claims', 'decisions', 'items', 'unknowns', 'checks', 'findings', 'axes'):
        if isinstance(value.get(key), list):
            return value[key]
    return [value]


def _links(row, legacy_links, passages):
    if not isinstance(row, dict):
        return []
    links = []
    identity = row.get('claim_id') or row.get('assertion_id') or row.get('id')
    if isinstance(identity, str):
        links.extend(legacy_links.get(identity, []))
    passage_id = row.get('passage_id')
    if type(passage_id) is int and passage_id in passages:
        links.append({'href': '#passage-' + str(passage_id), 'label': 'Passage exact et contexte source'})
    return links


def build_architecture_review(job, assessment, *, query=None, history=(), legacy_review=None):
    query = query or {}
    selected = query.get('problem', 'all')
    selected = selected if selected in FILTERS else 'all'
    base = {'schema': SCHEMA, 'available': False, 'reason': 'Évaluation des quatre couches non reçue pour cette empreinte.',
            'selected_filter': selected, 'filters': [{'value': key, 'label': label} for key, label in FILTERS.items()],
            'history': list(history), 'human_note': 'Aucune revue personnelle n’est créée par cette présentation.'}
    base['historical_negative'] = any(row.get('decision') == 'not_supported'
                                      and row.get('fingerprint') == job.fingerprint for row in base['history'])
    if (not isinstance(assessment, dict) or assessment.get('schema') not in {ASSESSMENT_SCHEMA, DEPENDENCY_ASSESSMENT_SCHEMA}
            or assessment.get('revision_fingerprint') != job.fingerprint):
        return base
    layers = assessment.get('layers', {})
    if not isinstance(layers, dict) or set(layers) != set(LAYER_LABELS):
        return {**base, 'reason': 'Évaluation incomplète : une famille de preuve manque.'}
    gates = assessment.get('gates', {})
    if not isinstance(gates, dict) or set(gates) != set(GATE_LABELS):
        return {**base, 'reason': 'Évaluation incomplète : une porte documentaire manque.'}
    elements = []
    categories = {**layers, 'editorial': assessment.get('editorial_rules', layers['editorial']),
                  'unknowns': assessment.get('unknowns', layers['unknowns']),
                  'objection': assessment.get('objections', []),
                  'counterevidence': assessment.get('counterevidence', {}),
                  'rights': [gates.get('rights_qualified', {})] + list(assessment.get('rights_current', []))}
    graph = assessment.get('dependency_graph')
    if assessment.get('schema') == DEPENDENCY_ASSESSMENT_SCHEMA:
        if not isinstance(graph, dict) or graph.get('fingerprint') != job.fingerprint:
            return {**base, 'reason': 'Graphe des preuves manquant ou lié à une autre conclusion.'}
        categories['dependencies'] = graph
    passages = {p.get('id') for s in job.input_snapshot.get('sources', []) for p in s.get('passages', [])}
    legacy_links = {}
    for attempt in (legacy_review or {}).get('attempts', []):
        for group in attempt.get('groups', []):
            for claim in group.get('claims', []):
                legacy_links.setdefault(claim['id'], []).append(
                    {'href': '#' + claim['anchor'], 'label': 'Jugement canonique et fragment reçu'})
    for category, value in categories.items():
        for position, row in enumerate(_rows(value)):
            elements.append({'category': category, 'category_label': FILTERS[category],
                             'anchor': f'architecture-{category}-{position + 1}',
                             'fields': list(fields(row)), 'links': _links(row, legacy_links, passages)})
    visible = [row for row in elements if selected == 'all' or row['category'] == selected]
    page = Paginator(visible, 30).get_page(query.get('proof_page'))
    preflight = assessment.get('preflight', {})
    preflight = preflight if isinstance(preflight, dict) else {}
    summary = assessment.get('summary', {})
    summary = summary if isinstance(summary, dict) else {'detail': summary}
    return {**base, 'available': True, 'reason': '',
            'has_dependencies': isinstance(graph, dict),
            'conclusion_fields': list(fields(assessment.get('conclusion', {}))),
            'dependency_fields': list(fields(graph or {})),
            'blocking_checks': [{'code': row.get('code'), 'fields': list(fields(row))}
                for row in preflight.get('checks', []) if row.get('status') in {'hold', 'reject'}],
            'documentary_status': assessment.get('documentary_status', 'insufficient_evidence'),
            'documentary_label': STATE_LABELS.get(assessment.get('documentary_status'), 'État documentaire non reconnu'),
            'summary_fields': list(fields(summary)),
            'layers': [{'key': key, 'label': label, 'anchor': 'architecture-layer-' + key,
                        'fields': list(fields({k: v for k, v in layers[key].items()
                                             if k in {'status', 'summary', 'reason', 'counts'}} if isinstance(layers[key], dict)
                                            else layers[key]))} for key, label in LAYER_LABELS.items()],
            'gate_rows': [{'key': key, 'label': label, 'fields': list(fields(gates[key]))} for key, label in GATE_LABELS.items()],
            'preflight_status': STATE_LABELS.get(preflight.get('status'), 'Non reçu'),
            'preflight_fields': list(fields(preflight)),
            'cost_fields': list(fields(preflight.get('cost', {}))),
            'counterevidence_fields': list(fields(assessment.get('counterevidence', {}))),
            'objection_fields': list(fields(assessment.get('objections', []))),
            'element_count': len(elements), 'filtered_count': len(visible), 'page': page,
            'page_prefix': '?problem=' + selected + '&proof_page=',
            'warning': 'Les filtres changent le détail affiché. Ils ne changent ni le verdict, ni les réserves, ni les portes.'}
