"""Prepare attributed factual notices from frozen public exports, never publish."""
import argparse,hashlib,json
from collections import Counter,defaultdict
from pathlib import Path

SCHEMA='dv-notice-preparation/1'
KINDS={'person','organization','mandate','affiliation','source','coverage','text','text_version','dossier','scrutin'}
LICENCES={'assemblee-nationale.fr':'https://data.assemblee-nationale.fr/licence-ouverte-open-licence',
          'senat.fr':'https://data.senat.fr/licence/'}
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(obj):return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')

def load_exports(root):
    records={}; provenance={}; manifests=[]; source_archives=[]
    root=root.resolve()
    selection=root/'runtime'/'export-inputs.json'
    if not selection.is_file():
        raise ValueError('export_inputs_required: configure runtime/export-inputs.json with ordered relative MANIFEST.json paths')
    inputs=json.loads(selection.read_text(encoding='utf-8'))
    if not isinstance(inputs,list) or not inputs or any(not isinstance(item,str) or not item for item in inputs):
        raise ValueError('export_inputs_must_be_nonempty_path_list')
    if len(set(inputs))!=len(inputs):
        raise ValueError('export_inputs_duplicate_manifest')
    for relative in inputs:
        path=(root/relative).resolve()
        if Path(relative).is_absolute() or not path.is_relative_to(root):
            raise ValueError('export_manifest_outside_root')
        directory=path.parent
        raw=path.read_bytes();m=json.loads(raw.decode('utf-8-sig'))
        if m.get('schema')!='dv-public-export-v1':
            raise ValueError('unsupported_export_manifest_schema')
        manifests.append({'file':str(path.relative_to(root)),'sha256':sha(raw),'counts':m['counts']})
        for batch in m['batches']:
            if batch['type'] not in KINDS|{'delta'}:continue
            path=(directory/batch['file']).resolve()
            if not path.is_relative_to(directory):
                raise ValueError('export_batch_outside_manifest_directory')
            raw=path.read_bytes();assert sha(raw)==batch['sha256']
            data=json.loads(raw.decode('utf-8-sig'))
            assert len(data['records'])==batch['records']
            for i,record in enumerate(data['records']):
                if record['record_type'] not in KINDS:continue
                assert record['is_test'] is False and record['publication_circuit']=='factual'
                key=(record['record_type'],record['record_id']);records[key]=record
                provenance[key]={'batch_file':str(path.relative_to(root)),'batch_sha256':batch['sha256'],'record_index':i,
                                 'record_id':record['record_id'],'record_revision':record['revision'],'record_sha256':sha(encoded(record))}
    return records,provenance,manifests

def prepare(root,out):
    out.mkdir(parents=True,exist_ok=False)
    records,prov,manifests=load_exports(root)
    grouped=defaultdict(dict)
    for (kind,identifier),row in records.items():grouped[kind][identifier]=row['data']
    people=grouped['person'];orgs=grouped['organization'];sources=grouped['source']
    mandates=defaultdict(list);affiliations=defaultdict(list)
    for identifier,row in grouped['mandate'].items():mandates[row['person_external_id']].append((identifier,row))
    for identifier,row in grouped['affiliation'].items():affiliations[row['person_external_id']].append((identifier,row))
    def proof(kind,identifier):
        data=grouped[kind][identifier];sid=data.get('source_external_id');source=sources.get(sid,{})
        url=source.get('public_url','');licence=next((v for k,v in LICENCES.items() if k in url),'unknown')
        return {**prov[(kind,identifier)],'source_id':sid,'source_url':url,'source_version':source.get('exact_version'),
                'source_sha256':source.get('byte_sha256'),'source_checked_at':source.get('checked_at'),
                'source_locator':data.get('source_locator'),
                'evidence_locator':data.get('source_locator') or f"{prov[(kind,identifier)]['batch_file']}#records/{prov[(kind,identifier)]['record_index']}",
                'locator_scope':'institutional_source' if data.get('source_locator') else 'frozen_export_classification_only',
                'licence_url':licence,
                'extracted_facts':data}
    inventory=[];drafts=[]
    for identifier,person in sorted(people.items()):
        ms=sorted(mandates[identifier],key=lambda x:(x[1].get('starts_on') or '',x[0]),reverse=True)
        fs=affiliations[identifier]
        claims=[{'text':f"Le corpus documentaire identifie cette personne sous le nom « {person['display_name']} ».",'proofs':[proof('person',identifier)]}]
        for mid,m in ms:
            org=orgs.get(m.get('institution_external_id'),{})
            institution=org.get('name') or m.get('chamber') or 'institution non renseignée'
            text=f"Un mandat est documenté au sein de {institution}."
            text+=f" Début : {m['starts_on']}." if m.get('starts_on') else ' Date de début non renseignée.'
            text+=f" Fin : {m['ends_on']}." if m.get('ends_on') else ' Date de fin non renseignée.'
            if m.get('current_attested_on'):text+=f" La source atteste ce mandat au {m['current_attested_on']} ; cela ne vaut pas attestation à la date de consultation."
            if m.get('territory'):
                text+=f" Territoire documenté : {m['territory']}."
                if m.get('territory_attested_on'):text+=f" Cette indication territoriale est attestée au {m['territory_attested_on']}, sans rétroprojection."
            ps=[proof('mandate',mid)]
            if m.get('institution_external_id') in orgs:ps.append(proof('organization',m['institution_external_id']))
            claims.append({'text':text,'proofs':ps})
        party_links=[]
        for aid,a in sorted(fs):
            oid=a['organization_external_id'];o=orgs.get(oid)
            if o and o.get('type')=='party':
                party_links.append(aid)
                period=(f" Début documenté : {a['starts_on']}." if a.get('starts_on') else ' Début non renseigné.')+(f" Fin documentée : {a['ends_on']}." if a.get('ends_on') else ' Fin non renseignée.')
                claims.append({'text':f"Une relation explicite au parti « {o['name']} » est enregistrée dans la source.{period} Aucun rattachement actuel n'est déduit de l'absence de fin.",'proofs':[proof('affiliation',aid),proof('organization',oid)]})
        text='\n\n'.join(c['text'] for c in claims)
        draft={'schema':SCHEMA,'record_type':'person','record_id':identifier,'name':person['display_name'],'status':'prepared_pending_review',
               'notice_kind':'factual_documentary_summary_not_full_biography','text':text,'claims':claims,
               'political_analysis_authorized':False,'published':False,'wikipedia_reused':False}
        drafts.append(draft)
        inventory.append({'record_type':'person','record_id':identifier,'name':person['display_name'],'source_notice_present':bool(person.get('biography')),
                          'prepared_notice':True,'full_biography_status':'not_documented_in_received_sources','mandates':len(ms),'affiliations':len(fs),
                          'explicit_party_links':len(party_links),'party_unknown_not_inferred':not bool(party_links),
                          'slug':person['slug'],'draft_sha256':sha(encoded(draft))})
    for identifier,org in sorted(orgs.items()):
        label={'party':'parti politique','group':'groupe parlementaire','institution':'institution'}.get(org['type'])
        assert label
        text=f"« {org['name']} » est classé comme {label} dans le corpus institutionnel reçu."
        if org.get('chamber'):text+=f" Chambre documentée : {org['chamber']}."
        text+=' Cette qualification documentaire ne décrit ni son orientation politique ni son statut actuel.'
        if org.get('valid_from'):text+=f" Début de validité documenté : {org['valid_from']}."
        if org.get('valid_to'):text+=f" Fin de validité documentée : {org['valid_to']}."
        draft={'schema':SCHEMA,'record_type':'organization','record_id':identifier,'name':org['name'],'status':'prepared_pending_review',
               'notice_kind':'factual_documentary_summary_not_organization_history','text':text,
               'claims':[{'text':text,'proofs':[proof('organization',identifier)]}],
               'political_analysis_authorized':False,'published':False,'wikipedia_reused':False}
        drafts.append(draft)
        inventory.append({'record_type':'organization','record_id':identifier,'name':org['name'],'organization_type':org['type'],
                          'source_notice_present':bool(org.get('description')),'prepared_notice':True,'full_history_status':'not_documented_in_received_sources',
                          'slug':org['slug'],'draft_sha256':sha(encoded(draft))})
    for name,rows in [('notices.jsonl',drafts),('inventory.jsonl',inventory)]:
        with (out/name).open('w',encoding='utf-8') as stream:
            for row in rows:stream.write(encoded(row).decode()+'\n')
    missing_proof=sum(not p['source_url'] or not p['evidence_locator'] or not p['source_sha256'] for d in drafts for c in d['claims'] for p in c['proofs'])
    missing_original_locators=sorted({p['record_id'] for d in drafts for c in d['claims'] for p in c['proofs'] if not p['source_locator']})
    summary={'schema':SCHEMA,'status':'prepared_pending_review','input_manifests':manifests,'people':len(people),'organizations':len(orgs),
      'organizations_by_type':dict(Counter(o['type'] for o in orgs.values())),
      'source_notices_present':sum(r['source_notice_present'] for r in inventory),'source_notices_missing':sum(not r['source_notice_present'] for r in inventory),
      'factual_notices_prepared':len(drafts),'full_biographies_or_histories_prepared':0,
      'mandates':len(grouped['mandate']),'affiliations':len(grouped['affiliation']),
      'people_without_mandate':sum(not mandates[i] for i in people),'people_without_affiliation':sum(not affiliations[i] for i in people),
      'people_with_explicit_party_relation':sum(bool(r.get('explicit_party_links')) for r in inventory),
      'mandates_missing_start':sum(not m.get('starts_on') for m in grouped['mandate'].values()),
      'mandates_without_end_not_claimed_current':sum(not m.get('ends_on') for m in grouped['mandate'].values()),
      'scrutins_with_exact_text_version':sum(bool(v.get('text_version_external_id')) for v in grouped['scrutin'].values()),
      'scrutins_with_dossier':sum(bool(v.get('dossier_external_id')) for v in grouped['scrutin'].values()),
      'missing_claim_proof_components':missing_proof,'records_without_original_source_locator':missing_original_locators,
      'locator_limit':'These records have exact frozen export evidence only; no invented institutional locator.',
      'published':False,'human_review_claimed':False,
      'notice_format_change_to_bridge':False,'mapping':{'person':['biography','notice_sources'],'organization':['description']},
      'licences_checked_on':None,'licences':LICENCES,
      'outputs':{p.name:sha(p.read_bytes()) for p in out.glob('*.jsonl')}}
    assert len(drafts)==len(people)+len(orgs)
    assert not missing_proof
    (out/'receipt.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:summary[k] for k in ['status','people','organizations','organizations_by_type','source_notices_present','factual_notices_prepared','people_with_explicit_party_relation','missing_claim_proof_components']}))
    return summary

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();prepare(a.root.resolve(),a.output.resolve())
