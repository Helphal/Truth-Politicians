"""Candidate only: confirmed Site publication is NOT an 'applied' transport receipt.

Only synthetic test confirmations can be accepted until Site adoption. No sender,
subscriber storage or production endpoint is supplied.
"""
import base64,binascii
from datetime import timedelta
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from atelier.models import SitePublicationConfirmation
from .canonical import canonical_bytes,sha256_digest
from .publication import evaluate_revision,_validate_public_url

FORMAT='dv.site-publication-confirmation/0.1.0-draft.1'
DOMAIN=b'DV-SITE-PUBLISHED-ED25519-V1\n'
FIELDS={'format','event_id','event_digest','record_id','revision','environment','destination','status','published_at','public_url','change_kind','key_id'}

def verify_confirmation(event,statement,signature):
    if not isinstance(statement,dict) or set(statement)!=FIELDS:raise ValidationError('site_confirmation_shape_invalid')
    if event.environment!='test' or event.dossier.data_scope!='synthetic':raise ValidationError('site_confirmation_real_adapter_not_adopted')
    expected={'format':FORMAT,'event_id':str(event.event_id),'event_digest':event.payload_digest,
        'record_id':event.dossier.stable_id,'revision':event.content_revision,'environment':'test','destination':'dv-site:test','status':'published'}
    if any(statement[k]!=v for k,v in expected.items()) or type(statement['revision']) is not int:
        raise ValidationError('site_confirmation_binding_invalid')
    if event.operation!='upsert' or not event.snapshot_id or sha256_digest(event.payload)!=event.payload_digest:
        raise ValidationError('site_confirmation_event_invalid')
    try:stamp=parse_datetime(statement['published_at']) if isinstance(statement['published_at'],str) else None
    except ValueError as exc:raise ValidationError('site_confirmation_date_invalid') from exc
    if not stamp or timezone.is_naive(stamp) or stamp<event.created_at or stamp>timezone.now()+timedelta(seconds=30):raise ValidationError('site_confirmation_date_invalid')
    if statement['change_kind'] not in {'initial','correction'}:raise ValidationError('site_confirmation_change_invalid')
    _validate_public_url(statement['public_url'])
    keys=getattr(settings,'DV_SITE_PUBLICATION_PUBLIC_KEYS',{})
    key_id=statement['key_id']
    if not isinstance(key_id,str) or not key_id.startswith('test-') or key_id not in keys:raise ValidationError('site_confirmation_key_unknown')
    try:
        key=Ed25519PublicKey.from_public_bytes(base64.b64decode(keys[key_id],validate=True))
        key.verify(base64.b64decode(signature,validate=True),DOMAIN+canonical_bytes(statement))
    except (ValueError,TypeError,binascii.Error,InvalidSignature) as exc:raise ValidationError('site_confirmation_signature_invalid') from exc

@transaction.atomic
def accept_confirmation(event,statement,signature):
    event.refresh_from_db();verify_confirmation(event,statement,signature)
    if not evaluate_revision(event.snapshot.revision,'test').allowed:raise ValidationError('site_confirmation_content_not_current')
    obj,_=SitePublicationConfirmation.objects.get_or_create(event=event,defaults={'statement':statement,'signature':signature,'statement_digest':sha256_digest(statement)})
    if obj.statement!=statement or obj.signature!=signature:raise ValidationError('site_confirmation_conflict')
    return obj

def candidate_edition(*,period_start,period_end):
    """Requery/reverify immediately before generating; no reusable send authority."""
    if timezone.is_naive(period_start) or timezone.is_naive(period_end) or period_end<=period_start:raise ValidationError('edition_period_invalid')
    items=[];seen=set()
    for confirmation in SitePublicationConfirmation.objects.select_related('event__snapshot__revision__dossier','event__dossier').order_by('pk'):
        event=confirmation.event;s=confirmation.statement
        try:verify_confirmation(event,s,confirmation.signature)
        except ValidationError:continue
        if confirmation.statement_digest!=sha256_digest(s) or not period_start<=parse_datetime(s['published_at'])<period_end:continue
        if not evaluate_revision(event.snapshot.revision,'test').allowed:continue
        key=(s['record_id'],s['revision'])
        if key in seen:continue
        seen.add(key);p=event.snapshot.revision.public_payload
        items.append({'record_id':s['record_id'],'published_revision':s['revision'],'confirmed_at':s['published_at'],
            'change_kind':s['change_kind'],'public_url':s['public_url'],'title':p.get('title',p.get('official_title','')),
            'summary':p.get('short_answer',''),'main_caveat':p.get('main_caveat',p.get('coverage',{}).get('statement','')),
            'references':event.snapshot.revision.source_snapshot})
    items.sort(key=lambda i:(i['confirmed_at'],i['record_id'],i['published_revision']))
    result={'format':'dv.editorial-feed-candidate/0.1.0-draft.1','environment':'test','period_start':period_start.isoformat(),
        'period_end':period_end.isoformat(),'selection_policy':'all-current-confirmed-in-period-candidate-not-approved',
        'items':items,'content_available':bool(items),'sendable':False,'site_adoption_required':True,
        'limitation':'Simulated confirmations only; no actual WordPress publication or email.'}
    return {**result,'edition_digest':sha256_digest(result)}
