"""Bounded, resumable official archive indexing. Never creates a fake text or authority."""
from collections import Counter
from datetime import date
import hashlib
import json
import re
import time
import zipfile
from django.core.exceptions import ValidationError
from django.db import transaction
from atelier.models import ArchiveScan, StagedScrutiny, Scrutiny
from .collector import inspect_zip
from .parsers.assembly import normalize_scrutiny, iter_vote_observations

VERSION = "DV-AN-STAGING-1"

def pinned_members(document, path, *, max_files=20000):
    if document.quarantined or document.source.rights_status == "forbidden":
        raise ValidationError("archive_source_unavailable")
    with path.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    if digest != document.sha256:
        raise ValidationError("archive_digest_mismatch")
    members = sorted((m for m in inspect_zip(path, max_files=max_files) if not m.is_dir() and m.filename.endswith('.json')), key=lambda m:m.filename)
    if len({m.filename for m in members}) != len(members):
        raise ValidationError("archive_duplicate_member")
    return members

def stage_archive(document, path, *, batch_size=250, max_seconds=30, stop_requested=lambda:False):
    if type(batch_size) is not int or not 1 <= batch_size <= 1000 or not 0 < max_seconds <= 60:
        raise ValidationError("archive_batch_bounds_invalid")
    started = time.monotonic()
    members = pinned_members(document, path)
    added, stopped = 0, False
    # One bounded transaction serializes cursor and rows. A failed batch rolls back both.
    with transaction.atomic():
        scan, _ = ArchiveScan.objects.get_or_create(document=document, defaults={"parser_version":VERSION,"total_members":len(members)})
        scan = ArchiveScan.objects.select_for_update().get(pk=scan.pk)
        if scan.parser_version != VERSION or scan.total_members != len(members):
            raise ValidationError("archive_cursor_version_mismatch")
        start_cursor = scan.cursor
        legacy = list(Scrutiny.objects.filter(source_document=document).values('pk','stable_id','source_identifier','legislature','institution','occurred_on','official_title'))
        with zipfile.ZipFile(path) as archive:
            for member in members[start_cursor:start_cursor+batch_size]:
                if stop_requested() or time.monotonic()-started >= max_seconds:
                    stopped = True
                    break
                if member.file_size > 10*1024*1024:
                    raise ValidationError("archive_member_limit")
                data = archive.read(member)
                payload = json.loads(data.decode('utf-8-sig'))
                raw = payload.get('scrutin', payload)
                normalized = normalize_scrutiny(raw)
                uid = normalized['source_uid']
                if not re.fullmatch(r'VT(?:AN|CG)R\d+L\d+V\d+', uid):
                    raise ValidationError("scrutiny_uid_namespace_invalid")
                date.fromisoformat(normalized['occurred_on'])
                observations = list(iter_vote_observations(raw))
                if len({x['actor_ref'] for x in observations}) != len(observations):
                    raise ValidationError("archive_duplicate_actor")
                normalized.pop('raw', None)
                normalized.update(procedure_code=(raw.get('typeVote') or {}).get('codeTypeVote',''),
                    procedure_label=(raw.get('typeVote') or {}).get('libelleTypeVote',''),
                    observations_count=len(observations),
                    observation_categories=dict(Counter(x['normalized_category'] for x in observations)),
                    exact_text_relation='unresolved', public_eligible=False)
                # The AN route is NOT an official Congress route. No guessed link.
                if normalized['institution'] == 'Congrès du Parlement':
                    normalized['official_vote_url'] = None
                counts = (normalized['count_for'],normalized['count_against'],normalized['count_abstention'])
                normalized['counts_controlled'] = all(type(v) is int and v>=0 for v in counts) and normalized['expressed'] == counts[0]+counts[1] and normalized['voters'] == sum(counts)
                matching = [x for x in legacy if x['source_identifier']==normalized['source_identifier'] and x['legislature']==normalized['legislature']]
                legacy_id, reason = None, ''
                for previous in matching:
                    if previous['occurred_on'].isoformat() == normalized['occurred_on'] and previous['official_title']==normalized['official_title']:
                        legacy_id = previous['pk']
                        reason = 'institution_namespace_correction_candidate_old_object_preserved' if previous['institution'] != normalized['institution'] else 'existing_materialization_preserved'
                        break
                defaults = dict(institution=normalized['institution'],locator=member.filename,raw_sha256=hashlib.sha256(data).hexdigest(),normalized=normalized,
                    legacy_scrutiny_id=legacy_id, correction_reason=reason)
                row, created = StagedScrutiny.objects.get_or_create(document=document,source_uid=uid,defaults=defaults)
                if not created and any(getattr(row,k)!=v for k,v in defaults.items()):
                    raise ValidationError("staged_record_conflict")
                added += int(created)
                scan.cursor += 1
        scan.completed = scan.cursor == len(members)
        scan.save()
        return {'source_id':document.source.stable_id, 'source_sha256':document.sha256, 'parser_version':VERSION,
            'members_total':len(members),'cursor_before':start_cursor,'cursor_after':scan.cursor,'added':added,
            'completed':scan.completed,'cooperative_stop':stopped,'elapsed_seconds':round(time.monotonic()-started,3),
            'new_publishable_objects':0,'human_reviews_created':0,'network_calls':0}
