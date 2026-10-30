"""Explicit institutional reference chains only. Adopted result != submitted input."""
import argparse,json,re,hashlib
from collections import defaultdict,Counter
from pathlib import Path
from prepare_notices_rec01 import load_exports,encoded,sha
from export_public_v1 import archive_items,sequence
from export_senate_public_v1 import copy_rows
from organization_semantics import BRIDGE,normalized

def subject_kind(title):
    title=normalized(title)
    if 'amendement' in title:return 'amendment'
    if 'motion' in title:return 'motion'
    if "l'ensemble" in title or 'ensemble du' in title:return 'whole_text'
    if re.search(r"(?:sur |^)(?:l['’])?article",title):return 'article'
    return 'other_or_unknown'

def adopted_candidate(subject,rid,chain,known_versions):
    """Admit only an explicit same-chamber whole-text adoption result."""
    if subject!='whole_text' or rid!='DV:SCRUTIN:AN:'+str(chain.get('vote_ref')):return None
    code=str(chain.get('act_code',''))
    if not code.startswith('AN') or not code.endswith('-DEBATS-DEC'):return None
    conclusion=normalized(str((chain.get('conclusion') or {}).get('libelle','')))
    if 'adopt' not in conclusion or 'non adopt' in conclusion or 'pas adopt' in conclusion:return None
    bta=[t.get('refTexteAssocie') for t in chain.get('texts',[]) if t.get('typeTexte')=='BTA']
    if len(bta)!=1 or not isinstance(bta[0],str) or 'ANR5' not in bta[0]:return None
    version='DV:VERSION:AN:'+bta[0]
    return version if version in known_versions else None

def link(root,out):
    records,prov,manifests=load_exports(root);sources={i:r['data'] for (k,i),r in records.items() if k=='source'}
    source_by_title={v['title']:i for i,v in sources.items()};chains=defaultdict(list);errors=[]
    source_hashes={};an_raw_refs=Counter()
    def verified(name):
        path=root/'runtime/sources'/(name if name.endswith('.zip') else name+'.zip')
        sid=source_by_title[name];actual=hashlib.sha256(path.read_bytes()).hexdigest()
        if actual!=sources[sid]['byte_sha256']:raise ValueError('source_hash_mismatch')
        source_hashes[name]=actual;return path,sid
    def walk(node,path,sid):
        if isinstance(node,dict):
            refs=(node.get('voteRefs') or {}).get('voteRef',[]) if isinstance(node.get('voteRefs'),dict) else []
            texts=sequence((node.get('textesAssocies') or {}).get('texteAssocie'))
            for ref in sequence(refs):
                if not isinstance(ref,str):continue
                key='DV:SCRUTIN:AN:'+ref
                if ('scrutin',key) not in records:continue
                chains[key].append({'source_external_id':sid,'locator':path,'act_id':node.get('uid'),
                  'act_code':node.get('codeActe'),'vote_ref':ref,'texts':texts,'conclusion':node.get('statutConclusion')})
            for k,v in node.items():walk(v,path+'/'+k,sid)
        elif isinstance(node,list):
            for i,v in enumerate(node):walk(v,path+'/'+str(i),sid)
    for name in ('AN-DOSSIERS-16','AN-DOSSIERS-17'):
        path,sid=verified(name)
        for loc,kind,row in archive_items(path):
            if kind=='dossierParlementaire':walk(row,loc,sid)
    for name in ('AN-SCRUTINS-16','AN-SCRUTINS-17'):
        path,sid=verified(name)
        for _,kind,row in archive_items(path):
            if kind=='scrutin':an_raw_refs['non_null' if (row.get('objet') or {}).get('referenceLegislative') else 'null']+=1
    senate_path,senate_sid=verified('SENAT-dosleg.zip');tables=defaultdict(dict)
    for table,row in copy_rows(senate_path,{'scr','date_seance','lecture','lecass','texte'}):
        row={k:v.strip() if isinstance(v,str) else v for k,v in row.items()}
        key={'scr':lambda:(row['sesann'],row['scrnum']),'date_seance':lambda:row['code'],'lecture':lambda:row['lecidt'],
             'lecass':lambda:row['lecassidt'],'texte':lambda:row['texcod']}[table]()
        tables[table][key]=row
    lecture_texts=defaultdict(set)
    for row in tables['texte'].values():
        lecture=tables['lecass'].get(row['lecassidt'],{}).get('lecidt')
        if lecture:lecture_texts[lecture].add(row['texcod'])
    changed=[];inventory=[];statuses=Counter();kinds=Counter()
    known_versions={i for (k,i) in records if k=='text_version'}
    for (kind,rid),original in sorted(records.items()):
        if kind!='scrutin':continue
        data=dict(original['data']);subject=subject_kind(data.get('official_title',''));kinds[subject]+=1
        candidates=[];evidence=chains[rid];status='dossier_only' if data.get('dossier_external_id') else 'insufficient_source'
        for chain in evidence:
            # Same decision explicitly names its ballot and one final adopted text.
            # Never apply this to an amendment, article, motion or another chamber.
            version=adopted_candidate(subject,rid,chain,known_versions)
            if version:candidates.append((version,chain))
        unique={v for v,_ in candidates}
        if len(unique)==1:
            version,chain=candidates[0];status='exact_adopted_result'
            data.update(text_version_external_id=version,text_version_relation='adopted_result',
                        link_source_external_id=chain['source_external_id'])
        elif len(unique)>1:status='ambiguous_explicit_chain'
        if rid.startswith('SENAT:SCRUTIN:'):
            session,number=rid.removeprefix('SENAT:SCRUTIN:').split(':')
            scr=tables['scr'].get((session,number),{});seance=tables['date_seance'].get(scr.get('code'),{})
            lecture=seance.get('lecidt');count=len(lecture_texts.get(lecture,()))
            evidence=[{'source_external_id':senate_sid,'locator':'scr:'+session+':'+number,'seance_code':scr.get('code'),
                       'lecture_id':lecture,'lecture_text_candidates':count,
                       'limit':'La clé relie une séance et une lecture, pas la version soumise à ce scrutin ; aucun choix par titre/date.'}]
        data.update(vote_subject_kind=subject,text_link_status=status,text_link_evidence=evidence)
        changed.append({**original,'data':data,'expects_revision':original['revision'],'revision':'rec01-links-'+sha(encoded(data))[:24]})
        inventory.append({'id':rid,'subject_kind':subject,'status':status,'exact_version':data.get('text_version_external_id'),
            'relation':data.get('text_version_relation'),'chains':evidence});statuses[status]+=1
    out.mkdir(parents=True,exist_ok=False);(out/'batches').mkdir();batches=[]
    for start in range(0,len(changed),1000):
        chunk=changed[start:start+1000];bid='REC01-LINKS-'+sha(encoded(chunk))[:24]
        binary=encoded({'bridge_schema':BRIDGE,'batch_id':bid,'idempotency_key':bid,'records':chunk})
        relative=f'batches/scrutin-{start:05d}.json';(out/relative).write_bytes(binary)
        batches.append({'file':relative,'type':'scrutin','records':len(chunk),'sha256':sha(binary)})
    manifest={'schema':'dv-public-export-v1','counts':{'scrutin':len(changed)},'batches':batches,'input_manifests':manifests,'source_hashes':source_hashes}
    (out/'MANIFEST.json').write_bytes(encoded(manifest));(out/'inventory.jsonl').write_text(''.join(encoded(r).decode()+'\n' for r in inventory),encoding='utf-8')
    result={'total_scrutins':len(changed),'statuses':dict(statuses),'subject_kinds':dict(kinds),'technical_errors':errors,
            'an_raw_reference_legislative':dict(an_raw_refs),'exact_input_version_claimed':0,
            'rule':'Explicit decision→voteRef→unique BTA, whole-text adoption, same chamber; adopted result only, never input version.',
            'votes_modified':0,'manifest_sha256':sha(encoded(manifest))}
    (out/'receipt.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();link(a.root,a.output)
