"""Versioned dissemination selection from the exact private editorial revision.

The Site receives selected excerpts, never the private evidence archive. This
module is deterministic: it makes no editorial, legal or political judgment.
"""
from copy import deepcopy
from django.core.exceptions import ValidationError
from .canonical import sha256_digest

PUBLIC_CONTRACT = 'dv.content/0.5.0-diffusion-draft.1'
PROVENANCE_CONTRACT = 'dv.content/0.6.0-provenance-draft.1'
CONCLUSION_CONTRACT = 'dv.content/0.7.0-conclusion-draft.1'
PUBLIC_PROFILES = frozenset({PUBLIC_CONTRACT, PROVENANCE_CONTRACT, CONCLUSION_CONTRACT})
SELECTION_CONTRACT = 'dv.diffusion-selection/1'
SNAPSHOT_SCHEMA = 'dv.diffusion-snapshot/1'
SITE_SCHEMA = 'dv.verification-site/3'
MAX_EXCERPT = 1200
MAX_TOTAL = 6000
PUBLIC_KEYS = frozenset({
    'title','short_answer','main_caveat','reasoning','result','declaration_quote','declaration_date',
    'declaration_date_state','declaration_source_url','official_text_url','official_vote_url','scope',
    'counterevidence','coverage','method_explanation','themes','catalogue','acts','evidence','attribution',
    'actors','declaration_date_precision','compared_actor_scope','collective_distribution','qualified_declaration',
})


def _require(condition, reason):
    if not condition: raise ValidationError('diffusion_selection_invalid:' + reason)


def _text(value, limit):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def validate_diffusion_selection(public_payload):
    """Validate pointers/literal inclusion. Does not qualify quote rights or meaning."""
    _require(isinstance(public_payload, dict) and public_payload.get('public_contract_version') in PUBLIC_PROFILES, 'profile')
    selection = public_payload.get('diffusion_selection')
    _require(isinstance(selection, dict) and set(selection) == {'contract','passages','public_note'}, 'shape')
    _require(selection['contract'] == SELECTION_CONTRACT, 'contract')
    _require(isinstance(selection['public_note'], str) and len(selection['public_note']) <= 2000, 'public_note')
    passages = selection['passages']; evidence = public_payload.get('evidence')
    _require(isinstance(passages, list) and 1 <= len(passages) <= 12 and isinstance(evidence,list), 'passages')
    seen = set(); total = 0; declaration_covered = False
    declaration = public_payload.get('declaration_quote')
    _require(_text(declaration, MAX_EXCERPT), 'declaration_length')
    for selected in passages:
        _require(isinstance(selected, dict) and set(selected) == {'evidence_index','exact_quote','attribution'}, 'passage_shape')
        index = selected['evidence_index']; quote = selected['exact_quote']
        _require(type(index) is int and 0 <= index < len(evidence), 'evidence_index')
        _require(_text(quote, MAX_EXCERPT) and _text(selected['attribution'],600), 'excerpt_or_attribution')
        _require((index,quote) not in seen, 'duplicate_excerpt'); seen.add((index,quote)); total += len(quote)
        original = evidence[index]
        _require(isinstance(original,dict) and isinstance(original.get('exact_quote'),str) and quote in original['exact_quote'], 'not_literal')
        source = original.get('source_version')
        _require(isinstance(source,dict) and set(source) == {'source_id','version_id','sha256','public_url'}, 'source_version')
        from .publication import _validate_public_url
        _validate_public_url(source['public_url'])
        _require(_text(original.get('locator'),5000), 'locator')
        if declaration in quote and source['public_url'] == public_payload.get('declaration_source_url'):
            declaration_covered = True
    _require(total <= MAX_TOTAL, 'total_excerpt_length')
    _require(declaration_covered, 'declaration_source_not_selected')


def selected_public_payload(public_payload):
    validate_diffusion_selection(public_payload)
    from .contracts import validate_speech_vote_payload
    validate_speech_vote_payload(public_payload)
    selection = public_payload['diffusion_selection']
    result = {key:deepcopy(value) for key,value in public_payload.items() if key in PUBLIC_KEYS}
    result['evidence'] = []
    for selected in selection['passages']:
        original = public_payload['evidence'][selected['evidence_index']]
        result['evidence'].append({'source_version':deepcopy(original['source_version']),
            'locator':original['locator'],'exact_quote':selected['exact_quote'],'attribution':selected['attribution']})
    result['origin_contract_version'] = public_payload['public_contract_version']
    if public_payload['public_contract_version'] == CONCLUSION_CONTRACT:
        if 'scrutiny_text_binding_refs' in public_payload:
            result['scrutiny_text_binding_refs'] = deepcopy(public_payload['scrutiny_text_binding_refs'])
        if 'personal_vote_selection' in public_payload:
            result['personal_vote_selection'] = deepcopy(public_payload['personal_vote_selection'])
        result['object_observations'] = []
        for observation in public_payload['object_observations']:
            # No complete working quote leaks outside the reviewed selection.
            covered = any(passage['source_version'] == observation['source_version']
                          and observation['exact_quote'] in passage['exact_quote']
                          for passage in result['evidence'])
            _require(covered and len(observation['exact_quote']) <= MAX_EXCERPT,
                     'object_observation_not_in_selected_excerpt')
            result['object_observations'].append(deepcopy(observation))
    result['diffusion'] = {'contract':SELECTION_CONTRACT, 'selection_sha256':sha256_digest(selection),
        'selected_passages':len(result['evidence']), 'public_note':selection['public_note'],
        'scope':'Selected dissemination only; the complete working evidence remains in the private Atelier.'}
    return result


def project_verified_content(verified_content):
    """Called only after the exporter checks the complete private snapshot receipt."""
    public = selected_public_payload(verified_content['public_payload'])
    refs = verified_content.get('source_snapshot')
    _require(isinstance(refs,list) and all(isinstance(ref,dict) and set(ref)=={'source_id','version_id','sha256','public_url'} for ref in refs),'source_snapshot_shape')
    _require(all(passage['source_version'] in refs for passage in public['evidence']),'source_not_bound')
    projected = {key:deepcopy(verified_content[key]) for key in (
        'title','short_answer','main_caveat','reasoning','result','method_version','source_snapshot')}
    projected['public_payload'] = public
    return {'schema':SNAPSHOT_SCHEMA,'content':projected}


def preview(revision):
    """Private draft preview. Presence never means qualified or approved."""
    try:
        public = selected_public_payload(revision.public_payload)
    except ValidationError as exc:
        return {'available':False,'error':'; '.join(exc.messages)}
    from .verification import gate
    allowed,reason = gate(revision)
    job = revision.verification_jobs.order_by('-pk').first()
    return {'verification_pending':not allowed and job is not None and job.state=='pending',
            'verification_blocked':revision.verification_required and not allowed,
            'verification_state':job.state_label if job else 'Vérification non demandée',
            'available':True,'passages':public['evidence'],'public_note':public['diffusion']['public_note'],
            'selection_sha256':public['diffusion']['selection_sha256'],
            'retained_private_passages':len(revision.public_payload.get('evidence',[]))}
