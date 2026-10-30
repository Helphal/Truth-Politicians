"""Private draft.2 publication feed. No contacts, mail, pixels or send authority.

Exclusions are persistent observations, not a claim of remote deletion. Reappearance
requires a strictly newer revision and its own valid publication confirmation.
"""
from pathlib import Path
from django.conf import settings
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from atelier.models import FeedExclusion,SitePublicationConfirmation,OutboxEvent
from .editorial_feed import candidate_edition,verify_confirmation
from .publication import evaluate_revision
from .canonical import sha256_digest


def exclusion_reason(confirmation):
    event=confirmation.event
    try:verify_confirmation(event,confirmation.statement,confirmation.signature)
    except ValidationError:return 'confirmation_invalid'
    if confirmation.statement_digest!=sha256_digest(confirmation.statement):return 'confirmation_digest_mismatch'
    if not evaluate_revision(event.snapshot.revision,'test').allowed:return 'authority_no_longer_current'
    if OutboxEvent.objects.filter(dossier_id=event.dossier_id,environment=event.environment,operation='suspend',sequence__gt=event.sequence).exists():return 'later_withdrawal'
    if event.last_error_code.startswith(('invalidated','superseded')):return 'event_invalidated'
    return None


@transaction.atomic
def confirmed_feed(*,period_start,period_end):
    if timezone.is_naive(period_start) or timezone.is_naive(period_end) or period_end<=period_start:
        raise ValidationError('edition_period_invalid')
    database=Path(settings.DATABASES['default']['NAME'])
    restored=any((path/'.dv-restored-unreconciled').exists() for path in (Path(settings.RUNTIME_DIR),database.parent,database.parent.parent))
    confirmations=list(SitePublicationConfirmation.objects.select_related('event__snapshot__revision__dossier','event__dossier').order_by('pk'))
    if not restored:
        for confirmation in confirmations:
            reason=exclusion_reason(confirmation)
            if reason:
                FeedExclusion.objects.get_or_create(event=confirmation.event,defaults={'reason':reason,'content_revision':confirmation.event.content_revision})
    exclusions=list(FeedExclusion.objects.select_related('event__dossier').order_by('event__dossier__stable_id','content_revision','pk'))
    ceiling={}
    for row in exclusions:
        key=row.event.dossier.stable_id;ceiling[key]=max(ceiling.get(key,0),row.content_revision)
    edition=candidate_edition(period_start=period_start,period_end=period_end)
    items=[]
    for item in ([] if restored else edition['items']):
        if item['published_revision']<=ceiling.get(item['record_id'],0):continue
        # Normalize timestamps before sorting; offsets must not change chronology.
        item={**item,'confirmed_at':parse_datetime(item['confirmed_at']).astimezone(__import__('datetime').timezone.utc).isoformat(),'state':'published','withdrawal':None}
        items.append(item)
    items.sort(key=lambda item:(item['confirmed_at'],item['record_id'],item['published_revision']))
    removals=[{'record_id':row.event.dossier.stable_id,'revision':row.content_revision,'state':'excluded','reason':row.reason,'remote_withdrawal_confirmed':False} for row in exclusions]
    result={'format':'dv.confirmed-feed/0.1.0-draft.2','environment':'test','period_start':period_start.isoformat(),'period_end':period_end.isoformat(),
            'items':items,'exclusions':removals,'content_available':bool(items),'sendable':False,'reconciliation_required':restored,'site_adoption_required':True,
            'scope':'synthetic confirmed publication only; no subscriber data; exclusions are not remote deletion receipts'}
    return {**result,'feed_digest':sha256_digest(result)}
