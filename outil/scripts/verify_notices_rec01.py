"""Verify all prepared claims against exact frozen export records, without publishing."""
import argparse,hashlib,json
from pathlib import Path

def verify(root,folder,reference_index=None):
    if (folder/'MANIFEST.json').exists() and json.loads((folder/'MANIFEST.json').read_bytes()).get('schema') in ('dv-notice-bundle/2','dv-notice-bundle/3'):
        from notices_v2 import verify as verify_v2
        result=verify_v2(root,folder);print(json.dumps(result));return result
    # Legacy control: compare the entire draft with the immutable received
    # inventory. Passing integrity never makes its old semantics importable.
    if reference_index is None:
        raise ValueError('legacy_notice_reference_index_required')
    index=json.loads(Path(reference_index).read_bytes())
    seen=set()
    cache={};count=0;drafts=0;missing=set();sources=set();errors=[]
    for line in (folder/'notices.jsonl').open(encoding='utf-8'):
        draft=json.loads(line);drafts+=1
        identity=draft['record_type']+':'+draft['record_id']
        actual=hashlib.sha256(json.dumps(draft,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if identity in seen or index['notices'].get(identity)!=actual:errors.append('legacy_text_or_provenance_mismatch:'+identity)
        seen.add(identity)
        assert draft['published'] is False and draft['political_analysis_authorized'] is False
        for claim in draft['claims']:
            assert claim['proofs']
            for proof in claim['proofs']:
                key=proof['batch_file'];path=(root/key).resolve();assert path.is_relative_to(root)
                if key not in cache:
                    raw=path.read_bytes();assert hashlib.sha256(raw).hexdigest()==proof['batch_sha256'];cache[key]=json.loads(raw)['records']
                record=cache[key][proof['record_index']]
                encoded=json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
                assert hashlib.sha256(encoded).hexdigest()==proof['record_sha256']
                assert record['record_id']==proof['record_id'] and record['data']==proof['extracted_facts'] and record['revision']==proof['record_revision']
                assert len(proof['source_sha256'])==64 and proof['source_url'].startswith('https://')
                sources.add((proof['source_id'],proof['source_sha256']))
                if not proof['source_locator']:missing.add(proof['record_id'])
                count+=1
    result={'drafts':drafts,'claim_proof_references_checked':count,'exact_batches_checked':len(cache),'source_digests':sorted(sources),
      'records_without_original_locator':len(missing),'missing_original_locator_ids':sorted(missing),'errors':errors,
      'scope':'Exact exported facts and provenance, not an independent political review or complete biographies.',
      'passed':seen==set(index['notices']) and not errors,
      'admissible_for_import':False,'legacy_semantics':'rejected_for_import_use_v2'}
    (folder/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ['missing_original_locator_ids','source_digests']}))
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--folder',type=Path,required=True);p.add_argument('--reference-index',type=Path);a=p.parse_args();verify(a.root.resolve(),a.folder.resolve(),a.reference_index)
