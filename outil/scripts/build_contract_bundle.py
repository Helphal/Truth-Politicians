"""Paquet déterministe à liste fermée, réservé à la coordination du contrat candidat."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
FILES = [
    "contracts/dv-content-0.4.0-draft.3.schema.json",
    "contracts/dv-event-0.2.0-draft.1.schema.json",
    "contracts/dv-receipt-0.1.0-draft.1.schema.json",
    "contracts/examples-draft3/official_vote.synthetic.json",
    "contracts/examples-draft3/vote_comparison.synthetic.json",
    "contracts/examples-draft3/speech_vote.synthetic.json",
    "contracts/canonical-draft3-vectors.json",
    "docs/CONTRAT_DRAFT3_MIGRATION.md",
]


def build():
    target = ROOT / "runtime/releases/truth-politicians-contract-draft3.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = {}
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(FILES):
            source = (ROOT / name).resolve()
            if ROOT not in source.parents or source.is_symlink():
                raise RuntimeError("bundle_path_not_allowed")
            data = source.read_bytes()
            manifest[name] = hashlib.sha256(data).hexdigest()
            info = zipfile.ZipInfo(name, (2026, 9, 13, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        info = zipfile.ZipInfo("MANIFEST.sha256.json", (2026, 9, 13, 0, 0, 0))
        archive.writestr(info, json.dumps(manifest, indent=2, sort_keys=True).encode())
    print(json.dumps(dict(file=target.name, files=len(FILES)+1, bytes=target.stat().st_size,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(), contents=manifest)))


if __name__ == "__main__":
    build()
