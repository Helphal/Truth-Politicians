"""Validation locale du contrat candidat partagé avec le fil Site.

Le schéma reste candidat tant qu'un récepteur WordPress réel ne l'a pas
accepté. Cette validation ne crée aucune autorisation éditoriale.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from jsonschema import Draft202012Validator, FormatChecker


_EVENT_SCHEMA_FILES = {
    "dv.event/0.2.0-draft.1": "dv-event-0.2.0-draft.1.schema.json",
    "dv.event/0.2.0-racc001-draft.1": "dv-event-0.2.0-racc001-draft.1.schema.json",
}

_SCHEMA_FILES = {
    "dv.content/0.6.0-provenance-draft.1": "dv-content-0.6.0-provenance-draft.1.schema.json",
    "dv.content/0.7.0-conclusion-draft.1": "dv-content-0.7.0-conclusion-draft.1.schema.json",
    "dv.content/0.5.0-diffusion-draft.1": "dv-content-0.5.0-diffusion-draft.1.schema.json",
    "dv.content/0.4.0-dvverite-draft.1": "dv-content-0.4.0-dvverite-draft.1.schema.json",
    "dv.content/0.4.0-draft.2": "dv-content-0.4.0-draft.2.schema.json",
    "dv.content/0.4.0-draft.3": "dv-content-0.4.0-draft.3.schema.json",
    "dv.content/0.4.0-racc001-draft.1": "dv-content-0.4.0-racc001-draft.1.schema.json",
}


def _schema_path_for_version(version: str) -> Path:
    name = _SCHEMA_FILES.get(version)
    if not name:
        name = _SCHEMA_FILES.get(settings.DV_PUBLICATION_SCHEMA, "dv-content-0.4.0-racc001-draft.1.schema.json")
    return Path(settings.BASE_DIR) / "contracts" / name


@lru_cache(maxsize=4)
def publication_schema_for(version: str) -> dict:
    path = _schema_path_for_version(version)
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("publication_schema_unavailable") from exc
    Draft202012Validator.check_schema(schema)
    return schema


def publication_schema() -> dict:
    return publication_schema_for(settings.DV_PUBLICATION_SCHEMA)


@lru_cache(maxsize=4)
def publication_validator_for(version: str) -> Draft202012Validator:
    return Draft202012Validator(publication_schema_for(version), format_checker=FormatChecker())


def publication_validator() -> Draft202012Validator:
    return publication_validator_for(settings.DV_PUBLICATION_SCHEMA)


def validate_contract_envelope(envelope: dict) -> None:
    version = envelope.get("schema_version") if isinstance(envelope, dict) else None
    validator = publication_validator_for(version or settings.DV_PUBLICATION_SCHEMA)
    errors = sorted(
        validator.iter_errors(envelope),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "$"
        raise ValidationError(f"contract_invalid:{location}:{error.validator}")


def validate_contract_schema() -> None:
    """Charge et contrôle le méta-schéma ; utile dans les commandes de recette."""
    Draft202012Validator.check_schema(publication_schema())


def validate_speech_vote_payload(payload: dict) -> None:
    """Full public contract, independent of envelope and editorial permission.

    Incomplete drafts can be saved, but new verified real revisions cannot
    cross the publication gate with a whitelist-only validation.
    """
    version = public_contract_version(payload)
    if version == "dv.content/0.5.0-diffusion-draft.1":
        from .diffusion import validate_diffusion_selection
        validate_diffusion_selection(payload)
    elif version in {"dv.content/0.6.0-provenance-draft.1", "dv.content/0.7.0-conclusion-draft.1"} and 'diffusion_selection' in payload:
        from .diffusion import validate_diffusion_selection
        validate_diffusion_selection(payload)
    schema = publication_schema_for(version)
    validator = Draft202012Validator(
        {"$defs": schema["$defs"], "$ref": "#/$defs/speechVote"},
        format_checker=FormatChecker(),
    )
    errors = sorted(validator.iter_errors(payload),
                    key=lambda error: tuple(str(item) for item in error.absolute_path))
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "$"
        raise ValidationError(f"public_contract_invalid:{location}:{error.validator}")


def public_contract_version(payload: dict) -> str:
    version = payload.get('public_contract_version', 'dv.content/0.4.0-racc001-draft.1')
    if version not in {'dv.content/0.4.0-racc001-draft.1', 'dv.content/0.4.0-dvverite-draft.1',
                       'dv.content/0.5.0-diffusion-draft.1', 'dv.content/0.6.0-provenance-draft.1',
                       'dv.content/0.7.0-conclusion-draft.1'}:
        raise ValidationError('public_contract_unsupported')
    return version


def validate_semantic_envelope(envelope):
    """Invariants vérifiables par le Site sans accès à la base privée du producteur."""
    from .factual_checks import counts_check, calculation_check
    from .canonical import sha256_digest
    from .publication import validate_public_payload
    public = envelope["public"]
    kind = envelope["content_kind"]
    validate_public_payload(kind, public)
    if kind in {"official_vote", "vote_comparison"}:
        good, reason = (counts_check if kind == "official_vote" else calculation_check)(public)
        if not good:
            raise ValidationError(reason)
    known_sources = {sha256_digest(item) for item in envelope["source_versions"]}
    def inspect(value):
        if isinstance(value, dict):
            if "source_version" in value and sha256_digest(value["source_version"]) not in known_sources:
                raise ValidationError("nested_source_not_in_envelope")
            for item in value.values():
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
    inspect(public)
    catalogue = public.get("catalogue")
    if catalogue:
        identifiers = {}
        for family, key in (("persons", "id"), ("organizations", "id"), ("mandates", "id"), ("text_versions", "id"), ("legislative_dossiers", "id"), ("scrutinies", "scrutin_id")):
            values = [item[key] for item in catalogue[family]]
            if len(set(values)) != len(values):
                raise ValidationError("catalogue_duplicate_identity")
            identifiers[family] = set(values)
        if identifiers["scrutinies"] != set(catalogue["scope_scrutiny_ids"]):
            raise ValidationError("catalogue_scope_mismatch")
        for item in catalogue["vote_observations"]:
            if item["person_id"] not in identifiers["persons"] or item["scrutiny_id"] not in identifiers["scrutinies"]:
                raise ValidationError("catalogue_observation_dangling")
            for key, family in (("mandate_id", "mandates"), ("affiliation_at_event_id", "organizations")):
                if item.get(key) and item[key] not in identifiers[family]:
                    raise ValidationError("catalogue_observation_relation_dangling")
            if item.get("mandate_id"):
                from .mandate_links import validate_mandate_link
                mandate = next(m for m in catalogue["mandates"] if m["id"] == item["mandate_id"])
                scrutiny = next(s for s in catalogue["scrutinies"] if s["scrutin_id"] == item["scrutiny_id"])
                validate_mandate_link(person_id=item["person_id"], mandate_person_id=mandate["person_id"],
                    institution=scrutiny["institution"], mandate_institution=mandate["institution"],
                    occurred_on=scrutiny["occurred_on"], starts_on=mandate["starts_on"],
                    ends_on=mandate.get("ends_on"), date_precision=scrutiny["date_precision"])
        for item in catalogue["affiliations"]:
            if item["person_id"] not in identifiers["persons"] or item["organization_id"] not in identifiers["organizations"]:
                raise ValidationError("catalogue_affiliation_dangling")
            if item.get("ends_on") and item.get("starts_on") and item["ends_on"] < item["starts_on"]:
                raise ValidationError("catalogue_affiliation_interval_invalid")
        for item in catalogue["mandates"]:
            if item["person_id"] not in identifiers["persons"] or (item.get("ends_on") and item["ends_on"] < item["starts_on"]):
                raise ValidationError("catalogue_mandate_invalid")
        for item in catalogue["text_versions"]:
            if item["dossier_id"] not in identifiers["legislative_dossiers"]:
                raise ValidationError("catalogue_text_dangling")
        page = catalogue["pagination"]
        if page["returned_count"] != len(catalogue["vote_observations"]) or page["total_count_in_materialized_scope"] != page["returned_count"]:
            raise ValidationError("catalogue_pagination_mismatch")
        for item in catalogue["scrutinies"]:
            if not counts_check(item)[0]:
                raise ValidationError("catalogue_counts_inconsistent")
            if not any(t["official_url"] == item["official_text_url"] and t["relation"] == "submitted" for t in catalogue["text_versions"]):
                raise ValidationError("catalogue_submitted_text_missing")
        for item in public.get("acts", []):
            if item["scrutiny_id"] not in identifiers["scrutinies"]:
                raise ValidationError("comparison_act_dangling")
        if kind == "official_vote":
            matching = [item for item in catalogue["scrutinies"] if item["scrutin_id"] == public["scrutin_id"]]
            if len(matching) != 1 or any(public[key] != value for key, value in matching[0].items() if key != "source_version"):
                raise ValidationError("official_vote_catalogue_mismatch")
        if kind == "vote_comparison":
            for observation in public["observations"]:
                if observation["scrutin_id"] not in identifiers["scrutinies"]:
                    raise ValidationError("comparison_scope_dangling")
                for side in ("left", "right"):
                    person = observation.get(side + "_person_id")
                    basis = observation.get(side + "_basis")
                    if person:
                        positions = {vote["normalized_category"] for vote in catalogue["vote_observations"] if vote["person_id"] == person and vote["scrutiny_id"] == observation["scrutin_id"]}
                        if positions != {observation[side]}:
                            raise ValidationError("comparison_position_not_bound")
                    elif not basis or basis["kind"] != "reference_category" or basis["category"] != observation[side]:
                        raise ValidationError("comparison_basis_not_bound")
        attribution = public.get("attribution", {})
        for key, family in (("person_id", "persons"), ("organization_id", "organizations")):
            if attribution.get(key) and attribution[key] not in identifiers[family]:
                raise ValidationError("attribution_dangling")
    if kind == "speech_vote" and public.get("evidence"):
        if not any(public["declaration_quote"] in item["exact_quote"] for item in public["evidence"]):
            raise ValidationError("quote_not_supported_by_frozen_evidence")


def event_schema_filename(version: str | None = None) -> str:
    version = version or settings.DV_EVENT_SCHEMA
    name = _EVENT_SCHEMA_FILES.get(version)
    if not name:
        raise ValidationError("event_schema_unsupported")
    return name


@lru_cache(maxsize=2)
def event_schema_for(version: str) -> dict:
    path = Path(settings.BASE_DIR) / "contracts" / event_schema_filename(version)
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("event_schema_unavailable") from exc
    Draft202012Validator.check_schema(schema)
    return schema


def event_schema() -> dict:
    return event_schema_for(settings.DV_EVENT_SCHEMA)


@lru_cache(maxsize=2)
def event_validator_for(version: str) -> Draft202012Validator:
    return Draft202012Validator(event_schema_for(version), format_checker=FormatChecker())


def event_validator() -> Draft202012Validator:
    return event_validator_for(settings.DV_EVENT_SCHEMA)


def validate_event_packet_schema(packet: dict) -> None:
    version = packet.get("event_schema_version") or settings.DV_EVENT_SCHEMA
    errors = sorted(
        event_validator_for(version).iter_errors(packet),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "$"
        raise ValidationError(f"event_contract_invalid:{location}:{error.validator}")
    if packet.get("operation") == "upsert":
        validate_contract_envelope(packet["payload"])
