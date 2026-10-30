from django import template
register = template.Library()


@register.filter
def review_label(value):
    return {"registered": "Inscrits", "voters": "Votants", "expressed": "Suffrages exprimés",
        "for": "Pour", "against": "Contre", "abstention": "Abstentions", "known": "Établi",
        "unknown": "Non établi", "not_applicable": "Sans objet", "source_fidelity": "Liaison aux sources",
        "identity": "Identités", "procedure": "Procédure", "coverage": "Couverture déclarée",
        "reuse": "Qualification de réutilisation", "privacy": "Filtrage des champs",
        "public_sources": "Liens officiels", "counts": "Cohérence des décomptes",
        "calculation": "Recalcul factuel", "comparability": "Critères de comparabilité documentés",
        "citations": "Citation documentée", "counterevidence": "Contre-éléments documentés",
        "not_comparable":"Non comparable", "convergence":"Convergence documentée", "divergence":"Divergence documentée",
        "inconclusive":"Non concluant", "mixed":"Éléments mixtes", "evolution":"Évolution établie"}.get(str(value), value)
