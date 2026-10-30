from __future__ import annotations


ADMITTED = {"for", "against", "abstention"}


def pair_agreement(observations: list[dict]) -> dict:
    comparable = [item for item in observations if item.get("left") in ADMITTED and item.get("right") in ADMITTED]
    numerator = sum(item["left"] == item["right"] for item in comparable)
    denominator = len(comparable)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "excluded": len(observations) - denominator,
        "fraction": None if denominator == 0 else {"numerator": numerator, "denominator": denominator},
        "rule_id": "PAIR_EQUAL_KNOWN_V1",
        "universe": "Paires où les deux positions sont pour, contre ou abstention",
        "result_state": "not_calculable" if denominator == 0 else "known",
    }


def group_distribution(categories: list[str]) -> dict:
    return {
        key: sum(value == key for value in categories)
        for key in ("for", "against", "abstention", "non_voting", "not_recorded", "unknown")
    }
