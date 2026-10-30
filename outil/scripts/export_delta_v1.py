"""Revision-checked delta between pinned public exports; omission never withdraws."""
import argparse,collections,hashlib,json
from pathlib import Path
from export_public_v1 import canonical

def records(path,kinds=None):
    root=path.resolve().parent;manifest=json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('schema')!='dv-public-export-v1':raise ValueError('manifest_schema')
    result={}
    for entry in manifest['batches']:
        if kinds and entry['type'] not in kinds:continue
        relative=Path(entry['file']);target=(root/relative).resolve()
        if relative.is_absolute() or '..' in relative.parts or not target.is_relative_to(root) or relative.suffix!='.json' or target.suffix!='.json':raise ValueError('manifest_path_escape')
        raw_path=root/relative
        if any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in [raw_path,*raw_path.parents] if p!=root.parent):raise ValueError('manifest_link_refused')
        raw=target.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise ValueError('batch_hash_mismatch')
        for record in json.loads(raw)['records']:
            key=(record['record_type'],record['record_id'])
            if key in result:raise ValueError('duplicate_record_identity')
            result[key]=record
    return result,manifest

def main():
    p=argparse.ArgumentParser();p.add_argument('--before',type=Path,required=True);p.add_argument('--after',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.exists():raise ValueError('fresh_output_required')
    after,manifest=records(a.after);before,_=records(a.before,{k[0] for k in after})
    changed=[]
    for key,record in after.items():
        old=before.get(key)
        if old and canonical(old['data'])==canonical(record['data']):continue
        if old:
            if old['revision']==record['revision']:raise ValueError('changed_content_same_revision')
            record=dict(record,expects_revision=old['revision'])
        changed.append(record)
    a.output.mkdir(parents=True);(a.output/'batches').mkdir();entries=[]
    digest=hashlib.sha256(a.after.read_bytes()).hexdigest()
    for start in range(0,len(changed),500):
        part=changed[start:start+500];identifier=f'DV-DELTA-{digest[:20]}-{start//500+1}'
        batch={'bridge_schema':'dv-site-bridge/0.1.0-draft.1','batch_id':identifier,'idempotency_key':identifier,
               'generated_at':manifest['created_at'],'records':part}
        raw=canonical(batch).encode('utf-8');filename=f'batches/{start//500+1:04d}.json';(a.output/filename).write_bytes(raw)
        entries.append({'file':filename,'sha256':hashlib.sha256(raw).hexdigest(),'type':'delta','records':len(part)})
    receipt={'schema':'dv-public-export-v1','is_delta':True,'baseline_manifest_sha256':hashlib.sha256(a.before.read_bytes()).hexdigest(),
             'after_manifest_sha256':digest,'source_receipts':manifest['source_receipts'],'batches':entries,
             'counts':dict(collections.Counter(r['record_type'] for r in changed)),
             'omitted_baseline_records_not_withdrawn':len(before.keys()-after.keys()),'automatic_withdrawals':0}
    (a.output/'MANIFEST.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in receipt.items() if k not in ('batches','source_receipts')}))
if __name__=='__main__':main()
