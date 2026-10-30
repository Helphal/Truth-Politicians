"""Public coverage statements derived from explicitly pinned received manifests."""
import argparse,hashlib,json
from pathlib import Path
from export_public_v1 import canonical

def read_pinned(path,expected):
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('manifest_hash_mismatch')
    data=json.loads(raw)
    if data.get('schema')!='dv-public-export-v1':raise ValueError('manifest_schema')
    return data

def main():
    p=argparse.ArgumentParser()
    for name in ['an','senate','territory']:
        p.add_argument('--'+name,type=Path,required=True);p.add_argument('--'+name+'-sha256',required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.exists():raise ValueError('fresh_output_required')
    an=read_pinned(a.an,a.an_sha256);sen=read_pinned(a.senate,a.senate_sha256);territory=read_pinned(a.territory,a.territory_sha256)
    counts=an['counts'];losses=an['losses'];sc=sen['counts'];sl=sen['losses']
    stamp=max(an['created_at'],sen['created_at'])
    notes=[
      ('AN','Assemblée nationale : XVIe et XVIIe législatures',
       f"Copie reçue le {stamp[:10]}. {counts['scrutin']} scrutins et {counts['vote']} positions nominatives dans l'export Assemblée, y compris les scrutins du Congrès explicitement distingués. "
       f"{counts['person']} personnes, {counts['mandate']} mandats et {counts['affiliation']} affiliations exportés. "
       f"{losses['vote_actor_unresolved']} références de personne non résolues sont exclues des positions ; {losses['vote_group_unresolved_retained_as_source_ref']} références de groupe non résolues restent inconnues, avec leur référence source. "
       "Les bornes non documentées ne sont pas reconstruites ; les mandats hors périmètre sont exclus."),
      ('SENAT','Sénat : votes nominatifs depuis 2022',
       f"Copie reçue le {stamp[:10]}. Scrutins nominatifs depuis le 01/01/2022 ; mandats et affiliations chevauchant cette période. "
       f"{sc['scrutin']} scrutins, {sc['vote']} positions, {sc['mandate']} mandats et {sc['affiliation']} affiliations exportés. "
       f"Pour {sl['active_mandate_start_not_documented']} mandats actuels, la date de début n'est pas documentée : seule la présence à la date d'attestation est retenue. "
       "Une position absente de la source ne devient ni une abstention ni une absence supposée."),
      ('DOCUMENTS','Textes, dossiers et versions : limites des liens',
       f"{counts['dossier']} dossiers, {counts['text']} textes et {counts['text_version']} versions documentaires reçus des exports de l'Assemblée. "
       f"{losses['document_dossier_not_resolved']} références de dossier non résolues sont conservées comme telles. "
       "Les liens vers une version exacte ou un texte ne sont établis que lorsqu'ils sont attestés. Le catalogue de versions ne prouve pas à lui seul quelle version a été soumise à un scrutin. "
       "Les métadonnées et les liens institutionnels restent accessibles quand le texte intégral ou un lien exact manque."),
      ('TERRITORIES','Territoires sénatoriaux : attestation actuelle',
       f"{territory['counts']['mandate']} mandats portent un territoire tiré du champ Circonscription de la copie ODSEN_GENERAL du {stamp[:10]}. "
       "Ce territoire décrit l'attestation actuelle ; il n'est pas attribué aux périodes antérieures. Le filtre territorial historique ne s'appuie pas sur cette attestation actuelle. "
       "Les autres données historiques restent consultables lorsque leurs dates sont documentées."),
    ]
    records=[]
    for key,label,details in notes:
        data={'scope_type':'corpus','scope_external_id':key,'label':label,'details':details}
        records.append({'record_type':'coverage','record_id':'DV:V1:COVERAGE:'+key,'revision':hashlib.sha256(canonical(data).encode()).hexdigest(),
                        'operation':'upsert','is_test':False,'publication_circuit':'factual','data':data})
    revision=hashlib.sha256(canonical(records).encode()).hexdigest()
    batch={'bridge_schema':'dv-site-bridge/0.1.0-draft.1','batch_id':'DV-COVERAGE-'+revision[:20],
           'idempotency_key':'DV-COVERAGE-'+revision[:20],'generated_at':stamp,'records':records}
    raw=canonical(batch).encode();a.output.mkdir(parents=True);(a.output/'batches').mkdir();(a.output/'batches/0001.json').write_bytes(raw)
    manifest={'schema':'dv-public-export-v1','created_at':stamp,'input_manifest_sha256':{'an':a.an_sha256,'senate':a.senate_sha256,'territory':a.territory_sha256},
              'counts':{'coverage':len(records)},'batches':[{'file':'batches/0001.json','sha256':hashlib.sha256(raw).hexdigest(),'type':'coverage','records':len(records)}],
              'source_receipts':an['source_receipts']+sen['source_receipts'],'editorial_authorization':False}
    (a.output/'MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':'exported','coverage':len(records),'batch_sha256':hashlib.sha256(raw).hexdigest()}))

if __name__=='__main__':main()
