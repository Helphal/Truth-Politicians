"""File DV-VERITE. Les reçus ne sont ni des revues humaines ni des publications."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class VerificationJob(models.Model):
    revision = models.ForeignKey('atelier.EditorialRevision', on_delete=models.PROTECT, related_name='verification_jobs')
    key = models.CharField(max_length=64, unique=True)
    fingerprint = models.CharField(max_length=64)
    policy = models.CharField(max_length=80)
    input_snapshot = models.JSONField()
    state = models.CharField(max_length=24, default='pending')
    reason = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def state_label(self):
        return {'pending': 'À vérifier', 'running': 'Examen en cours', 'completed': 'Examen terminé',
                'blocked': 'Compléments ou corrections nécessaires', 'stale': 'À réexaminer',
                'error': 'Exécution interrompue', 'cancelled': 'Annulé'}.get(self.state, self.state)

    class Meta:
        indexes = [models.Index(fields=['state', 'created_at'])]


class VerificationAttempt(models.Model):
    job = models.ForeignKey(VerificationJob, on_delete=models.PROTECT, related_name='attempts')
    number = models.PositiveIntegerField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    execution_id = models.CharField(max_length=160)
    provider = models.CharField(max_length=80)
    model = models.CharField(max_length=100, default='non_expose')
    effort = models.CharField(max_length=40, default='non_expose')
    stage = models.CharField(max_length=24, default='evidence')
    evidence_findings = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    result_digest = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['job', 'number'], name='dv_verite_attempt_unique')]


class VerificationReceipt(models.Model):
    job = models.OneToOneField(VerificationJob, on_delete=models.PROTECT, related_name='receipt')
    attempt = models.OneToOneField(VerificationAttempt, on_delete=models.PROTECT)
    fingerprint = models.CharField(max_length=64)
    result_digest = models.CharField(max_length=64)
    decision = models.CharField(max_length=24)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('verification_receipt_immutable')
        super().save(*args, **kwargs)


class VerificationDelivery(models.Model):
    revision = models.ForeignKey('atelier.EditorialRevision', on_delete=models.PROTECT)
    receipt = models.ForeignKey(VerificationReceipt, on_delete=models.PROTECT)
    environment = models.CharField(max_length=16)
    sequence = models.PositiveBigIntegerField()
    mapping_digest = models.CharField(max_length=64)
    packet = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['revision', 'environment', 'sequence'], name='dv_verite_delivery_unique')]
