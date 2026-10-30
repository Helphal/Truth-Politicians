"""Construct and reproduce the complete, short, typed notice inventory."""
import argparse,json
from collections import defaultdict,Counter
from pathlib import Path
from prepare_notices_rec01 import load_exports,encoded,sha
from documentary_rendering import render_notice,notice_id,digest
from organization_semantics import BRIDGE

def load_delta(root,manifest,records,provenance):
    path=(root/manifest).resolve();assert path.is_relative_to(root.resolve())
    raw=path.read_bytes();index=json.loads(raw)
    for entry in index['batches']:
        batch=(path.parent/entry['file']).resolve();assert batch.is_relative_to(path.parent)
        binary=batch.read_bytes();assert sha(binary)==entry['sha256']
        rows=json.loads(binary)['records'];assert len(rows)==entry['records']
        for i,row in enumerate(rows):
            key=(row['record_type'],row['record_id']);records[key]=row
            provenance[key]={'batch_file':batch.relative_to(root).as_posix(),'batch_sha256':sha(binary),
              'record_index':i,'record_id':row['record_id'],'record_revision':row['revision'],'record_sha256':sha(encoded(row))}
    return {'file':path.relative_to(root).as_posix(),'sha256':sha(raw)}

def build(records,provenance,version=2):
    if version not in (2,3):raise ValueError('notice_version_invalid')
    mandates=defaultdict(list)
    for (kind,rid),row in records.items():
        if kind=='mandate':mandates[row['data']['person_external_id']].append(row)
    def fact(kind,rid):
        row=records[kind,rid]
        return {'kind':kind,'id':rid,'revision':row['revision'],'data':row['data'],'provenance':provenance[kind,rid]}
    output=[]
    for (kind,rid),row in sorted(records.items()):
        if kind not in ('person','organization'):continue
        facts=[fact(kind,rid)]
        if kind=='organization' and not row['data'].get('qualification'):raise ValueError('organization_not_qualified')
        if kind=='person' and mandates[rid]:
            selected=max(mandates[rid],key=lambda r:(r['data'].get('current_attested_on') or r['data'].get('starts_on') or '',r['record_id']))
            oid=selected['data']['institution_external_id']
            if ('organization',oid) in records:facts.extend([fact('mandate',selected['record_id']),fact('organization',oid)])
        sids=sorted({f['data']['source_external_id'] for f in facts})
        sources=[fact('source',sid) for sid in sids]
        data={'schema':f'dv-documentary-notice/{version}','subject_type':kind,'subject_id':rid,
              kind+'_external_id':rid,'facts':facts,'sources':sources,'source_external_ids':sids,
              'text':render_notice(kind,facts,version=version),'qualification':'technical_documentary_concordance',
              'human_editorial_review_claimed':False,'political_analysis_authorized':False}
        output.append({'record_type':'notice','record_id':notice_id(kind,rid),'revision':f'notice-v{version}-'+digest(data)[:24],
            'operation':'upsert','publication_circuit':'factual','is_test':False,'data':data})
        if version == 3:
            previous={**data,'schema':'dv-documentary-notice/2','text':render_notice(kind,facts,version=2)}
            output[-1]['expects_revision']='notice-v2-'+digest(previous)[:24]
    return output

def prepare(root,delta,out,version=2):
    records,provenance,manifests=load_exports(root)
    delta_entry=load_delta(root,delta,records,provenance)
    rows=build(records,provenance,version=version);out.mkdir(parents=True,exist_ok=False)
    (out/'notices.jsonl').write_text(''.join(encoded(r).decode()+'\n' for r in rows),encoding='utf-8')
    descriptor={'schema':f'dv-notice-bundle/{version}','bridge_schema':BRIDGE,'delta_manifest':delta_entry,
        'input_manifests':manifests,'notices_sha256':sha((out/'notices.jsonl').read_bytes()),
        'inventory':{k:sum(r['record_type']==k for r in records.values()) for k in ('person','organization')},
        'records':len(rows)}
    (out/'MANIFEST.json').write_bytes(encoded(descriptor))
    print(json.dumps({'records':len(rows),'input_inventory':descriptor['inventory'],'notices_sha256':descriptor['notices_sha256']}))

def verify(root,folder):
    descriptor=json.loads((folder/'MANIFEST.json').read_bytes())
    if descriptor['schema'] not in ('dv-notice-bundle/2','dv-notice-bundle/3'):raise ValueError('notice_bundle_schema')
    version=int(descriptor['schema'].rsplit('/',1)[1])
    records,prov,manifests=load_exports(root)
    def normalized_paths(value):
        if isinstance(value,list):return [normalized_paths(x) for x in value]
        if isinstance(value,dict):return {k:(v.replace('\\','/') if k in ('file','batch_file') and isinstance(v,str) else normalized_paths(v)) for k,v in value.items()}
        return value
    if normalized_paths(manifests)!=normalized_paths(descriptor['input_manifests']):raise ValueError('input_manifest_mismatch')
    delta=load_delta(root,descriptor['delta_manifest']['file'],records,prov)
    if delta!=descriptor['delta_manifest']:raise ValueError('delta_manifest_mismatch')
    raw=(folder/'notices.jsonl').read_bytes()
    actual=[json.loads(line) for line in raw.decode().splitlines()]
    expected=build(records,prov,version=version)
    errors=[]
    def portable(rows):
        result=[]
        for row in rows:
            if row['revision']!=f'notice-v{version}-'+digest(row['data'])[:24]:raise ValueError('notice_revision_hash_mismatch')
            normalized=normalized_paths(row);normalized['revision']=f'notice-v{version}-'+digest(normalized['data'])[:24];result.append(normalized)
        return result
    if portable(actual)!=portable(expected):errors.append('typed_facts_text_or_provenance_mismatch')
    if sha(raw)!=descriptor['notices_sha256']:errors.append('notice_file_hash_mismatch')
    if len(expected)!=descriptor['records']:errors.append('inventory_mismatch')
    result={'passed':not errors,'admissible_for_import':not errors,'records':len(actual),'expected_from_source_inventory':len(expected),
            'errors':errors,'scope':'Reproduction des faits typés, du texte et de toute la provenance ; aucune revue politique.'}
    (folder/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--delta',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--version',type=int,choices=(2,3),default=2)
    a=p.parse_args();prepare(a.root,a.delta,a.output,version=a.version);print(json.dumps(verify(a.root,a.output)))
