"""Append-only proof records. Technical evidence is never a human review."""
from django.core.exceptions import ValidationError
from django.db import models


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('proof_record_immutable')

    def delete(self):
        raise ValidationError('proof_record_immutable')

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError('proof_record_immutable')

    def bulk_create(self, *args, **kwargs):
        raise ValidationError('proof_record_requires_validated_creation')


class AppendOnlyProof(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError('proof_record_immutable')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('proof_record_immutable')


class ProofArtifact(AppendOnlyProof):
    revision = models.ForeignKey('atelier.EditorialRevision', on_delete=models.PROTECT,
                                 related_name='proof_artifacts')
    revision_fingerprint = models.CharField(max_length=64)
    kind = models.CharField(max_length=32, choices=[(x, x) for x in
        ('preflight', 'semantic', 'counterevidence', 'assessment', 'editorial',
         'rights', 'dependency_graph')])
    schema = models.CharField(max_length=80)
    payload = models.JSONField()
    payload_digest = models.CharField(max_length=64)
    # Declared technical actor only; this field never creates an account or review.
    actor_id = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['revision', 'kind', 'payload_digest'], name='dv_unique_proof_artifact')]
        indexes = [models.Index(fields=['revision', 'kind'], name='dv_proof_revision_kind')]

    def clean(self):
        from .services.canonical import sha256_digest
        import re
        if not re.fullmatch(r'[0-9a-f]{64}', self.revision_fingerprint or ''):
            raise ValidationError('proof_fingerprint_invalid')
        if not isinstance(self.payload, dict) or sha256_digest(self.payload) != self.payload_digest:
            raise ValidationError('proof_payload_digest_mismatch')
        declared = self.payload.get('fingerprint', self.payload.get('revision_fingerprint'))
        if declared is not None and declared != self.revision_fingerprint:
            raise ValidationError('proof_payload_fingerprint_mismatch')
        declared_schema = self.payload.get('schema', self.payload.get('contract'))
        if declared_schema is not None and declared_schema != self.schema:
            raise ValidationError('proof_payload_schema_mismatch')

    def validate_binding(self, snapshot):
        """Call on every receipt read; a digest alone is not a current binding."""
        from .services.canonical import sha256_digest
        self.clean()
        if sha256_digest(snapshot) != self.revision_fingerprint:
            raise ValidationError('proof_revision_binding_stale')
        return True


class ScrutinyTextBinding(AppendOnlyProof):
    """Explicit source-pinned object binding; not a semantic qualification."""
    scrutiny = models.ForeignKey('atelier.Scrutiny', on_delete=models.PROTECT,
                                 related_name='text_bindings')
    submitted_text = models.ForeignKey('atelier.TextVersion', on_delete=models.PROTECT,
                                      related_name='scrutiny_bindings')
    source_document = models.ForeignKey('atelier.DocumentVersion', on_delete=models.PROTECT,
                                       related_name='scrutiny_text_bindings')
    version = models.PositiveIntegerField()
    procedure = models.CharField(max_length=250)
    actor_id = models.PositiveIntegerField()
    payload = models.JSONField()
    payload_digest = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['scrutiny', 'version'],
                                               name='dv_unique_scrutiny_text_version')]
        indexes = [models.Index(fields=['scrutiny', 'payload_digest'], name='dv_scrutiny_text_digest')]

    def clean(self):
        from .services.canonical import sha256_digest
        from .services.catalogue import source_ref
        from .services.provenance import _safe_original, _Tree
        from django.conf import settings
        from .models import DocumentVersion
        import re
        fields = {'contract', 'version', 'scrutiny_id', 'submitted_text_id',
            'legislative_dossier_id', 'source_version', 'text_source_version',
            'dossier_source_version', 'procedure', 'object_kind', 'literal_text_status',
            'source_locator', 'exact_source_quote', 'actor_id'}
        p = self.payload
        if (not isinstance(p, dict) or set(p) != fields
                or p.get('contract') != 'dv.scrutiny-text-binding/1'
                or sha256_digest(p) != self.payload_digest):
            raise ValidationError('scrutiny_text_binding_closed_payload_required')
        if (type(self.version) is not int or self.version < 1 or p['version'] != self.version
                or type(self.actor_id) is not int or self.actor_id < 1 or p['actor_id'] != self.actor_id):
            raise ValidationError('scrutiny_text_binding_version_actor_mismatch')
        if (p['scrutiny_id'] != self.scrutiny.stable_id
                or p['submitted_text_id'] != self.submitted_text.stable_id
                or p['legislative_dossier_id'] != self.submitted_text.dossier.stable_id
                or self.submitted_text.relation != 'submitted'
                or self.submitted_text.occurred_on != self.scrutiny.occurred_on
                or p['procedure'] != self.procedure or not self.procedure.strip()):
            raise ValidationError('scrutiny_text_binding_object_relation_mismatch')
        if (p['object_kind'] not in {'motion', 'amendment', 'whole_text'}
                or p['literal_text_status'] not in {'not_received', 'received_literal_document', 'unknown'}):
            raise ValidationError('scrutiny_text_binding_object_status_invalid')
        if p['object_kind'] == 'motion' and self.submitted_text.kind != 'motion':
            raise ValidationError('scrutiny_text_binding_kind_mismatch')
        for key, doc in [('source_version', self.source_document),
                         ('text_source_version', self.submitted_text.source_document)]:
            if not doc or doc.quarantined or p[key] != source_ref(doc):
                raise ValidationError('scrutiny_text_binding_source_version_mismatch')
        ref = p['dossier_source_version']
        if not isinstance(ref, dict) or set(ref) != {'source_id', 'version_id', 'sha256', 'public_url'}:
            raise ValidationError('scrutiny_text_binding_dossier_reference_required')
        documents = list(DocumentVersion.objects.filter(source__stable_id=ref['source_id'],
            version_id=ref['version_id'], sha256=ref['sha256']).select_related('source'))
        if (len(documents) != 1 or documents[0].quarantined or source_ref(documents[0]) != ref
                or ref['public_url'] != self.submitted_text.dossier.official_url):
            raise ValidationError('scrutiny_text_binding_dossier_source_mismatch')
        if (type(p['source_locator']) is not str or not p['source_locator'].strip()
                or type(p['exact_source_quote']) is not str or not p['exact_source_quote'].strip()):
            raise ValidationError('scrutiny_text_binding_source_quote_required')
        quote_text = None
        for doc in [self.source_document, self.submitted_text.source_document, documents[0]]:
            descriptor = {'sha256': doc.sha256, 'byte_size': doc.byte_size,
                'original_path': doc.original_path, 'quarantined': doc.quarantined}
            try:
                data = _safe_original(descriptor, settings.DV_EVIDENCE_VAULT_PATH)
                if doc.pk == self.source_document_id:
                    quote_text = _Tree(data.decode('utf-8-sig')).root.text()
            except (OSError, ValueError, UnicodeError):
                raise ValidationError('scrutiny_text_binding_original_integrity_failed') from None
        if not quote_text or p['exact_source_quote'] not in quote_text:
            raise ValidationError('scrutiny_text_binding_quote_not_in_original')
        # A scrutiny sheet or a debate report attests a procedural object; it
        # does not silently become the autonomous literal motion document.
        if p['literal_text_status'] == 'received_literal_document' and re.search(
                r'/(?:scrutins|comptes-rendus|scrutin-public|seances)/',
                self.submitted_text.source_document.source.document_url):
            raise ValidationError('procedural_record_is_not_literal_submitted_document')

    def validate_binding(self, *, scrutiny_id, version, payload_digest):
        self.clean()
        if (self.scrutiny.stable_id != scrutiny_id or self.version != version
                or self.payload_digest != payload_digest):
            raise ValidationError('scrutiny_text_binding_selection_changed')
        return True


class ProvenanceCertificate(AppendOnlyProof):
    revision = models.ForeignKey('atelier.EditorialRevision', on_delete=models.PROTECT,
                                 related_name='provenance_certificates')
    document = models.ForeignKey('atelier.DocumentVersion', on_delete=models.PROTECT,
                                 null=True, blank=True)
    revision_fingerprint = models.CharField(max_length=64)
    certificate_id = models.CharField(max_length=64)
    claim_id = models.CharField(max_length=100)
    location = models.CharField(max_length=1000)
    fact_type = models.CharField(max_length=80)
    source_id = models.CharField(max_length=160, blank=True)
    version_id = models.CharField(max_length=160, blank=True)
    source_sha256 = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=[(x, x) for x in
        ('proven', 'conflict', 'missing', 'ambiguous')])
    extraction_rule = models.CharField(max_length=100)
    transformation_rule = models.CharField(max_length=100)
    result_sha256 = models.CharField(max_length=64)
    payload = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['revision', 'certificate_id'], name='dv_unique_provenance_certificate')]
        indexes = [models.Index(fields=['revision', 'status'], name='dv_provenance_revision_status')]

    def clean(self):
        from .services.provenance import validate_certificate_shape
        validate_certificate_shape(self.payload)
        for name in ('certificate_id', 'claim_id', 'location', 'fact_type', 'source_id',
                     'version_id', 'source_sha256', 'status', 'result_sha256',
                     'extraction_rule', 'transformation_rule'):
            if getattr(self, name) != self.payload.get(name):
                raise ValidationError('provenance_model_payload_mismatch:' + name)
        if self.revision_fingerprint != self.payload.get('fingerprint'):
            raise ValidationError('provenance_model_fingerprint_mismatch')
        if self.document_id:
            document = self.document
            if (document.source.stable_id, document.version_id, document.sha256) != (
                    self.source_id, self.version_id, self.source_sha256):
                raise ValidationError('provenance_document_binding_mismatch')

    def validate_binding(self, snapshot, *, received_sources=None, source_root=None):
        from .services.provenance import build_provenance_report
        from .services.canonical import sha256_digest
        self.clean()
        if sha256_digest(snapshot) != self.revision_fingerprint:
            raise ValidationError('provenance_revision_binding_stale')
        rebuilt = build_provenance_report(snapshot, received_sources=received_sources,
                                         source_root=source_root)
        if self.payload not in rebuilt['certificates']:
            raise ValidationError('provenance_original_recomputation_mismatch')
        return True


class AffiliationObservation(AppendOnlyProof):
    person = models.ForeignKey('atelier.Person', on_delete=models.PROTECT,
                               related_name='affiliation_observations')
    organization = models.ForeignKey('atelier.Organization', on_delete=models.PROTECT,
                                     related_name='affiliation_observations')
    source_document = models.ForeignKey('atelier.DocumentVersion', on_delete=models.PROTECT)
    legacy_affiliation = models.ForeignKey('atelier.Affiliation', on_delete=models.PROTECT,
                                          null=True, blank=True)
    observed_on = models.DateField()
    known_at = models.DateTimeField()
    record_id = models.CharField(max_length=220)
    record_locator = models.JSONField()
    extraction_rule = models.CharField(max_length=100)
    result_digest = models.CharField(max_length=64)
    payload = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=[
            'person', 'organization', 'source_document', 'observed_on', 'result_digest'],
            name='dv_unique_affiliation_observation')]

    def clean(self):
        from .services.canonical import sha256_digest
        if not isinstance(self.payload, dict) or sha256_digest(self.payload) != self.result_digest:
            raise ValidationError('affiliation_observation_digest_mismatch')
        if self.organization.kind != 'parliamentary_group':
            raise ValidationError('observed_group_is_not_party')
        if self.payload.get('observed_on') != self.observed_on.isoformat():
            raise ValidationError('affiliation_observation_date_mismatch')
        if self.payload.get('schema') != 'dv.affiliation-observation/1':
            raise ValidationError('affiliation_observation_schema_invalid')
        if (self.payload.get('person_id'), self.payload.get('organization_id')) != (
                self.person.stable_id, self.organization.stable_id):
            raise ValidationError('affiliation_observation_actor_mismatch')
        from .services.catalogue import source_ref
        if self.payload.get('source_version') != source_ref(self.source_document):
            raise ValidationError('affiliation_observation_source_mismatch')
        if not isinstance(self.record_locator, dict) or not self.record_locator or self.payload.get('record_locator') != self.record_locator:
            raise ValidationError('affiliation_observation_locator_mismatch')
        if self.known_at != self.source_document.captured_at:
            raise ValidationError('affiliation_observation_receipt_time_mismatch')
        if any(key in self.payload for key in ('starts_on', 'ends_on', 'membership_active')):
            raise ValidationError('affiliation_observation_cannot_assert_boundaries')
