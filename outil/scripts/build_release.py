"""Construit un paquet local déterministe, sans données privées ni rapports."""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path


FIXED_ZIP_TIME = (2026, 9, 13, 0, 0, 0)
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "runtime",
    "reports",
    "references",
    "tmp",
}
EXCLUDED_DIR_NAMES = frozenset(
    {
        "runtime-probes",
        "runtime-staging",
        "caddy-data",
    }
)
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".sqlite", ".sqlite3", ".db", ".log"}
SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".age", ".ppk", ".crt"})
SECRET_BASENAMES = frozenset(
    {
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "credentials.json",
        "secrets.json",
    }
)
ALLOWED_ENV_EXAMPLES = frozenset(
    {
        ".env.example",
        ".env.caddy.example",
        ".env.compose-private.example",
        "env.staging.example",
    }
)
PRIVATE_MISSION_SCRIPT_PREFIXES = (
    "prepare_night_", "inspect_", "run_night_", "build_night_",
    "verify_night_", "freeze_night_", "package_night", "record_night_",
)


def release_relative_path_excluded(relative: Path) -> bool:
    """True si le chemin relatif au racine outil ne doit pas entrer dans une release."""
    parts = relative.parts
    if EXCLUDED_PARTS.intersection(parts):
        return True
    if EXCLUDED_DIR_NAMES.intersection(parts):
        return True
    if relative.parts[0] == "scripts" and relative.name.startswith(PRIVATE_MISSION_SCRIPT_PREFIXES):
        return True
    name = relative.name
    lower_name = name.lower()
    if lower_name in SECRET_BASENAMES or any(
        lower_name.startswith(f"{base}.") for base in SECRET_BASENAMES
    ):
        return True
    if Path(name).suffix.lower() in SECRET_SUFFIXES:
        return True
    if name == ".env" or name.startswith(".env"):
        return not (name in ALLOWED_ENV_EXAMPLES or name.endswith(".example"))
    if relative.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    if lower_name.endswith("-probe.json") or lower_name.endswith("_probe.json"):
        return True
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _included_files(root: Path) -> list[tuple[Path, str]]:
    entries = [
        line.strip()
        for line in (root / "release-include.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    result: dict[str, Path] = {}
    for entry in entries:
        candidate = (root / entry).resolve()
        if candidate != root and root not in candidate.parents:
            raise RuntimeError("release_entry_outside_project")
        if not candidate.exists():
            raise RuntimeError(f"release_entry_missing:{entry}")
        paths = [candidate] if candidate.is_file() else sorted(path for path in candidate.rglob("*") if path.is_file())
        for path in paths:
            relative = path.relative_to(root)
            if release_relative_path_excluded(relative):
                continue
            result[relative.as_posix()] = path
    return [(path, name) for name, path in sorted(result.items())]


def build(output: Path) -> tuple[int, str, int]:
    root = Path(__file__).resolve().parent.parent
    release_root = (root / "runtime" / "releases").resolve()
    release_root.mkdir(parents=True, exist_ok=True)
    output = output.resolve()
    if release_root not in output.parents:
        raise RuntimeError("release_output_must_be_under_runtime_releases")
    if output.exists():
        raise RuntimeError("release_output_already_exists")
    files = _included_files(root)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, name in files:
            info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
            info.create_system = 0
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        manifest = {name: _sha256(path) for path,name in files}
        info=zipfile.ZipInfo('MANIFEST.sha256.json',FIXED_ZIP_TIME)
        info.create_system=0;info.external_attr=0o100644<<16
        archive.writestr(info,json.dumps(manifest,sort_keys=True,indent=2).encode('utf-8'),compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    return len(files)+1, _sha256(output), output.stat().st_size


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    output = Path(args.output) if args.output else root / "runtime" / "releases" / "truth-politicians-outil.zip"
    count, digest, size = build(output)
    print(
        f"status=local_package_built path={output.resolve()} files={count} bytes={size} "
        f"sha256={digest} docker_used=false"
    )


if __name__ == "__main__":
    main()
