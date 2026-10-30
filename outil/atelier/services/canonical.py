"""Sérialisation déterministe bornée pour les empreintes inter-composants.

DV-CANON-JSON-V1 accepte uniquement le sous-ensemble JSON sans nombres flottants,
encode en UTF-8, trie les clés par ordre Unicode et retire les espaces. Ce format
n'est pas présenté comme RFC 8785 ; ses vecteurs de test font partie du contrat.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


VERSION = "DV-CANON-JSON-V1"


class CanonicalizationError(ValueError):
    pass


def _validated(value: Any, path: str = "$") -> Any:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        raise CanonicalizationError(f"floating_number_forbidden:{path}")
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise CanonicalizationError(f"non_string_key:{path}")
        return {key: _validated(value[key], f"{path}.{key}") for key in value}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_validated(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise CanonicalizationError(f"unsupported_type:{path}")


def canonical_bytes(value: Any) -> bytes:
    checked = _validated(value)
    return json.dumps(
        checked,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
