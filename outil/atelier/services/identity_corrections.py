"""Versioned repair of a legacy mixed namespace from two pinned official records.

No vote observation, editorial reference or review is reassigned to a new fact.
The legacy row remains addressable and blocked, outside the active catalogue.
"""
import hashlib, json, zipfile
from pathlib import Path
from django.core.exceptions import ValidationError
from django.db import transaction
from atelier.models import Scrutiny, ScrutinyIdentityCorrection, StagedScrutiny, TextVersion, LegislativeDossier
from .archive_staging import pinned_members
from .parsers.assembly import normalize_scrutiny
from .materialization import qualify
from .canonical import sha256_digest


def pinned_record(staged, path):
    if staged.document.quarantined or staged.document.source.access_status not in {'active','qualified'}:
        raise ValidationError('correction_source_unavailable')
    info=next((m for m in pinned_members(staged.document,Path(path)) if m.filename==staged.locator),None)
    if info is None or info.file_size>10*1024*1024:raise ValidationError('correction_member_invalid')
    with zipfile.ZipFile(path) as archive:raw=archive.read(info)
    if hashlib.sha256(raw).hexdigest()!=staged.raw_sha256:raise ValidationError('correction_member_digest_mismatch')
    value=json.loads(raw.decode('utf-8-sig'));record=normalize_scrutiny(value.get('scrutin',value))
    if record['source_uid']!=staged.source_uid or record['institution']!=staged.institution:raise ValidationError('correction_member_identity_mismatch')
    record['procedure_label']=(record['raw'].get('typeVote') or {}).get('libelleTypeVote') or 'Procédure non décrite dans cette pièce'
    return record


@transaction.atomic
def separate_congress_collision(legacy,assembly,congress,*,archive_path,evidence):
    """Specific audited collision, not an arbitrary identity/URL override API."""
    legacy=Scrutiny.objects.select_for_update().get(pk=legacy.pk)
    existing=ScrutinyIdentityCorrection.objects.filter(legacy=legacy).first()
    if existing:return existing,False
    an=pinned_record(assembly,archive_path);cg=pinned_record(congress,archive_path)
    if (an['source_uid'],an['occurred_on'],cg['source_uid'],cg['occurred_on'])!=('VTANR5L16V1','2022-07-11','VTCGR5L16V1','2024-03-04'):
        raise ValidationError('correction_unexpected_identity_pair')
    if assembly.document_id!=congress.document_id or legacy.source_document_id!=assembly.document_id:
        raise ValidationError('correction_source_version_mismatch')
    if (legacy.stable_id,legacy.institution,legacy.legislature,legacy.source_identifier,legacy.occurred_on.isoformat())!=('AN:16:SCRUTIN:1','Assemblée nationale','16','1','2024-03-04'):
        raise ValidationError('correction_legacy_not_expected_collision')
    cg_url='https://www.assemblee-nationale.fr/dyn/16/scrutins/c1'
    if not isinstance(evidence,dict) or evidence.get('congress_vote_url')!=cg_url or evidence.get('assembly_vote_url')!=an['official_vote_url'] or not evidence.get('discovered_from'):
        raise ValidationError('correction_route_evidence_missing')
    original={f.attname:str(getattr(legacy,f.attname)) if f.get_internal_type() in {'DateField','DateTimeField'} else getattr(legacy,f.attname) for f in legacy._meta.concrete_fields}
    # Free the false institutional unique key, not its stable identifier or dependents.
    legacy.institution='Archive invalidée — collision institutionnelle'
    legacy.anomaly='Identité historique invalide : Congrès 2024 et AN 2022 mélangés. Ne pas utiliser ni réattribuer ses observations.'
    legacy.save(update_fields=['institution','anomaly','updated_at'])
    replacements=[]
    for staged,record,url in [(assembly,an,an['official_vote_url']),(congress,cg,cg_url)]:
        stable=('AN:' if record['institution']=='Assemblée nationale' else 'CGR:')+record['source_uid']
        dossier=LegislativeDossier.objects.create(stable_id='UNQUALIFIED:'+record['source_uid'],institution=record['institution'],official_title=record['official_title'],official_url=url,status='exact_act_unresolved')
        text=TextVersion.objects.create(stable_id='UNQUALIFIED:TEXT:'+record['source_uid'],dossier=dossier,kind='unresolved',official_title=record['official_title'],version_label='Acte exact à qualifier',stage='Qualification requise',relation='other',official_url=url,source_document=staged.document)
        fields={k:record[k] for k in ['source_identifier','institution','legislature','occurred_on','official_title','result_status','voters','expressed','count_for','count_against','count_abstention']}
        scrutiny=Scrutiny(stable_id=stable,**fields,official_vote_url=url,procedure=record['procedure_label'],submitted_text=text,source_document=staged.document,counts_state='known',nominative_status='not_available',anomaly='Identité distincte vérifiée ; acte exact et observations nominatives non encore raccordés.')
        scrutiny.full_clean();scrutiny.save()
        refs=[{'source_sha256':staged.document.sha256,'member':staged.locator,'member_sha256':staged.raw_sha256,'official_vote_url':url},evidence]
        qualify(scrutiny,state='blocked',object_type='distinct_institutional_vote',expected_pieces=['Qualification applicative de la version soumise et rattachement nominatif indépendant'],references=refs,explanation='Identité corrigée depuis deux membres officiels distincts ; pas de transfert des observations historiques ambiguës.')
        StagedScrutiny.objects.filter(pk=staged.pk).update(legacy_scrutiny=scrutiny,qualification='distinct_identity_exact_act_pending')
        replacements.append(stable)
    qualify(legacy,state='blocked',object_type='retired_namespace_collision',expected_pieces=['Ne jamais réutiliser cet objet historique ; examiner indépendamment toute ancienne dépendance'],references=[evidence,{'replacement_ids':replacements}],explanation='Quarantaine historique versionnée. Aucun ancien dossier, vote ou agrément réattribué.')
    result=ScrutinyIdentityCorrection.objects.create(legacy=legacy,original_snapshot=original,replacement_ids=replacements,evidence=evidence,correction_digest=sha256_digest({'original':original,'replacements':replacements,'evidence':evidence}))
    return result,True
