"""Contrat fermé documentaire 0.2 ; aucune autorité parole-vote ou réseau."""
import hashlib
import json
import re
from datetime import date
from urllib.parse import urlsplit

FORMAT = 'dv.explain-public/0.2.0'
ENTRY_FIELDS = {'stable_id', 'slug', 'revision', 'expects_revision', 'operation',
                'title', 'body', 'scope', 'reserve', 'credit', 'verified_on',
                'sources', 'assertions', 'qualification', 'content_sha256'}
CONTENT_FIELDS = {'stable_id', 'slug', 'revision', 'title', 'body', 'scope',
                  'reserve', 'credit', 'verified_on', 'sources', 'assertions', 'qualification'}


def digest(entry):
    return hashlib.sha256(json.dumps({k: entry[k] for k in sorted(CONTENT_FIELDS)},
                                    ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate(payload):
    if not isinstance(payload, dict) or set(payload) != {'format', 'corpus', 'partisan_commentary', 'entries'}:
        raise ValueError('enveloppe_invalide')
    if payload['format'] != FORMAT or payload['corpus'] not in ('test', 'real') or payload['partisan_commentary'] is not False:
        raise ValueError('contrat_ou_corpus_invalide')
    entries = payload['entries']
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ValueError('entrees_invalides')
    seen, slugs = set(), set()
    for e in entries:
        if not isinstance(e, dict) or set(e) != ENTRY_FIELDS:
            raise ValueError('champs_fiche_invalides')
        for key in ENTRY_FIELDS - {'sources', 'assertions'}:
            if not isinstance(e[key], str) or len(e[key]) > 20000:
                raise ValueError('champ_texte_invalide:' + key)
        if not re.fullmatch(r'explain\.[a-z0-9_.-]{1,110}', e['stable_id']) or not re.fullmatch(r'[a-z0-9-]{1,50}', e['slug']):
            raise ValueError('identite_invalide')
        if e['stable_id'] in seen or e['slug'] in slugs:
            raise ValueError('identite_dupliquee')
        seen.add(e['stable_id']); slugs.add(e['slug'])
        if not e['revision'] or len(e['revision']) > 64 or len(e['title']) > 255:
            raise ValueError('revision_ou_titre_invalide')
        if e['operation'] not in ('upsert', 'withdraw') or e['qualification'] != 'qualified_documentary':
            raise ValueError('brouillon_ou_operation_refuse')
        if any(not e[k].strip() for k in ('title', 'body', 'scope', 'reserve', 'credit')):
            raise ValueError('qualification_incomplete')
        verified = date.fromisoformat(e['verified_on'])
        if verified > date.today():
            raise ValueError('qualification_future')
        if not isinstance(e['sources'], list) or not 1 <= len(e['sources']) <= 12:
            raise ValueError('sources_absentes')
        for source in e['sources']:
            if not isinstance(source, dict) or set(source) != {'url', 'publisher', 'locator', 'consulted_on', 'text_sha256'}:
                raise ValueError('preuve_source_invalide')
            if any(not isinstance(v, str) or not v or len(v) > 2000 for v in source.values()):
                raise ValueError('preuve_source_invalide')
            parsed = urlsplit(source['url'])
            if parsed.scheme != 'https' or parsed.hostname not in ('www.assemblee-nationale.fr', 'www.senat.fr', 'www.legifrance.gouv.fr', 'www.conseil-constitutionnel.fr') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None, 443):
                raise ValueError('source_institutionnelle_refusee')
            if not re.fullmatch(r'[a-f0-9]{64}', source['text_sha256']) or date.fromisoformat(source['consulted_on']) > verified:
                raise ValueError('preuve_source_invalide')
        if not isinstance(e['assertions'], list) or not 1 <= len(e['assertions']) <= 20:
            raise ValueError('assertions_absentes')
        for assertion in e['assertions']:
            if not isinstance(assertion, dict) or set(assertion) != {'text', 'source_indexes'} or not isinstance(assertion['text'], str) or not assertion['text'].strip():
                raise ValueError('assertion_invalide')
            indices = assertion['source_indexes']
            if not isinstance(indices, list) or not indices or any(type(i) is not int or not 0 <= i < len(e['sources']) for i in indices):
                raise ValueError('assertion_non_sourcee')
        if e['body'] != '\n\n'.join(a['text'] for a in e['assertions']):
            raise ValueError('texte_non_qualifie')
        if e['content_sha256'] != digest(e):
            raise ValueError('empreinte_fiche_invalide')
    return payload
