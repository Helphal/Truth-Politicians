"""Rebuildable private HTML passage index, pinned corpus cursors, never review evidence alone."""
import hashlib
from html.parser import HTMLParser
import re
import unicodedata
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from atelier.models import IndexedPassage,DocumentVersion
from .canonical import sha256_digest
from .catalogue import source_ref

VERSION='DV-HTML-PASSAGES-1'

def normalize(text):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD',text.casefold()) if not unicodedata.combining(c)).split())

class Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocked=[];self.active=False;self.parts=[];self.rows=[];self.ordinal=0
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style','nav','footer','header','aside','noscript'}:self.blocked.append(tag)
        if tag=='p':
            self._finish();self.ordinal+=1;self.active=not self.blocked;self.parts=[]
        if tag=='br' and self.active:self.parts.append(' ')
    def handle_endtag(self,tag):
        if tag=='p':self._finish()
        if self.blocked and tag==self.blocked[-1]:self.blocked.pop()
    def handle_data(self,data):
        if self.active and not self.blocked:self.parts.append(data)
    def _finish(self):
        if self.active:
            text=' '.join(''.join(self.parts).split())
            if 30<=len(text)<=12000:self.rows.append((self.ordinal,text))
        self.active=False;self.parts=[]

@transaction.atomic
def index_html(document,path):
    if document.quarantined or document.source.rights_status=='forbidden':raise ValidationError('index_source_unavailable')
    if path.stat().st_size>10*1024*1024:raise ValidationError('index_document_too_large')
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=document.sha256:raise ValidationError('index_original_mismatch')
    parser=Paragraphs();parser.feed(data.decode('utf-8-sig',errors='strict'));parser._finish()
    if len(parser.rows)>20000:raise ValidationError('index_passage_limit')
    added=0
    for ordinal,text in parser.rows:
        defaults={'locator':f'HTML p ordinal {ordinal}; {VERSION}; espaces normalisés',
            'visible_text':text,'search_text':normalize(text),'text_sha256':hashlib.sha256(text.encode()).hexdigest()}
        row,created=IndexedPassage.objects.get_or_create(document=document,extractor_version=VERSION,ordinal=ordinal,defaults=defaults)
        if not created and any(getattr(row,k)!=v for k,v in defaults.items()):raise ValidationError('index_version_conflict')
        added+=int(created)
    return {'source_id':document.source.stable_id,'source_sha256':document.sha256,'passages':len(parser.rows),'added':added,'extractor_version':VERSION}

def corpus_version():
    # Includes source availability, passage bytes and IDs. Any addition/change refuses an old cursor.
    rows=IndexedPassage.objects.order_by('pk').values_list('pk','document__sha256','extractor_version','ordinal','text_sha256','document__quarantined','document__source__rights_status','document__source__access_status')
    digest=hashlib.sha256()
    for row in rows.iterator(chunk_size=500):digest.update(sha256_digest(list(row)).encode())
    return digest.hexdigest()

def search_passages(*,query,limit=20,cursor=''):
    if not isinstance(query,str) or not 3<=len(query.strip())<=500 or type(limit) is not int or not 1<=limit<=25:raise ValidationError('passage_query_invalid')
    words=sorted(set(re.findall(r'[a-z0-9]{3,}',normalize(query))))
    if not words:raise ValidationError('passage_query_empty')
    version=corpus_version();query_hash=sha256_digest({'query':query,'limit':limit});last=0
    if cursor:
        try:decoded=signing.loads(cursor,salt=VERSION,max_age=86400)
        except signing.BadSignature as exc:raise ValidationError('passage_cursor_invalid') from exc
        if decoded.get('corpus')!=version:raise ValidationError('passage_corpus_changed_restart')
        if decoded.get('query')!=query_hash:raise ValidationError('passage_cursor_query_mismatch')
        last=decoded.get('last')
        if type(last) is not int or last<0:raise ValidationError('passage_cursor_invalid')
    rows=IndexedPassage.objects.filter(document__quarantined=False,document__source__access_status__in=['active','qualified']).exclude(document__source__rights_status='forbidden').filter(pk__gt=last)
    for word in words:rows=rows.filter(search_text__contains=word)
    found=list(rows.select_related('document__source').order_by('pk')[:limit+1]);more=len(found)>limit;found=found[:limit]
    if corpus_version()!=version:raise ValidationError('passage_corpus_changed_restart')
    next_cursor=signing.dumps({'corpus':version,'query':query_hash,'last':found[-1].pk},salt=VERSION,compress=True) if more else None
    return {'query':query,'corpus_version':version,'extractor_version':VERSION,'next_cursor':next_cursor,
        'matching_logic':'all normalized query terms; excerpts indexed, not political conclusions',
        'coverage':'Indexed HTML paragraphs only; PDF, unindexed sources and official corpus not exhaustive.',
        'results':[{'id':r.pk,'text':r.visible_text,'text_sha256':r.text_sha256,'locator':r.locator,
            'source_version':source_ref(r.document)} for r in found], 'human_review_created':False}
