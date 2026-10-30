from django import template

from atelier.services.accounts import public_attribution_for

register = template.Library()


@register.filter
def dv_attribution(user):
    """Libellé d'attribution éditoriale (pseudonyme), pas un identifiant Site."""
    if user is None:
        return "rédacteur"
    return public_attribution_for(user)
