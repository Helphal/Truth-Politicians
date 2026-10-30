"""Detached-review return path. Only an explicitly configured synthetic test route is enabled."""
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from atelier.models import EditorialDossier, DetachedReviewEvidence
from .full_review import build_review_bundle, verify_detached_review
from .canonical import sha256_digest


def configuration():
    # Trust is supplied out of band by the operator; never from bundle/proof.
    return getattr(settings,'DV_DETACHED_REVIEW_PUBLIC_KEYS',{})


def verify_current_receipt(review):
    try: evidence=review.detached_evidence
    except DetachedReviewEvidence.DoesNotExist:return False
    try:
        current=build_review_bundle(review.revision,environment=review.environment,destination=evidence.destination)
        if current!=evidence.bundle or sha256_digest(current)!=evidence.bundle_sha256:return False
        verify_detached_review(current,evidence.proof,trusted_public_keys=configuration(),environment=review.environment,
                               destination=evidence.destination,allow_test_fixtures=review.environment=='test')
    except (ValidationError,ValueError,TypeError,KeyError):return False
    return True


@transaction.atomic
def accept_detached_review(*,dossier_id,expected_revision,bundle,proof,user,environment,destination):
    from .publication import record_human_review
    dossier=EditorialDossier.objects.select_for_update().get(pk=dossier_id)
    # This mission does not activate the editorial owner's real identity or signing authority.
    if environment!='test' or dossier.data_scope!='synthetic' or not getattr(getattr(user,'dv_profile',None),'is_test_identity',False):
        raise PermissionDenied('detached_real_identity_not_activated')
    revision=dossier.revisions.get(revision=expected_revision)
    current=build_review_bundle(revision,environment=environment,destination=destination)
    if current!=bundle:raise ValidationError('detached_bundle_not_current')
    verified=verify_detached_review(bundle,proof,trusted_public_keys=configuration(),environment=environment,
                                    destination=destination,allow_test_fixtures=True)
    existing=DetachedReviewEvidence.objects.filter(review__revision=revision,review__reviewer=user,bundle_sha256=verified['bundle_sha256'],proof=proof).first()
    if existing:
        review=existing.review
        latest=revision.human_reviews.filter(environment=environment).order_by('-created_at','-pk').first()
        if review.revoked or latest!=review or dossier.suspended:raise ValidationError('detached_return_revoked')
        return review
    if dossier.suspended:raise ValidationError('detached_dossier_suspended')
    review=record_human_review(dossier_id=dossier.pk,expected_revision=expected_revision,expected_digest=revision.snapshot_digest,
                              user=user,decision='approve',environment=environment,note='Signature détachée fictive vérifiée ; aucune publication réelle.')
    review.created_via='detached_ui';review.save(update_fields=['created_via','updated_at'])
    DetachedReviewEvidence.objects.create(review=review,bundle=bundle,proof=proof,bundle_sha256=verified['bundle_sha256'],
                                         key_id=verified['key_id'],destination=destination)
    return review
