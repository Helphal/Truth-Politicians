"""Private source references under the currently received link conditions.

Sénat references stay readable as text while the required notification is not
received. This presentation policy never changes source content or rights.
"""
from urllib.parse import urlsplit
from django import template
from django.core.exceptions import ValidationError
from django.utils.html import format_html
from atelier.services.publication import _validate_public_url

register = template.Library()


@register.filter
def qualified_source_link(value, label='Source'):
    try:
        _validate_public_url(value)
        parsed = urlsplit(value)
        if parsed.scheme != 'https':
            raise ValidationError('https_source_required')
    except (ValidationError, ValueError, TypeError):
        return 'Référence publique non admissible'
    host = (parsed.hostname or '').lower()
    if host == 'senat.fr' or host.endswith('.senat.fr'):
        return format_html('{} : {} (référence textuelle ; lien non qualifié)', label, value)
    return format_html('<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>', value, label)
