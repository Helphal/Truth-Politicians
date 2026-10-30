"""Private candidate method, never maps For/Against to political agreement."""
from fractions import Fraction
from django.core.exceptions import ValidationError
from atelier.models import IndicatorContribution,IndicatorAggregate
from .canonical import sha256_digest

METHOD='DV-INDICATOR-UNWEIGHTED-CANDIDATE-1'
QUALIFICATIONS={'convergence','divergence','abstention','non_participation','mixed','evolution','not_comparable','inconclusive','group_divided'}
FIELDS={'case_id','actor_id','actor_type','declaration_id','act_id','institution','period_start','period_end','source_revision','qualification','vote_category','identity_resolved','nominative_available','admissible','zero_evidence','repetition_key','sources'}

def contribution(entry):
    if not isinstance(entry,dict) or set(entry)!=FIELDS:raise ValidationError('indicator_shape_invalid')
    if entry['qualification'] not in QUALIFICATIONS:raise ValidationError('indicator_qualification_unknown')
    if any(type(entry[k]) is not bool for k in ('identity_resolved','nominative_available','admissible')):
        raise ValidationError('indicator_status_invalid')
    if not isinstance(entry['zero_evidence'],list) or any(not isinstance(e,dict) or not e.get('locator') or e.get('source') not in entry['sources'] for e in entry['zero_evidence']):
        raise ValidationError('indicator_zero_evidence_invalid')
    if type(entry['source_revision']) is not int or entry['source_revision']<1 or not entry['sources'] or not entry['case_id'] or not entry['repetition_key']:
        raise ValidationError('indicator_binding_missing')
    from datetime import date
    try:
        if date.fromisoformat(entry['period_end'])<date.fromisoformat(entry['period_start']):raise ValueError()
    except (ValueError,TypeError):raise ValidationError('indicator_period_invalid')
    result={**entry,'method_version':METHOD,'method_status':'candidate_not_approved','value':None,'zero_subtype':None,'exclusion_reason':None}
    if not entry['identity_resolved']:reason='identity_unresolved'
    elif not entry['nominative_available'] or entry['vote_category'] in {'unknown','not_recorded'}:reason='individual_vote_unavailable'
    elif not entry['admissible']:reason='relationship_not_admissible'
    elif entry['actor_type']!='person':reason='collective_rule_pending'
    elif entry['qualification'] in {'mixed','evolution','not_comparable','inconclusive','group_divided'}:reason='qualification_not_mapped'
    elif entry['qualification'] in {'abstention','non_participation'}:
        expected='abstention' if entry['qualification']=='abstention' else 'non_voting'
        if entry['vote_category']!=expected or not entry['zero_evidence']:reason='zero_not_documented'
        else:
            result.update(value=0,zero_subtype=entry['qualification']);reason=None
    elif entry['vote_category'] not in {'for','against'}:reason='qualification_vote_inconsistent'
    else:
        # Qualification of the relationship, not the official vote direction.
        result['value']=1 if entry['qualification']=='convergence' else -1;reason=None
    result['exclusion_reason']=reason
    return result

def save_contribution(revision,entry):
    revision.refresh_from_db()
    if revision.dossier.data_scope!='synthetic':
        raise ValidationError('indicator_real_contribution_requires_adopted_review_workflow')
    if revision.dossier.content_kind!='speech_vote' or entry['source_revision']!=revision.revision or entry['case_id']!=revision.dossier.stable_id or entry['sources']!=revision.source_snapshot:
        raise ValidationError('indicator_revision_binding_invalid')
    payload=contribution(entry)
    if payload['value'] in {-1,1} and entry['qualification']!=revision.public_payload.get('result'):
        raise ValidationError('indicator_relationship_disagrees_with_reviewed_result')
    obj,created=IndicatorContribution.objects.get_or_create(revision=revision,defaults={'payload':payload,'payload_digest':sha256_digest(payload)})
    if obj.payload!=payload:raise ValidationError('indicator_contribution_requires_new_revision')
    return obj

def aggregate(entries,*,corpus_version,period_start,period_end):
    if not corpus_version or period_end<period_start:raise ValidationError('indicator_corpus_missing')
    from collections import Counter
    duplicates=Counter(e['case_id'] for e in entries)
    repetitions=Counter(e['repetition_key'] for e in entries)
    included=[];excluded=[];counts={'+1':0,'0':0,'-1':0};zeros={}
    for entry in entries:
        if entry.get('method_version')!=METHOD:raise ValidationError('indicator_method_mismatch')
        if contribution({k:entry[k] for k in FIELDS})!=entry:raise ValidationError('indicator_derived_value_mismatch')
        reason=entry.get('exclusion_reason')
        if duplicates[entry['case_id']]>1 or repetitions[entry['repetition_key']]>1:reason='duplicate_case_or_repetition'
        if entry['period_start']<period_start or entry['period_end']>period_end:reason='outside_period'
        if reason or entry['value'] is None:
            excluded.append({'case_id':entry['case_id'],'revision':entry['source_revision'],'reason':reason or 'value_unavailable'});continue
        if type(entry['value']) is not int or entry['value'] not in {-1,0,1}:raise ValidationError('indicator_value_invalid')
        key={1:'+1',0:'0',-1:'-1'}[entry['value']];counts[key]+=1;included.append(entry)
        if entry['value']==0:
            if entry['zero_subtype'] not in {'abstention','non_participation'}:raise ValidationError('indicator_zero_type_missing')
            zeros[entry['zero_subtype']]=zeros.get(entry['zero_subtype'],0)+1
    n=len(included);value=Fraction(counts['+1']-counts['-1'],n) if n else None
    return {'format':'dv.indicator-candidate/0.1.0-draft.1','method_version':METHOD,'method_status':'candidate_not_approved',
            'corpus_version':corpus_version,'period_start':period_start,'period_end':period_end,'counts':counts,'zero_subtypes':zeros,'denominator':n,
            'value':None if value is None else {'numerator':value.numerator,'denominator':value.denominator},'included':included,'excluded':excluded,
            'publication_authorized':False,'limitation':'Sélection de dossiers, pas couverture complète des mandats ; moyenne proposée, non décidée.'}

def prepare_aggregate(contributions,**scope):
    # Derived authority is rechecked, never trusted from the caller's numeric entry.
    from .publication import evaluate_revision
    from .detached_review import verify_current_receipt
    items=[]
    for item in contributions:
        item.refresh_from_db();revision=item.revision
        review=revision.human_reviews.filter(environment='test').order_by('-created_at','-pk').first()
        if not evaluate_revision(revision,'test').allowed or not review or review.created_via!='detached_ui' or not verify_current_receipt(review):
            raise ValidationError('indicator_contribution_not_reviewed_current')
        if item.payload_digest!=sha256_digest(item.payload):raise ValidationError('indicator_contribution_changed')
        items.append(item.payload)
    payload=aggregate(items,**scope);digest=sha256_digest(payload)
    return IndicatorAggregate.objects.get_or_create(input_digest=digest,defaults={'corpus_version':scope['corpus_version'],'method_version':METHOD,'payload':payload})[0]

def revalidate_aggregate(aggregate_object):
    from .publication import evaluate_revision
    from .detached_review import verify_current_receipt
    valid=sha256_digest(aggregate_object.payload)==aggregate_object.input_digest
    for item in aggregate_object.payload['included']:
        contribution_object=IndicatorContribution.objects.filter(payload__case_id=item['case_id'],revision__revision=item['source_revision']).first()
        if contribution_object is None or contribution_object.payload!=item or not evaluate_revision(contribution_object.revision,'test').allowed:
            valid=False;break
        review=contribution_object.revision.human_reviews.filter(environment='test').order_by('-created_at','-pk').first()
        if not review or review.created_via!='detached_ui' or not verify_current_receipt(review):
            valid=False;break
    if not valid:
        IndicatorAggregate.objects.filter(pk=aggregate_object.pk).update(valid=False)
    aggregate_object.refresh_from_db()
    return aggregate_object.valid


def export_candidate(aggregate_object):
    if not revalidate_aggregate(aggregate_object):raise ValidationError('indicator_aggregate_invalidated')
    # No public transport adapter until the method and Site contract are adopted.
    return {**aggregate_object.payload,'publication_authorized':False,'destination':'private_candidate_only'}
