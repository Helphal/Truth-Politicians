"""Projection contrat DV-RACC-001 (C01–C04) sans règle politique inventée."""
from __future__ import annotations

from copy import deepcopy

from .canonical import sha256_digest


def qualification_fingerprints(public: dict) -> dict:
    material = public.get("qualification_material") or {}
    qualified = material.get("qualified_declaration") or public.get("qualified_declaration")
    c01 = sha256_digest(qualified if qualified is not None else {"state": "missing"})
    actors = material.get("actors") or public.get("actors")
    c02 = sha256_digest(actors if actors is not None else {"state": "missing"})
    c03 = sha256_digest(
        {
            "declaration_measure_links": material.get("declaration_measure_links", []),
            "measure_scrutiny_links": material.get("measure_scrutiny_links", []),
            "declaration_source": material.get("declaration_source"),
            "scrutiny_id": material.get("scrutiny_id"),
        }
    )
    collective = material.get("collective_vote_distribution")
    c04 = sha256_digest(collective if collective is not None else {"state": "absent"})
    return {
        "version": "racc001-1",
        "C01": c01,
        "C02": c02,
        "C03": c03,
        "C04": c04,
    }


def attach_qualification_fingerprints(body: dict) -> dict:
    """Empreintes sur l'autorisation uniquement ; le public reste l'instantané revu."""
    body = deepcopy(body)
    auth = deepcopy(body.get("authorization") or {})
    auth["qualification_fingerprints"] = qualification_fingerprints(body["public"])
    body["authorization"] = auth
    return body


def enrich_publication_envelope(body: dict) -> dict:
    """Conservé pour compatibilité d'import : n'altère plus le public après approbation."""
    return attach_qualification_fingerprints(body)
