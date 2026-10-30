"""Déplier les relevés nominatifs déjà conservés, sans inventer noms/mandats."""
from collections import Counter
from datetime import date
from django.core.exceptions import ValidationError
from django.db import transaction
from atelier.models import AffiliationObservation, Person, Organization, Scrutiny, VoteObservation
from .parsers.assembly import iter_archive_records, iter_vote_observations, normalize_scrutiny, PARSER_VERSION
from .canonical import sha256_digest
from .catalogue import source_ref


@transaction.atomic
def expand_materialized_votes(document, archive_path):
    import hashlib
    if document.quarantined or hashlib.sha256(archive_path.read_bytes()).hexdigest() != document.sha256:
        raise ValidationError("assembly_archive_not_pinned")
    selected = {(s.legislature, s.source_identifier):s for s in Scrutiny.objects.filter(
        institution="Assemblée nationale").select_related("source_document")}
    seen, added, seen_records, anomalies, rows, dates = set(), 0, 0, [], [], []
    unresolved_mandates = set()
    for raw in iter_archive_records(archive_path):
        seen_records += 1
        try:
            normalized = normalize_scrutiny(raw)
            dates.append(normalized["occurred_on"])
        except (ValueError, TypeError, KeyError):
            anomalies.append({"record_index":seen_records, "reason":"invalid_scrutiny_record"})
            continue
        if normalized["institution"] != "Assemblée nationale":
            anomalies.append({"source_uid":normalized["source_uid"], "reason":"separate_congress_namespace_not_imported_as_assembly"})
            continue
        scrutiny = selected.get((normalized["legislature"], normalized["source_identifier"]))
        if scrutiny is None or scrutiny.source_document_id != document.pk:
            continue
        if normalized["official_title"] != scrutiny.official_title or date.fromisoformat(normalized["occurred_on"]) != scrutiny.occurred_on:
            anomalies.append({"scrutiny_id":scrutiny.stable_id, "source_uid":normalized["source_uid"],
                "reason":"existing_materialized_record_mismatch_not_overwritten"})
            continue
        observations = list(iter_vote_observations(raw))
        if len({v["actor_ref"] for v in observations}) != len(observations):
            raise ValidationError("assembly_duplicate_actor_observation")
        counts = Counter()
        for item in observations:
            actor = item["actor_ref"]
            person, _ = Person.objects.get_or_create(stable_id="AN:PERSON:"+actor, defaults={
                "display_name":"Identité AN " + actor + " (nom non résolu)",
                "identity_status":"unresolved", "institutional_ids":{"assemblee_nationale":actor}})
            if person.institutional_ids.get("assemblee_nationale") != actor:
                raise ValidationError("assembly_person_identifier_conflict")
            group = None
            if item["group_ref"]:
                group, _ = Organization.objects.get_or_create(stable_id="AN:ORG:"+item["group_ref"], defaults={
                    "name":"Organe AN " + item["group_ref"] + " (libellé non résolu)", "kind":"parliamentary_group"})
                locator = {"format": "assembly-scrutiny-voter/1", "scrutiny_uid": normalized["source_uid"],
                    "actor_ref": actor, "group_ref": item["group_ref"],
                    "source_category": item["source_category"]}
                payload = {"schema": "dv.affiliation-observation/1", "person_id": person.stable_id,
                    "organization_id": group.stable_id, "scrutiny_id": scrutiny.stable_id,
                    "observed_on": scrutiny.occurred_on.isoformat(),
                    "source_version": source_ref(document), "record_locator": locator,
                    "meaning": "observed_at_scrutiny_no_membership_boundaries"}
                AffiliationObservation.objects.get_or_create(person=person, organization=group,
                    source_document=document, observed_on=scrutiny.occurred_on,
                    result_digest=sha256_digest(payload), defaults={"payload": payload,
                        "known_at": document.captured_at, "record_id": normalized["source_uid"]+":"+actor,
                        "record_locator": locator, "extraction_rule": PARSER_VERSION})
            if item["mandate_ref"]:
                unresolved_mandates.add(item["mandate_ref"])
            vote, created = VoteObservation.objects.get_or_create(scrutiny=scrutiny, person=person, source_document=document,
                defaults={"source_category":item["source_category"], "normalized_category":item["normalized_category"],
                    "mapping_rule":PARSER_VERSION, "affiliation_at_event":group, "mandate":None})
            if vote.normalized_category != item["normalized_category"]:
                raise ValidationError("assembly_existing_vote_conflict")
            vote.clean()
            added += int(created)
            counts[item["normalized_category"]] += 1
            seen.add((scrutiny.stable_id, person.stable_id))
        rows.append({"scrutiny_id":scrutiny.stable_id, "occurred_on":scrutiny.occurred_on.isoformat(),
            "observations":len(observations), "categories":dict(sorted(counts.items())),
            "official_vote_url":scrutiny.official_vote_url,
            "official_text_url":scrutiny.submitted_text.official_url,
            "qualified_text_relation":not bool(scrutiny.anomaly), "source_version":source_ref(document)})
    return {"format":"dv.materialized-expansion/1", "source_version":source_ref(document),
        "archive_records_examined":seen_records, "archive_first_date":min(dates) if dates else None,
        "archive_last_date":max(dates) if dates else None,
        "matched_scrutinies":rows, "observations_seen":len(seen), "observations_added":added,
        "unresolved_mandate_refs_count":len(unresolved_mandates), "anomalies":anomalies,
        "limits":["Seuls les scrutins déjà matérialisés avec la même version source sont enrichis.",
            "Noms non résolus restent identifiants ; aucun mandat ou statut d'affiliation continu inventé.",
            "XV et Sénat hors de ces deux archives ; aucune exhaustivité depuis janvier 2022."],
        "normalized_digest":sha256_digest(rows)}
