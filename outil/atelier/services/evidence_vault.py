"""Coffre privé et sas d'export des preuves externes.

Ce module ne contacte aucun service, ne publie rien et n'expose aucune route.
Il conserve les octets complets dans le runtime privé puis produit, sur demande,
un manifeste public strictement borné qui peut être remis au chantier Site.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings

from .canonical import canonical_bytes


class EvidenceVaultError(RuntimeError):
    pass


SOURCE_KINDS = {
    "press",
    "television",
    "radio",
    "video",
    "social",
    "speech",
    "institutional",
    "other_public",
}
STORAGE_TIERS = {"light", "web_archive", "full_media"}
SAFE_SUFFIXES = {".html", ".json", ".txt", ".pdf", ".png", ".jpg", ".webp", ".warc", ".wacz", ".bin"}
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
SECRET_QUERY_PARTS = {"token", "secret", "signature", "password", "session", "auth", "key"}


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _vault_root() -> Path:
    root = Path(settings.DV_EVIDENCE_VAULT_PATH).resolve()
    runtime = Path(settings.RUNTIME_DIR).resolve()
    static = Path(settings.STATIC_ROOT).resolve()
    if not _inside(root, runtime):
        raise EvidenceVaultError("vault_outside_private_runtime")
    if _inside(root, static) or _inside(static, root):
        raise EvidenceVaultError("vault_static_overlap")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _staging_root() -> Path:
    root = Path(settings.DV_PUBLIC_EVIDENCE_STAGING_PATH).resolve()
    vault = _vault_root()
    static = Path(settings.STATIC_ROOT).resolve()
    if _inside(root, vault) or _inside(vault, root):
        raise EvidenceVaultError("public_staging_vault_overlap")
    if _inside(root, static) or _inside(static, root):
        raise EvidenceVaultError("public_staging_static_overlap")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _public_url(value: str, *, optional: bool = False) -> str:
    if optional and not value:
        return ""
    split = urlsplit(value)
    if split.scheme != "https" or not split.hostname or split.username or split.password:
        raise EvidenceVaultError("public_url_invalid")
    host = split.hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith(".local") or host.endswith(".private"):
        raise EvidenceVaultError("public_url_private_host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise EvidenceVaultError("public_url_private_address")
    clean_query = []
    for key, item in parse_qsl(split.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in TRACKING_KEYS:
            continue
        if any(part in lowered for part in SECRET_QUERY_PARTS):
            raise EvidenceVaultError("public_url_sensitive_query")
        clean_query.append((key, item))
    return urlunsplit(("https", split.netloc, split.path or "/", urlencode(clean_query), ""))


def _suffix(value: str) -> str:
    suffix = value.casefold() if value.startswith(".") else f".{value.casefold()}"
    if suffix not in SAFE_SUFFIXES:
        return ".bin"
    return suffix


def _current_usage(objects_root: Path) -> int:
    if not objects_root.exists():
        return 0
    return sum(path.stat().st_size for path in objects_root.rglob("*") if path.is_file())


def _atomic_write(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix="dv-evidence-", suffix=".part", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def _public_text(value: str, maximum: int) -> str:
    text = value.strip()
    if any(ord(character) < 32 and character not in {"\n", "\t"} for character in text):
        raise EvidenceVaultError("public_text_control_character")
    return text[:maximum]


def store_private_capture(
    payload: bytes,
    *,
    source_url: str,
    source_kind: str,
    media_type: str,
    captured_at: datetime,
    suffix: str = ".bin",
    storage_tier: str = "light",
) -> dict:
    """Stocke une capture dédupliquée ; aucun octet n'entre dans le sas public."""
    if source_kind not in SOURCE_KINDS:
        raise EvidenceVaultError("source_kind_invalid")
    if storage_tier not in STORAGE_TIERS:
        raise EvidenceVaultError("storage_tier_invalid")
    if not isinstance(payload, bytes) or not payload:
        raise EvidenceVaultError("capture_empty")
    if captured_at.tzinfo is None:
        raise EvidenceVaultError("captured_at_timezone_required")
    clean_url = _public_url(source_url)
    root = _vault_root()
    objects = root / "objects" / "sha256"
    digest = hashlib.sha256(payload).hexdigest()
    target = objects / digest[:2] / f"{digest}{_suffix(suffix)}"
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    if not target.exists():
        if _current_usage(objects) + len(payload) > settings.DV_EVIDENCE_VAULT_MAX_BYTES:
            raise EvidenceVaultError("vault_quota_exceeded")
        _atomic_write(target, payload)
        created = True
    elif target.stat().st_size != len(payload):
        raise EvidenceVaultError("capture_digest_collision")

    record = {
        "format": "dv.private-evidence/1",
        "evidence_id": f"sha256:{digest}",
        "source_url": clean_url,
        "source_kind": source_kind,
        "storage_tier": storage_tier,
        "captured_at": captured_at.isoformat(),
        "sha256": digest,
        "media_type": media_type[:160],
        "bytes": len(payload),
        "object_path": target.relative_to(root).as_posix(),
    }
    record_digest = hashlib.sha256(canonical_bytes(record)).hexdigest()
    records = root / "records"
    records.mkdir(parents=True, exist_ok=True)
    record_path = records / f"{record_digest}.json"
    if not record_path.exists():
        _atomic_write(record_path, canonical_bytes(record))
    return {**record, "record_sha256": record_digest, "created": created}


def stage_public_record(
    private_record: dict,
    *,
    attribution: str,
    excerpt: str = "",
    published_at: str = "",
    archive_url: str = "",
) -> dict:
    """Produit un dérivé JSON sans chemin local, reçu réseau ni octets bruts."""
    required = {"evidence_id", "source_url", "source_kind", "captured_at", "sha256", "media_type", "bytes"}
    if not required.issubset(private_record):
        raise EvidenceVaultError("private_record_incomplete")
    if private_record["source_kind"] not in SOURCE_KINDS:
        raise EvidenceVaultError("source_kind_invalid")
    if private_record.get("storage_tier", "light") not in STORAGE_TIERS:
        raise EvidenceVaultError("storage_tier_invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(private_record["evidence_id"])):
        raise EvidenceVaultError("evidence_id_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(private_record["sha256"])):
        raise EvidenceVaultError("evidence_digest_invalid")
    if len(excerpt) > 1200:
        raise EvidenceVaultError("public_excerpt_too_long")
    public = {
        "format": "dv.public-evidence/1",
        "evidence_id": private_record["evidence_id"],
        "source": {
            "url": _public_url(str(private_record["source_url"])),
            "kind": private_record["source_kind"],
            "attribution": _public_text(attribution, 250),
            "published_at": _public_text(published_at, 40),
        },
        "capture": {
            "captured_at": private_record["captured_at"],
            "sha256": private_record["sha256"],
            "media_type": private_record["media_type"],
            "bytes": int(private_record["bytes"]),
            "storage_tier": private_record.get("storage_tier", "light"),
        },
        "archive_url": _public_url(archive_url, optional=True),
        "excerpt": _public_text(excerpt, 1200),
        "notice": "La copie intégrale reste dans le coffre privé ; ce dérivé n'autorise aucune publication implicite.",
    }
    staging = _staging_root()
    digest = hashlib.sha256(canonical_bytes(public)).hexdigest()
    target = staging / f"{digest}.json"
    if not target.exists():
        _atomic_write(target, canonical_bytes(public))
    return {**public, "export_sha256": digest, "staged_path": target.name}


def plan_capture(*, source_kind: str, fragile: bool = False, full_media_exception: bool = False) -> dict:
    """Plan déterministe de conservation ; ne capture ni ne transmet rien."""
    if source_kind not in SOURCE_KINDS:
        raise EvidenceVaultError("source_kind_invalid")
    heavy_media = source_kind in {"television", "radio", "video"}
    if heavy_media and full_media_exception:
        storage_tier = "full_media"
        retained = ["original_media", "transcript", "timestamps", "sha256"]
        reason = "Conservation intégrale explicitement marquée comme exceptionnelle."
    elif heavy_media:
        storage_tier = "light"
        retained = ["transcript", "timestamps", "key_frames", "sha256"]
        reason = "Le média intégral est omis par défaut pour maîtriser le volume."
    elif fragile:
        storage_tier = "web_archive"
        retained = ["wacz_or_warc", "visible_text", "screenshot", "sha256"]
        reason = "La page fragile justifie une archive web complète dans le coffre privé."
    else:
        storage_tier = "light"
        retained = ["visible_text", "screenshot", "metadata", "sha256"]
        reason = "Capture légère suffisante par défaut."
    return {
        "format": "dv.evidence-capture-plan/1",
        "source_kind": source_kind,
        "storage_tier": storage_tier,
        "retained": retained,
        "reason": reason,
        "private_vault": True,
        "wayback_submission_authorized": False,
        "public_export_authorized": False,
    }
