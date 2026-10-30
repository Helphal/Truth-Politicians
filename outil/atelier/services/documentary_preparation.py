"""Versioned source inventory and counter-search, never an invented political label."""
from django.db import transaction
from django.core.exceptions import ValidationError
from atelier.models import ResearchPreparation,IndexedPassage,Declaration
from .document_index import search_passages,corpus_version
from .canonical import sha256_digest
from .thematic_integrity import thematic_preparation_snapshot

@transaction.atomic
def prepare_documentary_review(case,*,counter_queries):
    if not isinstance(counter_queries,list) or not 1<=len(counter_queries)<=5 or any(not isinstance(q,str) or not 3<=len(q)<=500 for q in counter_queries):
        raise ValidationError('counter_search_scope_invalid')
    case.refresh_from_db();version=corpus_version();sections=[];exclusions=[]
    for hit in case.candidate_evidence['passages']['results']:
        row=IndexedPassage.objects.select_related('document__source').filter(pk=hit['id']).first()
        if row is None or row.text_sha256!=hit['text_sha256'] or row.document.sha256!=hit['source_version']['sha256'] or row.document.quarantined or row.document.source.access_status not in {'active','qualified'} or row.document.source.rights_status=='forbidden':
            exclusions.append({'passage_id':hit['id'],'reason':'original_passage_or_authority_changed'});continue
        sections.append({'role':'context_to_attribute','evidence':hit,'inclusion_reason':'Correspondance lexicale dans un passage épinglé ; auteur/portée non déduits.','admissible_comparison':False})
    inquiry=case.candidate_evidence['declarations_and_votes']
    for candidate in inquiry.get('candidates',[]):
        declaration=Declaration.objects.select_related('source_passage__document__source').filter(stable_id=candidate['declaration_id']).first()
        if declaration is None:
            exclusions.append({'declaration_id':candidate['declaration_id'],'reason':'declaration_missing'});continue
        passage=declaration.source_passage;doc=passage.document
        if declaration.exact_quote!=candidate['exact_quote'] or candidate['exact_quote'] not in passage.exact_text or doc.sha256!=candidate['source_version']['sha256'] or doc.quarantined or doc.source.access_status not in {'active','qualified'} or doc.source.rights_status=='forbidden':
            exclusions.append({'declaration_id':candidate['declaration_id'],'reason':'declaration_or_source_changed'});continue
        thematic=thematic_preparation_snapshot(declaration)
        if thematic['stale_qualified_links']:
            exclusions.append({'declaration_id':candidate['declaration_id'],'reason':'thematic_fingerprint_stale','stale_count':len(thematic['stale_qualified_links'])})
        sections.append({'role':'declaration','evidence':candidate,'inclusion_reason':'Citation déjà attribuée et contrôlée ; rapprochement non établi.','admissible_comparison':False,'thematic':thematic})
    counter=[]
    for query in counter_queries:
        result=search_passages(query=query,limit=25);result['more_available']=bool(result.pop('next_cursor'))
        counter.append({'role':'potential_counterevidence','search':result,'assessment':'Résultats à examiner ; ni réfutation prouvée ni absence de contre-élément si vide.'})
    if corpus_version()!=version:raise ValidationError('preparation_corpus_changed_retry')
    payload={'format':'dv.documentary-preparation/1','case_id':case.pk,'raw_query':case.query,'original_input_digest':case.input_digest,'original_corpus_version':case.corpus_version,'current_corpus_version':version,
             'original_scope':{k:v for k,v in inquiry.items() if k not in {'candidates','exclusions'}},'sections':sections,'exclusions':exclusions+inquiry.get('exclusions',[]),'counter_searches':counter,'state':'research_only',
             'comparability_established':False,'formal_comparison_created':False,'human_review_created':False,'political_label':None,'blocking_reason':'Acte exact, mandat/attribution à la date et contre-éléments restent à qualifier ; correspondance lexicale insuffisante. Une empreinte thématique périmée n’établit pas de conclusion.',
             'coverage':'Sources indexées locales, recherches bornées, pas tout le web ni corpus exhaustif.'}
    digest=sha256_digest(payload)
    return ResearchPreparation.objects.get_or_create(preparation_digest=digest,defaults={'case':case,'payload':payload})
