"""Modèle transactionnel de l'atelier.

Les originaux, faits, interprétations, revues et publications sont séparés. Les
champs JSON servent uniquement aux données structurées variables ; les identités,
relations datées, versions et états qui portent les invariants ont leurs tables.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class Timestamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class DataState(models.TextChoices):
    KNOWN = "known", "Connue"
    UNKNOWN = "unknown", "Inconnue"
    UNAVAILABLE = "unavailable", "Indisponible"
    NOT_APPLICABLE = "not_applicable", "Non applicable"


class DocumentaryQualification(models.TextChoices):
    CANDIDATE = "candidate", "À qualifier"
    QUALIFIED = "qualified", "Qualifiée"
    REJECTED = "rejected", "Écartée"


class ArchiveScan(Timestamped):
    """Version-pinned private staging cursor, not evidence of publication eligibility."""
    document = models.OneToOneField("DocumentVersion", on_delete=models.PROTECT)
    parser_version = models.CharField(max_length=80)
    cursor = models.PositiveIntegerField(default=0)
    total_members = models.PositiveIntegerField(default=0)
    completed = models.BooleanField(default=False)
    last_error = models.CharField(max_length=120, blank=True)


class CatalogueScopeVersion(Timestamped):
    scope_id = models.CharField(max_length=220, unique=True)
    generation = models.PositiveBigIntegerField(default=0)
    transfer = models.ForeignKey("CatalogueTransfer", on_delete=models.PROTECT, null=True)
    suspended = models.BooleanField(default=False)


class StagedScrutiny(Timestamped):
    document = models.ForeignKey("DocumentVersion", on_delete=models.PROTECT)
    source_uid = models.CharField(max_length=160)
    institution = models.CharField(max_length=120)
    locator = models.CharField(max_length=500)
    raw_sha256 = models.CharField(max_length=64)
    normalized = models.JSONField(default=dict)
    qualification = models.CharField(max_length=80, default="exact_text_not_linked")
    legacy_scrutiny = models.ForeignKey("Scrutiny", on_delete=models.PROTECT, null=True, blank=True)
    correction_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "source_uid"], name="dv_staged_source_uid")]


class ReferenceResolution(Timestamped):
    document = models.ForeignKey("DocumentVersion", on_delete=models.PROTECT)
    kind = models.CharField(max_length=40)
    institutional_ref = models.CharField(max_length=180)
    locator = models.CharField(max_length=500)
    previous = models.JSONField(default=dict)
    proposed = models.JSONField(default=dict)
    status = models.CharField(max_length=40)
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "kind", "institutional_ref"], name="dv_reference_version_decision")]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("reference_resolution_immutable")
        super().save(*args, **kwargs)


class IndexedPassage(Timestamped):
    document = models.ForeignKey("DocumentVersion", on_delete=models.PROTECT)
    extractor_version = models.CharField(max_length=80)
    ordinal = models.PositiveIntegerField()
    locator = models.CharField(max_length=500)
    visible_text = models.TextField()
    search_text = models.TextField()
    text_sha256 = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "extractor_version", "ordinal"], name="dv_indexed_passage_version")]


class Environment(models.TextChoices):
    TEST = "test", "Test"
    PRIVATE = "private", "Privé"
    PRODUCTION = "production", "Production"


class Source(Timestamped):
    class AccessStatus(models.TextChoices):
        CANDIDATE = "candidate", "À qualifier"
        QUALIFIED = "qualified", "Qualifiée"
        ACTIVE = "active", "Active"
        BLOCKED = "blocked", "Bloquée"
        RETIRED = "retired", "Retirée"

    class RightsStatus(models.TextChoices):
        UNKNOWN = "unknown", "À qualifier"
        INTERNAL_ONLY = "internal_only", "Conservation interne seulement"
        EXCERPT_ALLOWED = "excerpt_allowed", "Extraits diffusables"
        OPEN = "open", "Réutilisation ouverte"
        FORBIDDEN = "forbidden", "Conservation interdite"

    stable_id = models.CharField(max_length=120, unique=True)
    producer = models.CharField(max_length=200)
    family = models.CharField(max_length=80)
    discovery_url = models.URLField(max_length=1000)
    document_url = models.URLField(max_length=1000, blank=True)
    terms_url = models.URLField(max_length=1000, blank=True)
    temporal_scope = models.CharField(max_length=250, blank=True)
    licence_label = models.CharField(max_length=250, blank=True)
    rights_status = models.CharField(max_length=32, choices=RightsStatus.choices, default=RightsStatus.UNKNOWN)
    access_status = models.CharField(max_length=24, choices=AccessStatus.choices, default=AccessStatus.CANDIDATE)
    operational_enabled = models.BooleanField(default=False)
    candidate_frequency = models.CharField(max_length=120, blank=True)
    qualification_notes = models.TextField(blank=True)
    qualified_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_failure_code = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["producer", "stable_id"]

    def __str__(self) -> str:
        return f"{self.producer} — {self.stable_id}"


class DocumentVersion(Timestamped):
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="documents")
    version_id = models.CharField(max_length=160)
    sha256 = models.CharField(max_length=64)
    document_date = models.DateTimeField(null=True, blank=True)
    captured_at = models.DateTimeField()
    media_type = models.CharField(max_length=160)
    byte_size = models.PositiveBigIntegerField()
    original_path = models.CharField(max_length=1000, blank=True)
    public_copy_allowed = models.BooleanField(default=False)
    parser_version = models.CharField(max_length=80, blank=True)
    etag = models.CharField(max_length=500, blank=True)
    last_modified = models.CharField(max_length=500, blank=True)
    collection_receipt = models.JSONField(default=dict, blank=True)
    quarantined = models.BooleanField(default=False)
    quarantine_reason = models.CharField(max_length=120, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "sha256"], name="dv_unique_source_document_bytes"),
            models.UniqueConstraint(fields=["source", "version_id"], name="dv_unique_source_version_id"),
        ]
        indexes = [models.Index(fields=["source", "captured_at"])]

    def __str__(self) -> str:
        return f"{self.source.stable_id}@{self.version_id}"


class EvidencePassage(Timestamped):
    document = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT, related_name="passages")
    exact_text = models.TextField(blank=True)
    text_state = models.CharField(max_length=24, choices=DataState.choices, default=DataState.KNOWN)
    transcription_status = models.CharField(max_length=40, default="source_text")
    locator = models.CharField(max_length=500)
    context = models.TextField(blank=True)
    public_url = models.URLField(max_length=1000)
    public_access_status = models.CharField(max_length=24, default="available")

    def __str__(self) -> str:
        excerpt = self.exact_text.strip().replace("\n", " ")[:90] or "passage sans texte"
        return f"{self.document.source.producer} — {self.locator} — {excerpt}"


class Person(Timestamped):
    class IdentityStatus(models.TextChoices):
        RESOLVED = "resolved", "Résolue"
        CONFLICT = "conflict", "Conflit à examiner"
        UNRESOLVED = "unresolved", "Non résolue"

    stable_id = models.CharField(max_length=160, unique=True)
    display_name = models.CharField(max_length=250)
    aliases = models.JSONField(default=list, blank=True)
    institutional_ids = models.JSONField(default=dict, blank=True)
    identity_status = models.CharField(max_length=24, choices=IdentityStatus.choices, default=IdentityStatus.RESOLVED)

    class Meta:
        ordering = ["display_name", "stable_id"]

    def __str__(self) -> str:
        return self.display_name


class Organization(Timestamped):
    class Kind(models.TextChoices):
        INSTITUTION = "institution", "Institution"
        PARLIAMENTARY_GROUP = "parliamentary_group", "Groupe parlementaire"
        PARTY = "party", "Parti"
        COALITION = "coalition", "Coalition"

    stable_id = models.CharField(max_length=160, unique=True)
    name = models.CharField(max_length=250)
    kind = models.CharField(max_length=32, choices=Kind.choices)
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)

    def __str__(self) -> str:
        return self.name


class Mandate(Timestamped):
    stable_id = models.CharField(max_length=180, unique=True)
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="mandates")
    institution = models.CharField(max_length=120)
    mandate_type = models.CharField(max_length=120)
    territory = models.CharField(max_length=200, blank=True)
    starts_on = models.DateField()
    ends_on = models.DateField(null=True, blank=True)
    source_document = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_on__isnull=True) | Q(ends_on__gte=models.F("starts_on")),
                name="dv_mandate_valid_interval",
            )
        ]


class Affiliation(Timestamped):
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="affiliations")
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="affiliations")
    mandate = models.ForeignKey(Mandate, on_delete=models.PROTECT, null=True, blank=True, related_name="affiliations")
    status = models.CharField(max_length=100)
    starts_on = models.DateField()
    ends_on = models.DateField(null=True, blank=True)
    observed_on = models.DateField(null=True, blank=True)
    ambiguity = models.TextField(blank=True)
    source_document = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_on__isnull=True) | Q(ends_on__gte=models.F("starts_on")),
                name="dv_affiliation_valid_interval",
            ),
            models.UniqueConstraint(
                fields=["person", "organization", "status", "starts_on"], name="dv_unique_affiliation_period"
            ),
        ]
        indexes = [models.Index(fields=["person", "starts_on", "ends_on"])]


class LegislativeDossier(Timestamped):
    stable_id = models.CharField(max_length=180, unique=True)
    institution = models.CharField(max_length=120)
    official_title = models.TextField()
    official_url = models.URLField(max_length=1000)
    status = models.CharField(max_length=80)


class TextVersion(Timestamped):
    class Relation(models.TextChoices):
        SUBMITTED = "submitted", "Soumis au vote"
        ADOPTED = "adopted", "Issu du vote"
        IN_FORCE = "in_force", "En vigueur"
        OTHER = "other", "Autre"

    stable_id = models.CharField(max_length=200, unique=True)
    dossier = models.ForeignKey(LegislativeDossier, on_delete=models.PROTECT, related_name="text_versions")
    kind = models.CharField(max_length=80)
    official_title = models.TextField()
    version_label = models.CharField(max_length=200)
    stage = models.CharField(max_length=160)
    relation = models.CharField(max_length=24, choices=Relation.choices)
    occurred_on = models.DateField(null=True, blank=True)
    official_url = models.URLField(max_length=1000)
    source_document = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT, null=True, blank=True)


class Measure(Timestamped):
    stable_id = models.CharField(max_length=200, unique=True)
    text_version = models.ForeignKey(TextVersion, on_delete=models.PROTECT, related_name="measures")
    title = models.TextField()
    locator = models.CharField(max_length=300)
    mechanism = models.TextField(blank=True)
    plain_summary = models.TextField(blank=True)
    concrete_effect = models.TextField(blank=True)
    composite = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.title[:180]


class Scrutiny(Timestamped):
    class NominativeStatus(models.TextChoices):
        AVAILABLE = "available", "Disponible"
        PARTIAL = "partial", "Partiel"
        NOT_AVAILABLE = "not_available", "Non disponible"

    stable_id = models.CharField(max_length=220, unique=True)
    source_identifier = models.CharField(max_length=120)
    institution = models.CharField(max_length=120)
    legislature = models.CharField(max_length=40)
    session = models.CharField(max_length=80, blank=True)
    occurred_on = models.DateField()
    date_precision = models.CharField(max_length=24, default="day")
    procedure = models.CharField(max_length=200)
    official_title = models.TextField()
    official_vote_url = models.URLField(max_length=1000)
    submitted_text = models.ForeignKey(TextVersion, on_delete=models.PROTECT, related_name="scrutinies")
    result_status = models.CharField(max_length=80)
    nominative_status = models.CharField(max_length=24, choices=NominativeStatus.choices)
    registered = models.PositiveIntegerField(null=True, blank=True)
    voters = models.PositiveIntegerField(null=True, blank=True)
    expressed = models.PositiveIntegerField(null=True, blank=True)
    count_for = models.PositiveIntegerField(null=True, blank=True)
    count_against = models.PositiveIntegerField(null=True, blank=True)
    count_abstention = models.PositiveIntegerField(null=True, blank=True)
    counts_state = models.CharField(max_length=24, choices=DataState.choices, default=DataState.KNOWN)
    source_document = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT, related_name="scrutinies")
    anomaly = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "legislature", "source_identifier"], name="dv_unique_scrutiny_namespace"
            )
        ]
        indexes = [models.Index(fields=["institution", "occurred_on"])]

    def __str__(self) -> str:
        return f"{self.occurred_on:%d/%m/%Y} — {self.official_title[:150]}"

    def clean(self) -> None:
        if self.counts_state == DataState.KNOWN:
            values = (self.count_for, self.count_against, self.count_abstention)
            if any(value is None for value in values):
                raise ValidationError("Les trois décomptes sont requis lorsque leur état est connu")
            if self.expressed is not None and self.count_for + self.count_against != self.expressed:
                raise ValidationError("Pour + contre doit égaler les suffrages exprimés")


class ScrutinyIdentityCorrection(Timestamped):
    """Immutable quarantine record; never silently rebind legacy evidence."""
    legacy = models.OneToOneField(Scrutiny, on_delete=models.PROTECT, related_name='identity_correction')
    original_snapshot = models.JSONField()
    replacement_ids = models.JSONField()
    evidence = models.JSONField()
    correction_digest = models.CharField(max_length=64, unique=True)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('identity_correction_immutable')
        super().save(*args, **kwargs)


class FactQualification(Timestamped):
    scrutiny = models.ForeignKey(Scrutiny,on_delete=models.PROTECT,related_name='qualifications')
    version_digest = models.CharField(max_length=64)
    state = models.CharField(max_length=32,choices=[('blocked','Pièce exacte attendue'),('qualified','Qualifié')])
    object_type = models.CharField(max_length=80)
    expected_pieces = models.JSONField(default=list)
    references = models.JSONField(default=list)
    explanation = models.TextField()
    class Meta:
        constraints=[models.UniqueConstraint(fields=['scrutiny','version_digest'],name='dv_unique_fact_qualification')]
    def save(self,*args,**kwargs):
        if self.pk:raise ValidationError('fact_qualification_immutable')
        super().save(*args,**kwargs)


class VoteObservation(Timestamped):
    class Category(models.TextChoices):
        FOR = "for", "Pour"
        AGAINST = "against", "Contre"
        ABSTENTION = "abstention", "Abstention"
        NON_VOTING = "non_voting", "N'a pas pris part"
        NOT_RECORDED = "not_recorded", "Non enregistré"
        UNKNOWN = "unknown", "Inconnu"

    scrutiny = models.ForeignKey(Scrutiny, on_delete=models.PROTECT, related_name="observations")
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="vote_observations")
    mandate = models.ForeignKey(Mandate, on_delete=models.PROTECT, null=True, blank=True)
    affiliation_at_event = models.ForeignKey(Organization, on_delete=models.PROTECT, null=True, blank=True)
    source_category = models.CharField(max_length=120)
    normalized_category = models.CharField(max_length=24, choices=Category.choices)
    mapping_rule = models.CharField(max_length=80)
    source_document = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT)

    def clean(self):
        super().clean()
        if self.mandate_id and self.person_id and self.scrutiny_id:
            from .services.mandate_links import validate_mandate_link
            validate_mandate_link(
                person_id=self.person_id, mandate_person_id=self.mandate.person_id,
                institution=self.scrutiny.institution, mandate_institution=self.mandate.institution,
                occurred_on=self.scrutiny.occurred_on, starts_on=self.mandate.starts_on,
                ends_on=self.mandate.ends_on, date_precision=self.scrutiny.date_precision,
            )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scrutiny", "person", "source_document"], name="dv_unique_vote_observation_revision"
            )
        ]


class VoteCorrection(Timestamped):
    observation = models.ForeignKey(VoteObservation, on_delete=models.PROTECT, related_name="corrections")
    author_label = models.CharField(max_length=250)
    statement = models.TextField()
    occurred_on = models.DateField(null=True, blank=True)
    source_document = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT)


class Theme(Timestamped):
    stable_id = models.CharField(max_length=120, unique=True)
    label = models.CharField(max_length=160)
    definition = models.TextField()
    aliases = models.JSONField(default=list, blank=True)


class ThemeAssignment(Timestamped):
    class Origin(models.TextChoices):
        OFFICIAL = "official", "Classification officielle"
        RULE = "rule", "Règle déterministe"
        PROPOSED = "proposed", "Proposition à examiner"

    theme = models.ForeignKey(Theme, on_delete=models.PROTECT, related_name="assignments")
    target_type = models.CharField(max_length=24, choices=[("dossier", "Dossier"), ("text", "Texte"), ("measure", "Mesure")])
    target_stable_id = models.CharField(max_length=200)
    locator = models.CharField(max_length=300, blank=True)
    origin = models.CharField(max_length=24, choices=Origin.choices)
    evidence = models.ForeignKey(EvidencePassage, on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=24, default="accepted")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["theme", "target_type", "target_stable_id", "locator"], name="dv_unique_theme_assignment"
            )
        ]


class Declaration(Timestamped):
    class Medium(models.TextChoices):
        PRESS = "press", "Presse"
        TELEVISION = "television", "Télévision"
        RADIO = "radio", "Radio"
        VIDEO = "video", "Vidéo en ligne"
        SOCIAL = "social", "Réseau social"
        SPEECH = "speech", "Discours ou réunion publique"
        INSTITUTIONAL = "institutional", "Publication institutionnelle"
        OTHER_PUBLIC = "other_public", "Autre source publique"

    class SourceRole(models.TextChoices):
        ORIGINAL = "original", "Source originale"
        RELAY = "relay", "Relais ou reprise"
        TRANSCRIPT = "transcript", "Transcription"
        UNKNOWN = "unknown", "Rôle à qualifier"

    class PositionScope(models.TextChoices):
        PERSONAL = "personal", "Position personnelle"
        COLLECTIVE = "collective", "Position collective"
        INDETERMINATE = "indeterminate", "Portée indéterminée"

    class DatePrecision(models.TextChoices):
        DAY = "day", "Jour"
        MONTH = "month", "Mois"
        YEAR = "year", "Année"
        INTERVAL = "interval", "Intervalle"
        ESTIMATED = "estimated", "Estimée"
        UNKNOWN = "unknown", "Inconnue"

    stable_id = models.CharField(max_length=200, unique=True)
    speaker_person = models.ForeignKey(Person, on_delete=models.PROTECT, null=True, blank=True)
    speaker_organization = models.ForeignKey(Organization, on_delete=models.PROTECT, null=True, blank=True)
    speaker_label = models.CharField(max_length=250)
    capacity = models.CharField(max_length=250)
    exact_quote = models.TextField()
    paraphrase = models.TextField(blank=True)
    occurred_on = models.DateField(null=True, blank=True)
    occurred_on_state = models.CharField(max_length=24, choices=DataState.choices, default=DataState.KNOWN)
    date_precision = models.CharField(
        max_length=24, choices=DatePrecision.choices, default=DatePrecision.DAY
    )
    published_on = models.DateField(null=True, blank=True)
    occurred_interval_end = models.DateField(
        null=True,
        blank=True,
        help_text="Borne finale qualifiée d'un intervalle documentaire (distincte de published_on).",
    )
    quantifier = models.CharField(max_length=250, blank=True)
    conditions = models.TextField(blank=True)
    amount = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    amount_unit = models.CharField(max_length=80, blank=True)
    position_scope = models.CharField(
        max_length=24, choices=PositionScope.choices, default=PositionScope.INDETERMINATE
    )
    source_passage = models.ForeignKey(EvidencePassage, on_delete=models.PROTECT, related_name="declarations")
    medium = models.CharField(max_length=32, choices=Medium.choices, default=Medium.OTHER_PUBLIC)
    source_role = models.CharField(max_length=24, choices=SourceRole.choices, default=SourceRole.UNKNOWN)
    verification_status = models.CharField(max_length=32, default="source_text")

    def clean(self) -> None:
        if not self.speaker_person_id and not self.speaker_organization_id:
            raise ValidationError("Une déclaration doit être rattachée à une personne ou une organisation")
        if self.occurred_on_state == DataState.KNOWN and self.occurred_on is None:
            raise ValidationError("La date est requise lorsque son état est connu")
        if self.position_scope == self.PositionScope.PERSONAL and not self.speaker_person_id:
            raise ValidationError("Une portée personnelle exige une personne locutrice")
        if self.position_scope == self.PositionScope.COLLECTIVE and not self.speaker_organization_id:
            raise ValidationError("Une portée collective exige une organisation représentée")
        if (
            self.date_precision == self.DatePrecision.INTERVAL
            and self.occurred_on
            and self.occurred_interval_end
            and self.occurred_interval_end < self.occurred_on
        ):
            raise ValidationError("La borne finale d'intervalle ne peut précéder le début documenté.")

    def __str__(self) -> str:
        return f"{self.speaker_label} — {self.exact_quote[:130]}"


class DeclarationMeasureLink(Timestamped):
    """Qualification documentaire versionnée d'une parole envers une mesure.

    Une ligne est immuable. Toute correction crée la révision suivante afin de
    préserver les décisions et preuves antérieures.
    """

    class Stance(models.TextChoices):
        SUPPORT = "support", "Soutient l'effet"
        OPPOSE = "oppose", "S'oppose à l'effet"
        CONDITIONAL = "conditional", "Position conditionnelle"
        UNCLEAR = "unclear", "Position non déterminée"

    declaration = models.ForeignKey(Declaration, on_delete=models.PROTECT, related_name="measure_links")
    measure = models.ForeignKey(Measure, on_delete=models.PROTECT, related_name="declaration_links")
    revision = models.PositiveIntegerField(default=1)
    qualification_status = models.CharField(
        max_length=24,
        choices=DocumentaryQualification.choices,
        default=DocumentaryQualification.CANDIDATE,
    )
    stance = models.CharField(max_length=24, choices=Stance.choices, default=Stance.UNCLEAR)
    rationale = models.TextField()
    evidence = models.ForeignKey(EvidencePassage, on_delete=models.PROTECT, related_name="declaration_measure_links")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="recorded_declaration_measure_links",
    )
    method_version = models.CharField(max_length=80, default="DV-THEMATIC-2")
    content_fingerprint = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["declaration", "measure", "revision"],
                name="dv_unique_declaration_measure_revision",
            )
        ]

    def clean(self) -> None:
        if self.declaration_id and self.evidence_id and self.evidence_id != self.declaration.source_passage_id:
            raise ValidationError("La relation doit citer le passage source exact de la déclaration")
        if self.qualification_status == DocumentaryQualification.QUALIFIED and self.stance == self.Stance.UNCLEAR:
            raise ValidationError("Une relation qualifiée doit préciser le sens de la position")
        if not self.rationale.strip():
            raise ValidationError("La justification documentaire est requise")
        if self.declaration_id and self.measure_id and self.evidence_id:
            from atelier.services.thematic_integrity import fingerprint_declaration_measure_link

            self.content_fingerprint = fingerprint_declaration_measure_link(self)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("declaration_measure_link_immutable")
        if self.declaration_id and self.measure_id and self.evidence_id and not self.content_fingerprint:
            from atelier.services.thematic_integrity import fingerprint_declaration_measure_link

            self.content_fingerprint = fingerprint_declaration_measure_link(self)
        super().save(*args, **kwargs)


class MeasureScrutinyLink(Timestamped):
    """Qualification versionnée de l'effet d'un vote sur une mesure."""

    class VoteEffect(models.TextChoices):
        ADVANCES = "advances", "Un vote pour fait avancer la mesure"
        BLOCKS = "blocks", "Un vote pour bloque la mesure"
        MIXED = "mixed", "Effet composite"
        UNCLEAR = "unclear", "Effet non déterminé"

    measure = models.ForeignKey(Measure, on_delete=models.PROTECT, related_name="scrutiny_links")
    scrutiny = models.ForeignKey(Scrutiny, on_delete=models.PROTECT, related_name="measure_links")
    revision = models.PositiveIntegerField(default=1)
    qualification_status = models.CharField(
        max_length=24,
        choices=DocumentaryQualification.choices,
        default=DocumentaryQualification.CANDIDATE,
    )
    vote_effect = models.CharField(max_length=24, choices=VoteEffect.choices, default=VoteEffect.UNCLEAR)
    rationale = models.TextField()
    evidence = models.ForeignKey(EvidencePassage, on_delete=models.PROTECT, related_name="measure_scrutiny_links")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="recorded_measure_scrutiny_links",
    )
    method_version = models.CharField(max_length=80, default="DV-THEMATIC-2")
    content_fingerprint = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["measure", "scrutiny", "revision"],
                name="dv_unique_measure_scrutiny_revision",
            )
        ]

    def clean(self) -> None:
        if self.scrutiny_id and self.evidence_id:
            allowed_documents = {self.scrutiny.source_document_id, self.scrutiny.submitted_text.source_document_id}
            if self.evidence.document_id not in allowed_documents:
                raise ValidationError("La relation doit citer la source du scrutin ou du texte soumis")
        if self.qualification_status == DocumentaryQualification.QUALIFIED and self.vote_effect == self.VoteEffect.UNCLEAR:
            raise ValidationError("Une relation qualifiée doit préciser l'effet du vote")
        if not self.rationale.strip():
            raise ValidationError("La justification documentaire est requise")
        if self.measure_id and self.scrutiny_id and self.evidence_id:
            from atelier.services.thematic_integrity import fingerprint_measure_scrutiny_link

            self.content_fingerprint = fingerprint_measure_scrutiny_link(self)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("measure_scrutiny_link_immutable")
        if self.measure_id and self.scrutiny_id and self.evidence_id and not self.content_fingerprint:
            from atelier.services.thematic_integrity import fingerprint_measure_scrutiny_link

            self.content_fingerprint = fingerprint_measure_scrutiny_link(self)
        super().save(*args, **kwargs)


class Comparison(Timestamped):
    class Result(models.TextChoices):
        CONVERGENCE = "convergence", "Convergence documentée"
        DIVERGENCE = "divergence", "Divergence documentée"
        EVOLUTION = "evolution", "Évolution établie"
        MIXED = "mixed", "Éléments mixtes"
        NOT_COMPARABLE = "not_comparable", "Non comparable"
        INCONCLUSIVE = "inconclusive", "Non concluant"

    stable_id = models.CharField(max_length=200, unique=True)
    declaration = models.ForeignKey(Declaration, on_delete=models.PROTECT, related_name="comparisons")
    method_version = models.CharField(max_length=80, default="DV-M0.1")
    result = models.CharField(max_length=32, choices=Result.choices)
    criteria = models.JSONField(default=dict)
    counterevidence = models.JSONField(default=list)
    scope = models.TextField()
    rationale = models.TextField()


class ComparisonAct(Timestamped):
    comparison = models.ForeignKey(Comparison, on_delete=models.CASCADE, related_name="acts")
    scrutiny = models.ForeignKey(Scrutiny, on_delete=models.PROTECT)
    included = models.BooleanField(default=True)
    reason = models.TextField()
    chronological_relation = models.CharField(max_length=40)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["comparison", "scrutiny"], name="dv_unique_comparison_act")]


class EditorialDossier(Timestamped):
    class Kind(models.TextChoices):
        OFFICIAL_VOTE = "official_vote", "Relevé officiel"
        VOTE_COMPARISON = "vote_comparison", "Comparaison factuelle"
        SPEECH_VOTE = "speech_vote", "Comparaison discours–votes"

    class State(models.TextChoices):
        DRAFT = "draft", "Brouillon"
        NEEDS_EVIDENCE = "needs_evidence", "Preuves à compléter"
        READY_FOR_REVIEW = "ready_for_review", "Prêt pour revue"
        APPROVED_REVISION = "approved_revision", "Révision approuvée"
        QUEUED = "queued", "En file d'envoi"
        DELIVERED = "delivered", "Livré"
        PUBLISHED_VERIFIED = "published_verified", "Publication vérifiée"
        SUSPENDED = "suspended", "Suspendu"
        WITHDRAWN = "withdrawn", "Écarté"

    stable_id = models.CharField(max_length=220, unique=True)
    content_kind = models.CharField(max_length=32, choices=Kind.choices)
    established_kind = models.CharField(max_length=32, choices=Kind.choices)
    data_scope = models.CharField(max_length=16, choices=[("synthetic", "Fictif"), ("real", "Réel")])
    state = models.CharField(max_length=32, choices=State.choices, default=State.DRAFT)
    current_revision = models.PositiveIntegerField(default=0)
    suspended = models.BooleanField(default=False)
    suspension_reason = models.TextField(blank=True)

    def clean(self) -> None:
        if self.pk:
            previous = EditorialDossier.objects.filter(pk=self.pk).values_list("established_kind", flat=True).first()
            if previous and previous != self.established_kind:
                raise ValidationError("La famille établie d'un dossier est immuable")
        if self.content_kind != self.established_kind:
            raise ValidationError("Une analyse ne peut pas être reclassée par simple changement d'étiquette")

    def __str__(self) -> str:
        return self.stable_id


class EditorialRevision(Timestamped):
    dossier = models.ForeignKey(EditorialDossier, on_delete=models.PROTECT, related_name="revisions")
    revision = models.PositiveIntegerField()
    comparison = models.ForeignKey(Comparison, on_delete=models.PROTECT, null=True, blank=True)
    title = models.CharField(max_length=350)
    short_answer = models.TextField()
    main_caveat = models.TextField()
    reasoning = models.TextField()
    result = models.CharField(max_length=32, choices=Comparison.Result.choices, blank=True)
    method_version = models.CharField(max_length=80, blank=True)
    source_snapshot = models.JSONField(default=list)
    public_payload = models.JSONField(default=dict)
    # Closed conclusion declaration for new dependency-aware revisions. Empty
    # historical declarations remain byte-compatible in verification snapshots.
    conclusion_contract = models.JSONField(default=dict, blank=True)
    snapshot_digest = models.CharField(max_length=64)
    canonicalization_version = models.CharField(max_length=40, default="DV-CANON-JSON-V1")
    change_summary = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    verification_required = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dossier", "revision"], name="dv_unique_dossier_revision"),
            models.UniqueConstraint(fields=["dossier", "snapshot_digest"], name="dv_unique_dossier_snapshot"),
        ]
        ordering = ["-created_at"]


class InquiryCase(Timestamped):
    input_digest = models.CharField(max_length=64,unique=True)
    query = models.CharField(max_length=500)
    corpus_version = models.CharField(max_length=64)
    candidate_evidence = models.JSONField()
    state = models.CharField(max_length=32,default='needs_evidence')


class ResearchPreparation(Timestamped):
    case = models.ForeignKey(InquiryCase,on_delete=models.PROTECT,related_name='preparations')
    preparation_digest = models.CharField(max_length=64,unique=True)
    payload = models.JSONField()
    def save(self,*args,**kwargs):
        if self.pk:raise ValidationError('research_preparation_immutable')
        super().save(*args,**kwargs)


class IndicatorContribution(Timestamped):
    revision = models.OneToOneField(EditorialRevision,on_delete=models.PROTECT,related_name='indicator_contribution')
    payload = models.JSONField()
    payload_digest = models.CharField(max_length=64)
    def save(self,*args,**kwargs):
        if self.pk:raise ValidationError('indicator_contribution_immutable_new_revision_required')
        super().save(*args,**kwargs)


class IndicatorAggregate(Timestamped):
    corpus_version = models.CharField(max_length=64)
    method_version = models.CharField(max_length=120)
    input_digest = models.CharField(max_length=64,unique=True)
    payload = models.JSONField()
    valid = models.BooleanField(default=True)


class ControlRecord(Timestamped):
    revision = models.ForeignKey(EditorialRevision, on_delete=models.PROTECT, related_name="controls")
    control_type = models.CharField(max_length=80)
    rule_version = models.CharField(max_length=80)
    input_digest = models.CharField(max_length=64)
    passed = models.BooleanField()
    anomaly_code = models.CharField(max_length=100, blank=True)
    evidence = models.TextField(blank=True)
    technical_actor = models.CharField(max_length=120)
    environment = models.CharField(max_length=16, choices=Environment.choices, default=Environment.TEST)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["revision", "control_type", "rule_version", "environment"],
                name="dv_unique_control_rule_environment",
            )
        ]


class UserProfileQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if "internal_uid" in kwargs:
            raise ValidationError("internal_uid_immutable")
        return super().update(**kwargs)


class UserProfileManager(models.Manager.from_queryset(UserProfileQuerySet)):
    pass


class UserProfile(Timestamped):
    class Role(models.TextChoices):
        ADMIN = "admin", "Administrateur"
        EDITOR = "editor", "Éditeur"
        REVIEWER = "reviewer", "Relecteur humain"
        OPERATOR = "operator", "Opérateur"
        COLLECTOR = "collector", "Compte de collecte"
        VERIFIER = "verifier", "Vérificateur documentaire"

    class AccountStatus(models.TextChoices):
        ACTIVE = "active", "Actif"
        SUSPENDED = "suspended", "Suspendu"
        REVOKED = "revoked", "Révoqué"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="dv_profile")
    role = models.CharField(max_length=24, choices=Role.choices)
    active = models.BooleanField(default=True)
    is_test_identity = models.BooleanField(default=False)
    # BUILD-003 : identité interne immuable ; login_pseudonym régénérable (= User.username).
    internal_uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    login_pseudonym = models.CharField(max_length=80, unique=True, blank=True, default="")
    public_attribution_label = models.CharField(
        max_length=80,
        blank=True,
        default="",
        help_text="Libellé d'attribution éditoriale ; jamais l'identifiant technique Site par défaut.",
    )
    account_status = models.CharField(
        max_length=16,
        choices=AccountStatus.choices,
        default=AccountStatus.ACTIVE,
    )
    must_change_password = models.BooleanField(default=False)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    session_epoch = models.PositiveIntegerField(default=0)
    permissions_extra = models.JSONField(default=list, blank=True)

    objects = UserProfileManager()

    def is_account_usable(self) -> bool:
        return (
            self.active
            and self.account_status == self.AccountStatus.ACTIVE
            and self.user.is_active
        )

    @staticmethod
    def _uid_equal(left, right) -> bool:
        if left is None and right is None:
            return True
        if left is None or right is None:
            return False
        return str(left) == str(right)

    def save(self, *args, **kwargs):
        if self.pk:
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values_list("internal_uid", flat=True)
                .first()
            )
            # Après création, internal_uid est figé (legacy null → première affectation autorisée).
            if previous is not None and not self._uid_equal(previous, self.internal_uid):
                raise ValidationError("internal_uid_immutable")
        if not self.login_pseudonym and self.user_id:
            self.login_pseudonym = self.user.username
        if self.account_status != self.AccountStatus.ACTIVE:
            self.active = False
        super().save(*args, **kwargs)


class HumanReview(Timestamped):
    class Decision(models.TextChoices):
        APPROVE = "approve", "Publier cette version"
        REQUEST_CHANGES = "request_changes", "Corriger"
        REQUEST_EVIDENCE = "request_evidence", "Demander un complément"
        REJECT = "reject", "Écarter"

    revision = models.ForeignKey(EditorialRevision, on_delete=models.PROTECT, related_name="human_reviews")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decision = models.CharField(max_length=32, choices=Decision.choices)
    reviewed_digest = models.CharField(max_length=64)
    evidence_snapshot_digest = models.CharField(max_length=64)
    proof_architecture_digest = models.CharField(max_length=64, blank=True, default='')
    environment = models.CharField(max_length=16, choices=Environment.choices)
    revoked = models.BooleanField(default=False)
    created_via = models.CharField(max_length=32, default="human_ui", editable=False)
    authority_internal_uid = models.CharField(max_length=36, blank=True, default="", editable=False)
    note = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["revision", "created_at"])]


class DetachedReviewEvidence(Timestamped):
    """Private immutable receipt of a detached signature; never contains a private key."""
    review = models.OneToOneField(HumanReview, on_delete=models.PROTECT, related_name='detached_evidence')
    bundle = models.JSONField()
    proof = models.JSONField()
    bundle_sha256 = models.CharField(max_length=64)
    key_id = models.CharField(max_length=120)
    destination = models.CharField(max_length=200)

    def save(self,*args,**kwargs):
        if self.pk:raise ValidationError('detached_review_evidence_immutable')
        super().save(*args,**kwargs)


class PublicationSnapshot(Timestamped):
    class State(models.TextChoices):
        AUTHORIZED = "authorized", "Autorisé"
        QUEUED = "queued", "En file"
        DELIVERED = "delivered", "Livré"
        VERIFIED = "verified", "Rendu vérifié"
        SUSPENDED = "suspended", "Suspendu"

    revision = models.ForeignKey(EditorialRevision, on_delete=models.PROTECT, related_name="publication_snapshots")
    environment = models.CharField(max_length=16, choices=Environment.choices, default=Environment.TEST)
    schema_version = models.CharField(max_length=80)
    policy_version = models.CharField(max_length=80)
    public_payload = models.JSONField()
    snapshot_digest = models.CharField(max_length=64)
    authorization_route = models.CharField(max_length=40)
    public_review_metadata = models.JSONField(default=dict)
    state = models.CharField(max_length=24, choices=State.choices, default=State.AUTHORIZED)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["revision", "environment"], name="dv_unique_snapshot_environment")
        ]


class OutboxEvent(Timestamped):
    class Operation(models.TextChoices):
        UPSERT = "upsert", "Créer ou mettre à jour"
        SUSPEND = "suspend", "Suspendre"

    class State(models.TextChoices):
        PENDING = "pending", "En attente"
        SENDING = "sending", "En cours"
        DELIVERED = "delivered", "Livré"
        FAILED = "failed", "Échec"

    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    dossier = models.ForeignKey(EditorialDossier, on_delete=models.PROTECT, related_name="outbox_events")
    snapshot = models.ForeignKey(PublicationSnapshot, on_delete=models.PROTECT, null=True, blank=True)
    operation = models.CharField(max_length=16, choices=Operation.choices)
    environment = models.CharField(max_length=16, choices=Environment.choices, default=Environment.TEST)
    content_revision = models.PositiveIntegerField()
    sequence = models.PositiveBigIntegerField()
    idempotency_key = models.CharField(max_length=200, unique=True)
    payload = models.JSONField()
    payload_digest = models.CharField(max_length=64)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=100, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    signing_key_id = models.CharField(max_length=80, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["dossier", "sequence"], name="dv_unique_event_sequence")]
        ordering = ["sequence"]


class RemoteExposure(Timestamped):
    """Une émission possible, indépendante de l'état et du plafond de transport.

    Conservée même après remplacement/expiration. Seul un reçu vérifié de retrait
    lié à un événement ultérieur clôt cette dette ; aucune purge par ancienneté.
    """
    event = models.OneToOneField(OutboxEvent, on_delete=models.PROTECT, related_name="remote_exposure")
    withdrawal_requested = models.BooleanField(default=False)
    suspension = models.ForeignKey(OutboxEvent, null=True, blank=True, on_delete=models.PROTECT, related_name="covered_exposures")
    resolved_at = models.DateTimeField(null=True, blank=True)
    verified_receipt = models.JSONField(default=dict)


class CatalogueTransfer(Timestamped):
    """Assemblage privé de protocole candidat, jamais activation WordPress."""
    manifest_digest = models.CharField(max_length=64, unique=True)
    manifest = models.JSONField()
    assembled_catalogue = models.JSONField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class CatalogueTransferPage(Timestamped):
    transfer = models.ForeignKey(CatalogueTransfer, on_delete=models.PROTECT, related_name="pages")
    index = models.PositiveIntegerField()
    digest = models.CharField(max_length=64)
    payload = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["transfer", "index"], name="dv_unique_catalogue_page")]


class ReceiverCursor(Timestamped):
    """État du récepteur simulé localement ; le Site réel reste propriétaire de son état."""

    record_id = models.CharField(max_length=220, unique=True)
    revision = models.PositiveIntegerField(default=0)
    sequence = models.PositiveBigIntegerField(default=0)
    snapshot_digest = models.CharField(max_length=64, blank=True)
    suspended = models.BooleanField(default=False)
    last_event_id = models.UUIDField(null=True, blank=True)
    last_event_digest = models.CharField(max_length=64, blank=True)
    active_payload = models.JSONField(default=dict)


class ReceiverEventLedger(models.Model):
    """Identité persistante de chaque événement appliqué, au-delà du dernier curseur."""
    event_id = models.UUIDField(unique=True)
    record_id = models.CharField(max_length=220)
    sequence = models.PositiveBigIntegerField()
    event_digest = models.CharField(max_length=64)
    received_at = models.DateTimeField(auto_now_add=True)


class CorrectionRequest(Timestamped):
    class State(models.TextChoices):
        OPEN = "open", "Ouvert"
        TRIAGED = "triaged", "Qualifié"
        APPLIED = "applied", "Appliqué"
        REJECTED = "rejected", "Écarté"

    dossier = models.ForeignKey(EditorialDossier, on_delete=models.PROTECT, null=True, blank=True)
    private_contact = models.TextField(blank=True)
    statement = models.TextField()
    evidence_url = models.URLField(max_length=1000, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.OPEN)
    public_reason = models.TextField(blank=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)


class Coverage(Timestamped):
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="coverage_records")
    institution = models.CharField(max_length=120)
    period_start = models.DateField()
    period_end = models.DateField()
    object_type = models.CharField(max_length=80)
    expected_count = models.PositiveIntegerField(null=True, blank=True)
    expected_state = models.CharField(max_length=24, choices=DataState.choices, default=DataState.UNKNOWN)
    retrieved_count = models.PositiveIntegerField(default=0)
    normalized_count = models.PositiveIntegerField(default=0)
    publishable_count = models.PositiveIntegerField(default=0)
    errors_count = models.PositiveIntegerField(default=0)
    exclusions = models.JSONField(default=list)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "institution", "period_start", "period_end", "object_type"],
                name="dv_unique_coverage_slice",
            )
        ]


class JobRun(Timestamped):
    class State(models.TextChoices):
        PENDING = "pending", "En attente"
        RUNNING = "running", "En cours"
        SUCCEEDED = "succeeded", "Réussi"
        FAILED = "failed", "Échec"
        CANCELLED = "cancelled", "Annulé"
        BLOCKED = "blocked", "Bloqué"

    job_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    task_name = models.CharField(max_length=160)
    batch_id = models.CharField(max_length=160)
    input_digest = models.CharField(max_length=64)
    processor_version = models.CharField(max_length=80)
    configuration = models.JSONField(default=dict)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    attempt = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    cost_units = models.DecimalField(max_digits=16, decimal_places=6, default=0)
    cost_currency = models.CharField(max_length=8, default="EUR")
    cost_limit = models.DecimalField(max_digits=16, decimal_places=6, default=0)
    error_code = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task_name", "batch_id", "input_digest", "processor_version"], name="dv_unique_job_input"
            )
        ]


class EvaluationCase(Timestamped):
    """Affectation persistante à un corpus, distincte d'une revue éditoriale."""

    class Corpus(models.TextChoices):
        TUNING = "tuning", "Réglage"
        EVALUATION = "evaluation", "Évaluation"

    stable_id = models.CharField(max_length=220, unique=True)
    dossier = models.ForeignKey(EditorialDossier, on_delete=models.PROTECT, related_name="evaluation_cases")
    corpus = models.CharField(max_length=16, choices=Corpus.choices)
    label = models.CharField(max_length=32, choices=Comparison.Result.choices)
    label_source = models.CharField(
        max_length=40,
        choices=[("human_test", "Humain de test"), ("source_rule", "Règle sourcée")],
    )
    assignment_digest = models.CharField(max_length=64)
    frozen = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dossier"], name="dv_unique_dossier_corpus_assignment")
        ]


class AuditEvent(models.Model):
    occurred_at = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    event_type = models.CharField(max_length=100)
    object_type = models.CharField(max_length=80)
    object_id = models.CharField(max_length=220)
    detail = models.JSONField(default=dict)
    request_id = models.UUIDField(default=uuid.uuid4)

    class Meta:
        indexes = [models.Index(fields=["object_type", "object_id", "occurred_at"])]


class FeedExclusion(Timestamped):
    """Observed loss of authority is latched for this revision, even after rollback."""
    event = models.OneToOneField(OutboxEvent,on_delete=models.PROTECT,related_name='feed_exclusion')
    reason = models.CharField(max_length=100)
    content_revision = models.PositiveIntegerField()

    def save(self,*args,**kwargs):
        if self.pk:raise ValidationError('feed_exclusion_immutable')
        super().save(*args,**kwargs)


class SitePublicationConfirmation(Timestamped):
    """Candidate acknowledgement of publication, separate from transport receipt."""
    event = models.OneToOneField(OutboxEvent,on_delete=models.PROTECT,related_name='publication_confirmation')
    statement = models.JSONField()
    signature = models.TextField()
    statement_digest = models.CharField(max_length=64)

    def save(self,*args,**kwargs):
        if self.pk:raise ValidationError('publication_confirmation_immutable')
        return super().save(*args,**kwargs)


class LoginThrottle(models.Model):
    """Compteur anti-bruteforce sans identifiant réseau (compte ou bucket global).

    ``window_started_at`` borne la fenêtre de cumul des échecs ; à expiration du
    verrou ou de la fenêtre, le compteur est réinitialisé (BUILD-003 R2).
    """

    key_digest = models.CharField(max_length=64, unique=True)
    failures = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    window_started_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class AccountInviteToken(Timestamped):
    """Jeton d'invitation à usage unique — seul le condensat est persisté."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dv_invite_tokens",
    )
    token_digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dv_invites_created",
    )

    class Meta:
        indexes = [models.Index(fields=["expires_at", "used_at"])]


class FeedCursor(Timestamped):
    """Curseur d'alimentation permanente par flux catalogue (BUILD-002)."""

    class Cadence(models.TextChoices):
        FAST = "fast", "Rapide"
        HOURLY = "hourly", "Horaire"
        DAILY = "daily", "Quotidien"
        NIGHTLY = "nightly", "Réconciliation nocturne"
        MANUAL = "manual", "Manuel / figé"

    class BlockReason(models.TextChoices):
        NONE = "", "Aucun"
        CREDENTIALS = "blocked_credentials_or_provider_terms", "Identifiants ou conditions fournisseur"
        NETWORK = "network_disabled", "Réseau désactivé"
        RIGHTS = "rights_forbidden", "Droits interdits"
        PROVIDER = "provider_unavailable", "Fournisseur indisponible"
        CONFIG = "configuration_incomplete", "Configuration incomplète"
        NOT_IMPLEMENTED = "blocked_adapter_not_implemented", "Adaptateur non implémenté"

    class ExecutionMode(models.TextChoices):
        LIVE = "live", "Live"
        FIXTURE = "fixture", "Fixture"
        BLOCKED = "blocked", "Bloqué"
        CONFIGURED_NOT_RUN = "configured_not_run", "Configuré non exécuté"

    feed_id = models.CharField(max_length=120, unique=True)
    source = models.ForeignKey(Source, on_delete=models.PROTECT, null=True, blank=True, related_name="feed_cursors")
    family = models.CharField(max_length=80)
    authority = models.CharField(max_length=120)
    cadence = models.CharField(max_length=24, choices=Cadence.choices, default=Cadence.DAILY)
    mechanism = models.CharField(max_length=80, blank=True)
    auth_required = models.BooleanField(default=False)
    ingestion_active = models.BooleanField(default=False)
    cursor_token = models.CharField(max_length=500, blank=True)
    etag = models.CharField(max_length=500, blank=True)
    last_modified = models.CharField(max_length=500, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_live_success_at = models.DateTimeField(null=True, blank=True)
    last_fixture_success_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=120, blank=True)
    consecutive_errors = models.PositiveIntegerField(default=0)
    coverage_note = models.CharField(max_length=500, blank=True)
    block_reason = models.CharField(max_length=64, choices=BlockReason.choices, blank=True, default="")
    execution_mode = models.CharField(
        max_length=32, choices=ExecutionMode.choices, default=ExecutionMode.CONFIGURED_NOT_RUN
    )
    last_run_provenance = models.CharField(max_length=120, blank=True)
    metrics = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["authority", "feed_id"]

    def __str__(self) -> str:
        return self.feed_id


class DetailWorkItem(Timestamped):
    """File persistante discovery → détail (BUILD-002-R5).

    Indépendante du parse_cursor snapshot : un détail différé ne disparaît pas
    quand le curseur raw avance.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "En attente"
        FETCHED = "fetched", "Fetché"
        PARSED = "parsed", "Parsé (discours extrait)"
        INCOMPLETE = "incomplete", "Incomplet (pas de citation démontrée)"
        RETRYABLE_ERROR = "retryable_error", "Erreur réessayable"
        BLOCKED_PERMANENT = "blocked_permanent", "Bloqué permanent"

    TERMINAL_STATUSES = frozenset(
        {
            Status.PARSED,
            Status.INCOMPLETE,
            Status.BLOCKED_PERMANENT,
        }
    )

    feed_id = models.CharField(max_length=120, db_index=True)
    parser_id = models.CharField(max_length=80)
    canonical_url = models.URLField(max_length=1000)
    work_key = models.CharField(max_length=64, unique=True)
    parent_document = models.ForeignKey(
        DocumentVersion,
        on_delete=models.PROTECT,
        related_name="detail_work_children",
        null=True,
        blank=True,
    )
    detail_document = models.ForeignKey(
        DocumentVersion,
        on_delete=models.SET_NULL,
        related_name="detail_work_as_evidence",
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=120, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    execution_mode = models.CharField(max_length=32, blank=True)
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["feed_id", "status", "next_attempt_at"], name="dv_detail_work_due_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.feed_id}:{self.status}:{self.canonical_url[:60]}"

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES


class RelationCandidate(Timestamped):
    """Candidat de rapprochement non qualifié (jamais une conclusion)."""

    class Kind(models.TextChoices):
        DECLARATION_MEASURE = "declaration_measure", "Parole → mesure"
        MEASURE_SCRUTINY = "measure_scrutiny", "Mesure → scrutin"

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposé"
        DISMISSED = "dismissed", "Écarté"
        PROMOTED = "promoted", "Promu en qualification explicite"

    stable_id = models.CharField(max_length=220, unique=True)
    kind = models.CharField(max_length=40, choices=Kind.choices)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PROPOSED)
    left_object_type = models.CharField(max_length=80)
    left_object_id = models.CharField(max_length=220)
    right_object_type = models.CharField(max_length=80)
    right_object_id = models.CharField(max_length=220)
    rationale = models.TextField(blank=True)
    score_hint = models.FloatField(null=True, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    processor_version = models.CharField(max_length=80, default="dv-relation-candidate/1")

    class Meta:
        indexes = [
            models.Index(fields=["kind", "status"]),
            models.Index(fields=["left_object_type", "left_object_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.stable_id}"


class ExplainEntry(Timestamped):
    """Fiche pédagogique sourcée, versionnée, consommable plus tard par le Site."""

    class Institution(models.TextChoices):
        ASSEMBLEE = "assemblee_nationale", "Assemblée nationale"
        SENAT = "senat", "Sénat"
        BICAMERAL = "bicameralisme", "Bicamérisme / navette"
        GOUVERNEMENT = "gouvernement", "Gouvernement"
        PRESIDENCE = "presidence", "Présidence de la République"
        PROCEDURE = "procedure", "Procédure législative"
        OTHER = "other", "Autre institution (registry)"

    stable_id = models.CharField(max_length=160)
    version = models.PositiveIntegerField(default=1)
    institution = models.CharField(max_length=40, choices=Institution.choices)
    role_or_procedure = models.CharField(max_length=160)
    title = models.CharField(max_length=250)
    short_text = models.TextField()
    detailed_text = models.TextField()
    official_sources = models.JSONField(default=list, blank=True)
    verified_on = models.DateField(null=True, blank=True)
    limits_and_variants = models.TextField(blank=True)
    dependencies = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["stable_id", "version"], name="dv_unique_explain_version"),
        ]
        ordering = ["institution", "stable_id", "-version"]

    def __str__(self) -> str:
        return f"{self.stable_id}@v{self.version}"


class StoredBlob(Timestamped):
    """Octets physiques dédupliqués, distincts des métadonnées légères."""

    class RetentionClass(models.TextChoices):
        LIGHT = "light", "Léger"
        MEDIUM = "medium", "Moyen"
        HEAVY = "heavy", "Lourd"

    class Lifecycle(models.TextChoices):
        ACTIVE = "active", "Actif"
        QUARANTINE = "quarantine", "Quarantaine"
        PURGE_ELIGIBLE = "purge_eligible", "Éligible à purge"
        TOMBSTONED = "tombstoned", "Tombstone après purge"

    content_hash = models.CharField(max_length=64, unique=True)
    byte_size = models.PositiveBigIntegerField()
    media_type = models.CharField(max_length=160)
    retention_class = models.CharField(max_length=16, choices=RetentionClass.choices, default=RetentionClass.LIGHT)
    lifecycle = models.CharField(max_length=24, choices=Lifecycle.choices, default=Lifecycle.ACTIVE)
    storage_path = models.CharField(max_length=1000, blank=True)
    source_url = models.URLField(max_length=1000, blank=True)
    rights_note = models.CharField(max_length=250, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"{self.retention_class}:{self.content_hash[:12]}"


class BlobReference(Timestamped):
    """Lien conservateur d'un objet métier vers un blob partagé."""

    class Conserving(models.TextChoices):
        ACTIVE = "active", "Actif conservateur"
        REJECTED = "rejected", "Dossier rejeté"
        ARCHIVAL = "archival", "Obligation d'audit / révision"
        RELEASED = "released", "Lien libéré"

    blob = models.ForeignKey(StoredBlob, on_delete=models.PROTECT, related_name="references")
    object_type = models.CharField(max_length=80)
    object_id = models.CharField(max_length=220)
    role = models.CharField(max_length=80, blank=True)
    conserving_status = models.CharField(max_length=24, choices=Conserving.choices, default=Conserving.ACTIVE)
    rejection_reason = models.CharField(max_length=250, blank=True)
    # Date métier du rejet (QuerySet.update n'actualise pas auto_now ; ne pas réutiliser updated_at).
    rejected_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["blob", "object_type", "object_id", "role"],
                name="dv_unique_blob_ref",
            )
        ]
        indexes = [models.Index(fields=["object_type", "object_id"])]


class BlobTombstone(Timestamped):
    """Preuve minimale conservée après purge d'un blob heavy."""

    content_hash = models.CharField(max_length=64, unique=True)
    byte_size = models.PositiveBigIntegerField()
    media_type = models.CharField(max_length=160)
    source_url = models.URLField(max_length=1000, blank=True)
    useful_excerpt = models.TextField(blank=True)
    timecodes = models.JSONField(default=list, blank=True)
    provenance = models.JSONField(default=dict, blank=True)
    rejection_reason = models.CharField(max_length=250, blank=True)
    purged_at = models.DateTimeField()
    dry_run = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"tombstone:{self.content_hash[:12]}"


class IngestError(Timestamped):
    """File d'erreurs d'ingestion avec retry borné."""

    feed_id = models.CharField(max_length=120, blank=True)
    stage = models.CharField(max_length=80)
    error_code = models.CharField(max_length=120)
    detail = models.JSONField(default=dict, blank=True)
    attempt = models.PositiveIntegerField(default=1)
    max_attempts = models.PositiveIntegerField(default=5)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["resolved_at", "next_retry_at"])]

from .verification_models import VerificationJob, VerificationAttempt, VerificationReceipt, VerificationDelivery
from .provenance_models import ProofArtifact, ProvenanceCertificate, AffiliationObservation, ScrutinyTextBinding
