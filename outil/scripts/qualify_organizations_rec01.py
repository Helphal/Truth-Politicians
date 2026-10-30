"""Qualify the entire frozen organization/relationship inventory without rewriting votes."""
import argparse,csv,json,zipfile
from collections import Counter
from pathlib import Path
from prepare_notices_rec01 import load_exports,encoded,sha
from organization_semantics import BRIDGE,VERSION,classify_organization,classify_relation

def qualify(root,out):
    records,provenance,manifests=load_exports(root)
    out.mkdir(parents=True,exist_ok=False)
    sources={i:r['data'] for (k,i),r in records.items() if k=='source'}
    archives={}; csv_cache={}; changed=[]; inventory=[]; organizations={}
    def raw_record(data):
        source=sources[data['source_external_id']]; title=source['title']
        path=root/'runtime'/'sources'/(title if title.endswith(('.zip','.csv')) else title+'.zip')
        if title not in archives:
            assert sha(path.read_bytes())==source['byte_sha256'], 'source_hash_mismatch'
            archives[title]=zipfile.ZipFile(path) if path.suffix=='.zip' else None
        locator=data.get('source_locator','')
        if path.suffix=='.zip':
            member=locator.split('#',1)[0];fragment=locator.partition('#mandat=')[2]
            raw=json.loads(archives[title].read(member))
            if fragment:
                mandates=raw['acteur']['mandats']['mandat']
                if isinstance(mandates,dict):mandates=[mandates]
                return next(x for x in mandates if x['uid']==fragment)
            return next(iter(raw.values()))
        if title not in csv_cache:
            csv_cache[title]=list(csv.DictReader(l for l in path.read_bytes().decode('latin1').splitlines() if l and not l.startswith('%')))
        return csv_cache[title]
    def add(kind,identifier,data,original):
        record={**original,'data':data,'revision':'rec01-v2-'+sha(encoded(data))[:24], 'expects_revision':original['revision']}
        changed.append(record)
        inventory.append({'record_type':kind,'record_id':identifier,'before_revision':original['revision'],
                          'after_revision':record['revision'],'evidence':provenance[(kind,identifier)],'data':data})
    for (kind,identifier),original in sorted(records.items()):
        if kind!='organization':continue
        data=dict(original['data']); raw=raw_record(data)
        if identifier.startswith('DV:ORG:AN:'):
            assert raw['libelle']==data['name'];code=raw['codeType']
        elif identifier=='DV:INSTITUTION:CONGRES':
            assert raw['lieuVote']=='Congrès';code='AN_CONGRES_LIEUVOTE'
        elif identifier=='SENAT:INSTITUTION':code='ODSEN_INSTITUTION'
        else:
            group_code=identifier.removeprefix('SENAT:GROUP:')
            matches=[x for x in raw if x['Code du groupe politique']==group_code and x['Nom court du groupe politique']==data['name']]
            assert matches,'group_label_not_in_source'
            code='ODSEN_GROUP';data['raw_group_code']=group_code
            data['source_locator']='Code du groupe politique='+group_code+';Nom court du groupe politique='+data['name']
        data=classify_organization(data,code);organizations[identifier]=data
        add(kind,identifier,data,original)
    for (kind,identifier),original in sorted(records.items()):
        if kind!='affiliation':continue
        data=dict(original['data']);raw=raw_record(data)
        if identifier.startswith('SENAT:AFFILIATION:'):
            membership,function=identifier.removeprefix('SENAT:AFFILIATION:').split(':')
            matches=[x for x in raw if x['Id appartenance']==membership and x['Id fonction occupée']==function]
            assert len(matches)==1,'ambiguous_source_relationship'
            row=matches[0];assert row['Nom court fonction']==data['role']
            data['source_locator']='Id appartenance='+membership+';Id fonction occupée='+function
            data['raw_group_code']=row['Code du groupe politique'];code='ODSEN_GROUP_MEMBERSHIP'
        else:
            code=raw['typeOrgane'];assert raw['infosQualite']['libQualite']==data['role']
        data=classify_relation(data,organizations[data['organization_external_id']],code)
        add(kind,identifier,data,original)
    (out/'batches').mkdir()
    batches=[]
    for kind in ('organization','affiliation'):
        rows=[r for r in changed if r['record_type']==kind]
        for start in range(0,len(rows),1000):
            chunk=rows[start:start+1000];bid='REC01-v2-'+sha(encoded(chunk))[:24]
            payload={'bridge_schema':BRIDGE,'batch_id':bid,'idempotency_key':bid,'records':chunk}
            relative=f'batches/{kind}-{start:05d}.json';binary=encoded(payload);(out/relative).write_bytes(binary)
            batches.append({'file':relative,'type':kind,'records':len(chunk),'sha256':sha(binary)})
    counts=dict(Counter(r['record_type'] for r in changed))
    manifest={'schema':'dv-public-export-v1','semantics_version':VERSION,'counts':counts,'batches':batches,
              'input_manifests':manifests,'scope':'Classification documentaire complète ; aucun vote modifié.'}
    (out/'MANIFEST.json').write_bytes(encoded(manifest))
    (out/'inventory.jsonl').write_text(''.join(encoded(r).decode()+'\n' for r in inventory),encoding='utf-8')
    receipt={'counts':counts,'organizations_by_type':dict(Counter(o['type'] for o in organizations.values())),
             'relations_by_kind':dict(Counter(r['data']['relation_kind'] for r in changed if r['record_type']=='affiliation')),
             'votes_in_delta':0,'all_ids_preserved':all((r['record_type'],r['record_id']) in records for r in changed),
             'source_archives_verified':sorted(archives),'manifest_sha256':sha(encoded(manifest)),
             'inventory_sha256':sha((out/'inventory.jsonl').read_bytes())}
    (out/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    for archive in archives.values():
        if archive:archive.close()
    print(json.dumps(receipt,ensure_ascii=False))
    return receipt

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();qualify(a.root,a.output)
