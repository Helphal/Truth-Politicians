"""Lots complets, immuables et bornés du catalogue ; récepteur LOCAL candidat.

Un scrutin peut traverser plusieurs pages. Aucune page n'est une vue publiée.
Le Site doit adopter/implémenter ce protocole avant tout raccordement réel.
"""
from copy import deepcopy
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from atelier.models import CatalogueTransfer, CatalogueTransferPage, CatalogueScopeVersion
from .canonical import canonical_bytes, sha256_digest
from .catalogue import build_catalogue
from .publication import sign_packet, verify_packet_signature

FORMAT = "dv.catalogue-transfer/0.1.0-draft.1"
VERSIONED_FORMAT = "dv.catalogue-transfer/0.1.0-draft.2"
FAMILIES = ("persons", "organizations", "affiliations", "mandates", "text_versions",
            "legislative_dossiers", "scrutinies", "vote_observations")
MAX_OBJECTS = 25000
MAX_PAGES = 256
MAX_BYTES = 2 * 1024 * 1024


@transaction.atomic
def build_catalogue_transfer(scrutiny_ids, *, page_size=250, generation=None):
    if generation is not None and (type(generation) is not int or not 1 <= generation <= 9007199254740991):
        raise ValidationError("catalogue_generation_invalid")
    wire_format = VERSIONED_FORMAT if generation is not None else FORMAT
    if type(page_size) is not int or not 1 <= page_size <= 250:
        raise ValidationError("catalogue_page_size_invalid")
    # L'ancien constructeur et son défaut de capacité restent explicitement
    # bornés à 250 par défaut (Q06). Nouvelle voie, complétude vérifiée ci-dessous.
    catalogue, sources = build_catalogue(scrutiny_ids, limit=10000)
    if sum(len(catalogue[f]) for f in FAMILIES) > MAX_OBJECTS:
        raise ValidationError("catalogue_transfer_scope_too_large")
    snapshot = sha256_digest(catalogue)
    objects = [{"family": family, "record": record} for family in FAMILIES for record in catalogue[family]]
    pages = []
    while objects:
        part = objects[:page_size]
        page = {"format": wire_format, "snapshot_sha256": snapshot, "index": len(pages), "objects": part}
        while len(canonical_bytes(page)) > MAX_BYTES and len(part) > 1:
            part = part[:len(part)//2]
            page["objects"] = part
        if len(canonical_bytes(page)) > MAX_BYTES or len(pages) >= MAX_PAGES:
            raise ValidationError("catalogue_page_bounds_exceeded")
        pages.append(deepcopy(page))
        objects = objects[len(part):]
    manifest = {"format": wire_format, "environment": "test", "destination": "dv-site:test",
        "key_id": settings.DV_EVENT_KEY_ID, "snapshot_sha256": snapshot,
        "source_versions": sources, "scope_scrutiny_ids": catalogue["scope_scrutiny_ids"],
        "family_counts": {f:len(catalogue[f]) for f in FAMILIES},
        "page_sha256": [sha256_digest(p) for p in pages],
        "catalogue_metadata": {k:v for k,v in catalogue.items() if k not in FAMILIES}}
    if generation is not None:
        manifest["generation"] = generation
    if len(canonical_bytes(manifest)) > MAX_BYTES:
        raise ValidationError("catalogue_manifest_too_large")
    return {**manifest, "signature": sign_packet(manifest)}, pages


def _validate_manifest(manifest):
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator, FormatChecker
    schema = json.loads((Path(settings.BASE_DIR)/"contracts/dv-catalogue-transfer-0.1.0-draft.1.schema.json").read_text(encoding="utf-8"))
    versioned = isinstance(manifest,dict) and manifest.get('format') == VERSIONED_FORMAT
    if versioned:
        schema = json.loads((Path(settings.BASE_DIR)/'contracts/dv-catalogue-transfer-0.1.0-draft.2.schema.json').read_text(encoding='utf-8'))
    if list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(manifest)):
        raise ValidationError("catalogue_manifest_schema_invalid")
    if settings.DV_ENV not in {"local", "test"}:
        raise ValidationError("catalogue_receiver_local_only")
    fields = {"format", "environment", "destination", "key_id", "snapshot_sha256", "source_versions",
              "scope_scrutiny_ids", "family_counts", "page_sha256", "catalogue_metadata", "signature"}
    if versioned:fields.add('generation')
    if not isinstance(manifest, dict) or set(manifest) != fields or len(canonical_bytes(manifest)) > MAX_BYTES:
        raise ValidationError("catalogue_manifest_invalid")
    if manifest["format"] not in {FORMAT,VERSIONED_FORMAT} or manifest["environment"] != "test" or manifest["destination"] != "dv-site:test":
        raise ValidationError("catalogue_manifest_wrong_destination")
    unsigned = {k:v for k,v in manifest.items() if k != "signature"}
    if not verify_packet_signature(unsigned, manifest["signature"]):
        raise ValidationError("catalogue_manifest_unauthorized")
    counts, hashes = manifest["family_counts"], manifest["page_sha256"]
    if not isinstance(counts, dict) or set(counts) != set(FAMILIES) or any(type(v) is not int or v < 0 for v in counts.values()) or sum(counts.values()) > MAX_OBJECTS:
        raise ValidationError("catalogue_manifest_counts_invalid")
    import re
    if not isinstance(hashes, list) or not 1 <= len(hashes) <= MAX_PAGES or any(not isinstance(h, str) or not re.fullmatch("[a-f0-9]{64}", h) for h in hashes):
        raise ValidationError("catalogue_manifest_pages_invalid")


def missing_pages(transfer):
    seen = set(transfer.pages.values_list("index", flat=True))
    return [i for i in range(len(transfer.manifest["page_sha256"])) if i not in seen]


@transaction.atomic
def receive_catalogue_page(manifest, page):
    _validate_manifest(manifest)
    if not isinstance(page, dict) or set(page) != {"format", "snapshot_sha256", "index", "objects"}:
        raise ValidationError("catalogue_page_invalid")
    index = page["index"]
    if type(index) is not int or not 0 <= index < len(manifest["page_sha256"]):
        raise ValidationError("catalogue_page_index_invalid")
    if page["format"] != manifest['format'] or page["snapshot_sha256"] != manifest["snapshot_sha256"]:
        raise ValidationError("catalogue_page_wrong_snapshot")
    if not isinstance(page["objects"], list) or not 1 <= len(page["objects"]) <= 250 or len(canonical_bytes(page)) > MAX_BYTES:
        raise ValidationError("catalogue_page_bounds_exceeded")
    if sha256_digest(page) != manifest["page_sha256"][index]:
        raise ValidationError("catalogue_page_hash_mismatch")
    for item in page["objects"]:
        if not isinstance(item, dict) or set(item) != {"family", "record"} or item["family"] not in FAMILIES or not isinstance(item["record"], dict):
            raise ValidationError("catalogue_page_object_invalid")
    transfer, _ = CatalogueTransfer.objects.get_or_create(manifest_digest=sha256_digest(manifest), defaults={"manifest": manifest})
    transfer = CatalogueTransfer.objects.select_for_update().get(pk=transfer.pk)
    saved_page,created = CatalogueTransferPage.objects.get_or_create(transfer=transfer, index=index, defaults={"payload": page, "digest": sha256_digest(page)})
    if not created and (saved_page.payload != page or saved_page.digest != sha256_digest(page)):
        raise ValidationError('catalogue_saved_page_conflict')
    missing = missing_pages(transfer)
    if not missing and transfer.completed_at is None:
        catalogue = deepcopy(manifest["catalogue_metadata"])
        if set(catalogue) != {"scope_scrutiny_ids", "pagination", "exclusions"} or catalogue["scope_scrutiny_ids"] != manifest["scope_scrutiny_ids"]:
            raise ValidationError("catalogue_metadata_invalid")
        catalogue.update({f:[] for f in FAMILIES})
        for saved in transfer.pages.order_by("index"):
            for item in saved.payload["objects"]:
                catalogue[item["family"]].append(item["record"])
        if sha256_digest(catalogue) != manifest["snapshot_sha256"] or any(len(catalogue[f]) != manifest["family_counts"][f] for f in FAMILIES):
            raise ValidationError("catalogue_assembly_mismatch")
        _validate_assembled(catalogue, manifest["source_versions"])
        transfer.assembled_catalogue, transfer.completed_at = catalogue, timezone.now()
        transfer.save(update_fields=["assembled_catalogue", "completed_at", "updated_at"])
    activation = 'not_requested'
    if not missing and manifest['format'] == VERSIONED_FORMAT:
        activation = _activate_scoped_snapshot(transfer)
    return {"transfer_id": transfer.manifest_digest, "missing_pages": missing,
            "complete_locally": not missing, "local_scope_activation":activation, "site_activated": False}


def _activate_scoped_snapshot(transfer):
    """Atomic scope pointers to immutable catalogues; never overwrite shared person rows.

    Consumers resolve a person/group WITH its scope/transfer digest. There is no
    global last-writer-wins label. A late page never resumes a suspended scope.
    """
    manifest=transfer.manifest;states=[]
    for scope_id in sorted(manifest['scope_scrutiny_ids']):
        state,_=CatalogueScopeVersion.objects.get_or_create(scope_id=scope_id)
        states.append(CatalogueScopeVersion.objects.select_for_update().get(pk=state.pk))
    if any(s.suspended for s in states):return 'suspension_latched'
    if any(s.generation>manifest['generation'] for s in states):return 'stale_generation'
    if any(s.generation==manifest['generation'] and s.transfer_id!=transfer.pk for s in states):return 'generation_conflict'
    if all(s.generation==manifest['generation'] and s.transfer_id==transfer.pk for s in states):return 'duplicate'
    for state in states:
        state.generation=manifest['generation'];state.transfer=transfer
        state.save(update_fields=['generation','transfer','updated_at'])
    return 'activated_locally_immutable_scope'


def _validate_assembled(catalogue, sources):
    # Réutiliser les schémas des familles, mais pas le plafond monolithique.
    from .contracts import publication_schema, validate_semantic_envelope
    from jsonschema import Draft202012Validator, FormatChecker
    schema = publication_schema()
    definition = deepcopy(schema["$defs"]["catalogue"])
    definition["properties"]["vote_observations"]["maxItems"] = 10000
    definition["properties"]["pagination"]["properties"]["limit"]["maximum"] = 10000
    # Tous les tableaux de familles restent globalement bornés par MAX_OBJECTS.
    for family in FAMILIES:
        definition["properties"][family]["maxItems"] = MAX_OBJECTS
    validator = Draft202012Validator({**schema, **definition}, format_checker=FormatChecker())
    if list(validator.iter_errors(catalogue)):
        raise ValidationError("catalogue_assembled_schema_invalid")
    identities = set()
    for vote in catalogue["vote_observations"]:
        key = (vote["scrutiny_id"], vote["person_id"], sha256_digest(vote["source_version"]))
        if key in identities:
            raise ValidationError("catalogue_duplicate_vote")
        identities.add(key)
    if not catalogue["scrutinies"]:
        raise ValidationError("catalogue_empty_scope")
    public = {k:v for k,v in catalogue["scrutinies"][0].items() if k != "source_version"}
    public["catalogue"] = catalogue
    validate_semantic_envelope({"content_kind":"official_vote", "public":public, "source_versions":sources})
