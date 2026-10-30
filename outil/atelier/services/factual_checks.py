"""Contrôles des valeurs réellement exportées, distincts de la revue documentaire."""
from __future__ import annotations

from .calculations import pair_agreement


def counts_check(payload: dict) -> tuple[bool, str]:
    counts = payload.get("counts", {})
    expected = {"registered", "voters", "expressed", "for", "against", "abstention"}
    if not isinstance(counts, dict) or set(counts) != expected:
        return False, "counts_fields_invalid"
    for item in counts.values():
        if not isinstance(item, dict) or not set(item).issubset({"state", "value", "reason"}):
            return False, "count_invalid"
        if item.get("state") == "known":
            if type(item.get("value")) is not int or item["value"] < 0:
                return False, "count_value_invalid"
        elif item.get("state") not in {"unknown", "unavailable", "not_applicable"} or item.get("value") is not None or not item.get("reason"):
            return False, "count_state_invalid"
    # Cette règle ne prétend pas décrire une procédure non qualifiée.
    if payload.get("institution") not in {"Assemblée nationale", "Sénat"} or not payload.get("procedure", "").lower().startswith("scrutin public"):
        return False, "count_procedure_not_qualified"
    known = {key: item["value"] for key, item in counts.items() if item["state"] == "known"}
    if {"for", "against", "expressed"} <= known.keys() and known["for"] + known["against"] != known["expressed"]:
        return False, "expressed_count_mismatch"
    # Pas d'égalité imposée avec l'effectif d'un groupe, les inscrits ou les absents.
    if {"expressed", "voters"} <= known.keys() and known["expressed"] > known["voters"]:
        return False, "expressed_exceeds_voters"
    if {"expressed", "abstention", "voters"} <= known.keys() and known["expressed"] + known["abstention"] > known["voters"]:
        return False, "recorded_categories_exceed_voters"
    return True, "AN_SENAT_PUBLIC_COUNTS_V1: relations applicables vérifiées; inconnus conservés"


def calculation_check(payload: dict) -> tuple[bool, str]:
    if payload.get("rule_id") != "PAIR_EQUAL_KNOWN_V1":
        return False, "calculation_rule_unknown"
    observations = payload.get("observations")
    if not isinstance(observations, list) or not all(isinstance(item, dict) for item in observations):
        return False, "observations_invalid"
    universe_keys = [
        (
            item.get("scrutin_id"),
            item.get("left_person_id"),
            item.get("right_person_id"),
            item.get("left"),
            item.get("right"),
        )
        for item in observations
    ]
    if any(not key[0] for key in universe_keys) or len(set(universe_keys)) != len(universe_keys):
        return False, "calculation_universe_duplicate_or_missing"
    expected = pair_agreement(observations)
    # Comparaison canonique : booléens et entiers restent de types distincts.
    from .canonical import canonical_bytes
    if canonical_bytes(payload.get("calculation")) != canonical_bytes(expected):
        return False, "calculation_not_derived_from_observations"
    return True, "PAIR_EQUAL_KNOWN_V1: univers, exclusions et fraction recalculés"
