"""Materialize pinned staging facts privately, without pretending to know the exact text."""
from pathlib import Path
import hashlib,json,zipfile
from django.core.exceptions import ValidationError
from django.db import transaction
from atelier.models import Scrutiny,TextVersion,LegislativeDossier,FactQualification,StagedScrutiny
from .canonical import sha256_digest
from .archive_staging import pinned_members
from .parsers.assembly import normalize_scrutiny


def qualify(scrutiny,*,state,object_type,expected_pieces,references,explanation):
    if state not in {'blocked','qualified'} or not references or (state=='qualified' and expected_pieces):
        raise ValidationError('qualification_incomplete')
    data=dict(state=state,object_type=object_type,expected_pieces=expected_pieces,references=references,explanation=explanation)
    return FactQualification.objects.get_or_create(scrutiny=scrutiny,version_digest=sha256_digest(data),defaults=data)[0]


@transaction.atomic
def materialize_staged(staged,path):
    if staged.document.quarantined or staged.document.source.access_status not in {'active','qualified'}:
        raise ValidationError('materialization_source_unavailable')
    members=pinned_members(staged.document,Path(path))
    info=next((m for m in members if m.filename==staged.locator),None)
    if info is None or info.file_size>10*1024*1024:raise ValidationError('materialization_member_invalid')
    with zipfile.ZipFile(path) as archive:raw=archive.read(info)
    if hashlib.sha256(raw).hexdigest()!=staged.raw_sha256:raise ValidationError('materialization_member_digest_mismatch')
    value=json.loads(raw.decode('utf-8-sig'));value=value.get('scrutin',value)
    record=normalize_scrutiny(value)
    if record['source_uid']!=staged.source_uid or record['institution']!=staged.institution:
        raise ValidationError('materialization_identity_mismatch')
    # A versioned identity repair is authoritative for this pinned stage only.
    if staged.qualification=='distinct_identity_exact_act_pending' and staged.legacy_scrutiny_id:
        corrected=staged.legacy_scrutiny
        if (corrected.institution,corrected.occurred_on.isoformat(),corrected.source_document_id,corrected.official_title)!=(record['institution'],record['occurred_on'],staged.document_id,record['official_title']):
            raise ValidationError('materialization_corrected_identity_mismatch')
        return corrected,False
    # Congress needs an independently identified route; do not guess it from AN.
    if record['institution']!='Assemblée nationale':raise ValidationError('congress_exact_vote_route_required')
    stable=f"AN:{record['legislature']}:SCRUTIN:{record['source_identifier']}"
    existing=Scrutiny.objects.filter(stable_id=stable).first()
    if existing:
        if existing.institution!=record['institution'] or existing.occurred_on.isoformat()!=record['occurred_on'] or existing.official_title!=record['official_title']:
            raise ValidationError('materialization_legacy_namespace_conflict')
        if existing.source_document_id!=staged.document_id:
            raise ValidationError('materialization_source_version_requires_reconciliation')
        return existing,False
    dossier,_=LegislativeDossier.objects.get_or_create(stable_id='UNQUALIFIED:'+staged.source_uid,defaults={
        'institution':record['institution'],'official_title':record['official_title'],'official_url':staged.document.source.discovery_url,'status':'exact_act_unresolved'})
    text,_=TextVersion.objects.get_or_create(stable_id='UNQUALIFIED:TEXT:'+staged.source_uid,defaults={
        'dossier':dossier,'kind':'unresolved','official_title':record['official_title'],'version_label':'Non identifiée — référence de collecte uniquement',
        'stage':'Qualification documentaire requise','relation':'other','official_url':staged.document.source.discovery_url,'source_document':staged.document})
    allowed=('source_identifier','institution','legislature','occurred_on','official_title','official_vote_url','result_status','voters','expressed','count_for','count_against','count_abstention','nominative_status')
    scrutiny=Scrutiny(stable_id=stable,**{k:record[k] for k in allowed},submitted_text=text,source_document=staged.document,
        procedure=(value.get('typeVote') or {}).get('libelleTypeVote','Procédure à qualifier'),
        counts_state='known' if all(type(record[k]) is int for k in ('count_for','count_against','count_abstention')) else 'unknown',
        anomaly='Acte exact et version du texte non qualifiés ; export interdit.')
    scrutiny.full_clean();scrutiny.save()
    qualify(scrutiny,state='blocked',object_type='normalized_scrutiny',expected_pieces=['Acte précisément visé et version du texte à la date du vote'],
            references=[{'source_sha256':staged.document.sha256,'member':staged.locator,'member_sha256':staged.raw_sha256,'official_vote_url':scrutiny.official_vote_url}],explanation='Normalisation depuis les octets officiels ; aucune qualification du texte ni autorisation publique.')
    StagedScrutiny.objects.filter(pk=staged.pk).update(legacy_scrutiny=scrutiny,qualification='normalized_exact_act_pending')
    return scrutiny,True
