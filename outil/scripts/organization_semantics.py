"""Versioned interpretation of institutional labels, never an inference of party membership."""
import unicodedata

VERSION='dv-organization-semantics/2'
BRIDGE='dv-site-bridge/0.2.0-draft.1'
LABELS={'party':'parti ou groupement politique référencé','group':'groupe parlementaire référencé',
 'institution':'institution','non_attachment':'catégorie de non-rattachement',
 'non_membership':'catégorie de non-inscription','administrative':'réunion administrative',
 'unqualified':'organisation de nature non qualifiée'}
RELATIONS={'party_membership':'adhésion partisane documentée','funding_attachment':'rattachement de financement documenté',
 'group_member':'membre de groupe','related':'apparenté','administrative_attachment':'rattachement administratif',
 'non_membership':'non-inscription documentée','non_attachment':'non-rattachement déclaré',
 'declared_attachment':'rattachement déclaré, nature non qualifiée','unknown':'nature de relation inconnue'}

def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFD',value.lower().replace('’',"'")) if not unicodedata.combining(c))

def classify_organization(data,raw_code):
    name=normalized(data['name'])
    if 'non rattache' in name or 'aucun rattachement' in name:kind='non_attachment';rule='explicit_non_attachment_label'
    elif 'reunion administrative' in name:kind='administrative';rule='explicit_administrative_label'
    elif 'non inscrit' in name or "n'appartenant a aucun groupe" in name:kind='non_membership';rule='explicit_non_membership_label'
    elif raw_code in ('GP','GROUPESENAT','ODSEN_GROUP'):kind='group';rule='institutional_group_category'
    elif raw_code=='PARPOL':kind='party';rule='institutional_party_or_grouping_category_not_legal_status'
    elif raw_code in ('ASSEMBLEE','ODSEN_INSTITUTION','AN_CONGRES_LIEUVOTE'):kind='institution';rule='institutional_body'
    else:kind='unqualified';rule='unrecognized_source_code'
    return {**data,'type':kind,'raw_type_code':raw_code,'qualification':{
      'version':VERSION,'status':'source_category' if kind!='unqualified' else 'unresolved',
      'rule':rule,'source_label':data['name'],'valid_from':data.get('valid_from'),'valid_to':data.get('valid_to'),
      'limit':'Qualification du code et du libellé à leur période source ; pas de statut actuel, juridique ou adhésion déduits.'}}

def classify_relation(data,org,raw_code):
    role=normalized(data.get('role') or '');kind=org['type']
    if kind in ('non_attachment','non_membership'):relation=kind
    elif kind=='administrative':relation='administrative_attachment'
    elif kind=='party':relation='declared_attachment'
    elif kind=='group':
        if 'non-inscrit' in role or 'non inscrit' in role:relation='unknown'
        elif 'apparente' in role:relation='related'
        elif 'rattache' in role:relation='administrative_attachment'
        elif any(x in role for x in ['membre','president','delegue']):relation='group_member'
        else:relation='unknown'
    else:relation='unknown'
    return {**data,'raw_type_code':raw_code,'relation_kind':relation,'relation_qualification':{
      'version':VERSION,'raw_role':data.get('role',''),'organization_type':kind,
      'status':'unresolved' if relation in ('unknown','declared_attachment') else 'source_category',
      'limit':'Le libellé Membre ne prouve pas une adhésion partisane ; le financement exige une attestation explicite.'}}
