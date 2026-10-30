"""Private official categories with explicit denominators, not invented seat geometry."""
from collections import Counter
from django.core.exceptions import ValidationError
from .catalogue import source_ref

def scrutiny_distribution(scrutiny):
    fields={'for':scrutiny.count_for,'against':scrutiny.count_against,'abstention':scrutiny.count_abstention}
    if any(type(v) is not int or v<0 for v in fields.values()) or type(scrutiny.voters) is not int or type(scrutiny.expressed) is not int:
        raise ValidationError('distribution_official_totals_unavailable')
    if fields['for']+fields['against']!=scrutiny.expressed or sum(fields.values())!=scrutiny.voters:
        raise ValidationError('distribution_official_totals_contradictory')
    rows=list(scrutiny.observations.select_related('affiliation_at_event','source_document__source'))
    observed=Counter(row.normalized_category for row in rows)
    groups={}
    for row in rows:
        if not row.affiliation_at_event_id:continue
        key=row.affiliation_at_event.stable_id
        groups.setdefault(key,{'organization_id':key,'observed_on':scrutiny.occurred_on.isoformat(),'counts':{},'sources':[]})
        g=groups[key];g['counts'][row.normalized_category]=g['counts'].get(row.normalized_category,0)+1
        ref=source_ref(row.source_document)
        if ref not in g['sources']:g['sources'].append(ref)
    return {'format':'dv.official-distribution-candidate/0.1.0-draft.1','scrutiny_id':scrutiny.stable_id,
        'occurred_on':scrutiny.occurred_on.isoformat(),'institution':scrutiny.institution,'official_counts':fields,
        'proportion_base':{'category':'voters','value':scrutiny.voters},'expressed':scrutiny.expressed,
        'nominative_status':scrutiny.nominative_status,'observed_counts':dict(sorted(observed.items())),
        'observed_rows':len(rows),'nominative_list_reconciled':all(observed[k]==v for k,v in fields.items()),
        'group_observations_at_vote':list(groups.values()),'groups_are_not_party_or_current_affiliation':True,
        'source_versions':[source_ref(scrutiny.source_document)],'publication_authorized':False,
        'qualification':'private_totals_only_exact_act_may_be_pending','seat_coordinates':None}
