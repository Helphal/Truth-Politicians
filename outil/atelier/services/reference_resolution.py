"""Institutional-ID resolution from pinned official AMO archives, no name matching.

Only professional names/IDs, institutional mandates and group dates are retained.
Private addresses, birth details, collaborators and contact information are ignored.
Every decision retains prior state and source version; revisions are never rewritten.
"""
from datetime import date
import json
import re
import zipfile
import time
from django.core.exceptions import ValidationError
from django.db import transaction
from atelier.models import Person, Organization, Mandate, Affiliation, ReferenceResolution, ArchiveScan
from .archive_staging import pinned_members

VERSION='DV-AN-REFERENCE-3'

# Types d'organe AMO documentés (open data AN) — pas d'inférence de parti hors PARPOL explicite.
ORGAN_KIND_BY_CODE={
    'GP':'parliamentary_group',
    'ASSEMBLEE':'institution',
    'PARPOL':'party',
    'GROUPESENAT':'parliamentary_group',
}

def scalar(value):
    return value.get('#text','') if isinstance(value,dict) else (value or '')

def sequence(value):
    return value if isinstance(value,list) else ([] if value is None else [value])

def minimal_record(kind,record):
    uid=str(scalar(record.get('uid')))
    if kind=='acteur':
        identity=(record.get('etatCivil') or {}).get('ident') or {}
        return {'ref':uid,'name':' '.join(str(scalar(identity.get(k))).strip() for k in ('prenom','nom')).strip()}
    if kind=='organe':
        life=record.get('viMoDe') or {}
        return {'ref':uid,'name':str(scalar(record.get('libelle'))),'type':str(scalar(record.get('codeType'))),
            'start':life.get('dateDebut'),'end':life.get('dateFin')}
    return {'ref':uid,'person_ref':str(scalar(record.get('acteurRef'))),'type':str(scalar(record.get('typeOrgane'))),
        'start':record.get('dateDebut'),'end':record.get('dateFin'),'end_field_present':'dateFin' in record,
        'organ_refs':[str(scalar(x)) for x in sequence((record.get('organes') or {}).get('organeRef'))]}

def _day(value):
    if value in (None,''):return None
    return date.fromisoformat(str(value)[:10])

@transaction.atomic
def resolve_record(document,kind,item,locator):
    if document.quarantined or document.source.access_status not in {'qualified','active'} or document.source.rights_status not in {'open','excerpt_allowed'}:
        raise ValidationError('reference_source_not_qualified')
    ref=item.get('ref','')
    pattern={'acteur':r'PA\d+','organe':r'PO\d+','mandat':r'PM\d+'}.get(kind)
    if pattern is None or not re.fullmatch(pattern,ref):raise ValidationError('reference_identifier_invalid')
    existing=ReferenceResolution.objects.filter(document=document,kind=kind,institutional_ref=ref).first()
    if existing:
        if existing.proposed!=item:raise ValidationError('reference_version_conflict')
        return existing.status,False
    previous={};status='pending';reason='unsupported_institutional_record'
    if kind=='acteur':
        person=Person.objects.select_for_update().filter(stable_id='AN:PERSON:'+ref).first()
        if person:
            previous={'display_name':person.display_name,'identity_status':person.identity_status,'institutional_ids':person.institutional_ids}
        name=item.get('name','')
        if not name or len(name)>250:
            reason='professional_name_missing_or_invalid'
        elif person and (person.identity_status=='conflict' or person.institutional_ids.get('assemblee_nationale')!=ref or (person.identity_status=='resolved' and person.display_name!=name)):
            status,reason='conflict','existing_identity_conflict_preserved'
            person.identity_status='conflict';person.save(update_fields=['identity_status','updated_at'])
        else:
            if person:
                person.display_name=name;person.identity_status='resolved';person.save(update_fields=['display_name','identity_status','updated_at'])
            else:
                Person.objects.create(stable_id='AN:PERSON:'+ref,display_name=name,identity_status='resolved',institutional_ids={'assemblee_nationale':ref})
            status,reason='resolved','exact_institutional_id'
    elif kind=='organe':
        org=Organization.objects.select_for_update().filter(stable_id='AN:ORG:'+ref).first()
        if org:previous={'name':org.name,'kind':org.kind,'valid_from':str(org.valid_from or ''),'valid_to':str(org.valid_to or '')}
        mapped=ORGAN_KIND_BY_CODE.get(item.get('type'))
        try:start,end=_day(item.get('start')),_day(item.get('end'))
        except ValueError:start,end=None,None;reason='invalid_date'
        if not mapped:reason='organ_kind_not_supported_no_party_inference'
        elif end and start and end<start:status,reason='conflict','contradictory_organ_dates'
        elif not item.get('name') or len(item['name'])>250:reason='organ_name_missing'
        elif org and (org.kind!=mapped or (not org.name.endswith('(libellé non résolu)') and org.name!=item['name']) or (org.valid_from is not None and org.valid_from!=start) or (org.valid_to is not None and org.valid_to!=end)):
            status,reason='conflict','existing_organ_conflict_preserved'
        elif reason!='invalid_date':
            Organization.objects.update_or_create(stable_id='AN:ORG:'+ref,defaults={'name':item['name'],'kind':mapped,'valid_from':start,'valid_to':end})
            status,reason='resolved','exact_institutional_id'
    else:
        person=Person.objects.filter(stable_id='AN:PERSON:'+item.get('person_ref',''),identity_status='resolved').first()
        try:start,end=_day(item.get('start')),_day(item.get('end'))
        except ValueError:start,end=None,None
        if person is None:reason='actor_reference_unresolved'
        elif start is None:reason='start_date_missing_or_invalid'
        elif item.get('end') and end is None:reason='end_date_invalid'
        elif not item.get('end_field_present'):reason='end_date_state_unknown'
        elif end and end<start:status,reason='conflict','contradictory_mandate_dates'
        elif item.get('type')=='ASSEMBLEE':
            mandate=Mandate.objects.filter(stable_id='AN:MANDATE:'+ref).first()
            proposed={'person_id':person.pk,'institution':'Assemblée nationale','mandate_type':'député','starts_on':start,'ends_on':end}
            if mandate:
                previous={k:str(getattr(mandate,k)) for k in proposed}
                if any(getattr(mandate,k)!=v for k,v in proposed.items()):status,reason='conflict','existing_mandate_conflict_preserved'
                else:status,reason='resolved','same_mandate_confirmed'
            else:
                Mandate.objects.create(stable_id='AN:MANDATE:'+ref,source_document=document,**proposed)
                status,reason='resolved','sourced_interval_open_end' if end is None else 'sourced_interval'
        elif item.get('type')=='GP':
            refs=item.get('organ_refs',[])
            org=Organization.objects.filter(stable_id='AN:ORG:'+refs[0],kind='parliamentary_group').first() if len(refs)==1 else None
            if org is None:reason='group_reference_unresolved'
            else:
                affiliation=Affiliation.objects.filter(person=person,organization=org,status='official_group_mandate',starts_on=start).first()
                if affiliation and affiliation.ends_on!=end:
                    status,reason='conflict','existing_affiliation_interval_preserved'
                    previous={'starts_on':str(affiliation.starts_on),'ends_on':str(affiliation.ends_on)}
                else:
                    if affiliation is None:Affiliation.objects.create(person=person,organization=org,status='official_group_mandate',starts_on=start,ends_on=end,observed_on=document.captured_at.date(),source_document=document,ambiguity='Qualité détaillée non normalisée ; aucun parti inféré.')
                    status,reason='resolved','sourced_group_interval_not_party'
        elif item.get('type')=='PARPOL':
            refs=item.get('organ_refs',[])
            org=Organization.objects.filter(stable_id='AN:ORG:'+refs[0],kind='party').first() if len(refs)==1 else None
            if org is None:reason='party_reference_unresolved'
            else:
                affiliation=Affiliation.objects.filter(person=person,organization=org,status='official_party_organ',starts_on=start).first()
                if affiliation and affiliation.ends_on!=end:
                    status,reason='conflict','existing_affiliation_interval_preserved'
                    previous={'starts_on':str(affiliation.starts_on),'ends_on':str(affiliation.ends_on)}
                else:
                    if affiliation is None:Affiliation.objects.create(person=person,organization=org,status='official_party_organ',starts_on=start,ends_on=end,observed_on=document.captured_at.date(),source_document=document,ambiguity='Rattachement organe PARPOL AMO ; aucune nuance électorale inférée.')
                    status,reason='resolved','sourced_party_organ_interval'
    ReferenceResolution.objects.create(document=document,kind=kind,institutional_ref=ref,locator=locator,previous=previous,proposed=item,status=status,reason=reason)
    return status,True

def _records_from_member(filename,value):
    """AMO10 : mandats imbriqués dans json/acteur/*.json ; organes dans json/organe/*.json."""
    records=[]
    acteur=value.get('acteur')
    if isinstance(acteur,dict):
        records.append(('acteur',minimal_record('acteur',acteur),filename))
        for row in sequence((acteur.get('mandats') or {}).get('mandat')):
            if isinstance(row,dict):
                records.append(('mandat',minimal_record('mandat',row),filename))
    for kind in ('acteur','organe','mandat'):
        if kind not in value or (kind=='acteur' and isinstance(value.get('acteur'),dict)):
            continue
        for row in sequence(value[kind]):
            if isinstance(row,dict):
                records.append((kind,minimal_record(kind,row),filename))
    return records

def import_reference_archive(document,path,*,max_records=50000,batch_size=1000,max_seconds=30,stop_requested=lambda:False):
    if type(batch_size) is not int or not 1 <= batch_size <= 1000 or not 0 < max_seconds <= 60:
        raise ValidationError('reference_batch_bounds_invalid')
    if type(max_records) is not int or not 1 <= max_records <= 50000:
        raise ValidationError('reference_record_bounds_invalid')
    if document.quarantined or document.source.access_status not in {'qualified','active'} or document.source.rights_status not in {'open','excerpt_allowed'}:
        raise ValidationError('reference_source_not_qualified')
    # AMO50 XVI has 42,569 small members. A specific bounded profile, not a
    # global ZIP-guard bypass; byte, ratio, path and per-member limits remain.
    members=pinned_members(document,path,max_files=50000)
    # Bound records retained in memory; minimize data immediately, before sorting.
    records=[]
    with zipfile.ZipFile(path) as archive:
        for member in members:
            if member.file_size>10*1024*1024:raise ValidationError('reference_member_limit')
            value=json.loads(archive.read(member).decode('utf-8-sig'))
            for entry in _records_from_member(member.filename,value):
                records.append(entry)
                if len(records)>max_records:raise ValidationError('reference_record_limit')
    if not records:raise ValidationError('reference_archive_shape_unsupported')
    records.sort(key=lambda x:(('acteur','organe','mandat').index(x[0]),x[1]['ref']))
    if len({(kind,item['ref']) for kind,item,_ in records})!=len(records):
        raise ValidationError('reference_duplicate_identifier')
    counts={}
    started=time.monotonic()
    with transaction.atomic():
        scan,_=ArchiveScan.objects.get_or_create(document=document,defaults={'parser_version':VERSION,'total_members':len(records)})
        scan=ArchiveScan.objects.select_for_update().get(pk=scan.pk)
        if scan.parser_version!=VERSION or scan.total_members!=len(records):
            raise ValidationError('reference_cursor_version_mismatch')
        scan.save(update_fields=['updated_at'])  # acquire the SQLite write lock before decisions
        for kind,item,locator in records[scan.cursor:scan.cursor+batch_size]:
            if stop_requested() or time.monotonic()-started>=max_seconds:break
            status,created=resolve_record(document,kind,item,locator)
            key=status if created else 'already_recorded'
            counts[key]=counts.get(key,0)+1
            scan.cursor+=1
        scan.completed=scan.cursor==len(records)
        scan.save(update_fields=['cursor','completed','updated_at'])
    return {'parser_version':VERSION,'source_sha256':document.sha256,'records':len(records),'cursor':scan.cursor,'complete':scan.completed,'decisions':counts,'historical_revisions_rewritten':0}
