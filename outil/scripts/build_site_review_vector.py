"""Vecteur partagé Ed25519 fictif ; seule la clé publique est conservée."""
import base64
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from atelier.services.site_bridge import site_record_digest

target = ROOT / "contracts/site-ed25519-vector.synthetic.json"
if target.exists():
    raise SystemExit("vector_already_exists_preserved")
record = {"record_type":"comparison", "record_id":"SYNTHETIC:ED25519:1", "revision":1,
    "sequence":1, "operation":"upsert", "publication_circuit":"analysis", "is_test":True,
    "controls":{k:True for k in ("source_fidelity", "identity", "procedure", "coverage", "public_sources",
        "reuse", "privacy", "comparability", "citations", "counterevidence")},
    "data":{"slug":"synthetic-ed25519", "title":"FICTIF : é / test \u2028 séparation",
        "declaration_quote":"Exemple fictif, aucune déclaration réelle.", "declaration_date":"2024-01-01",
        "author_person_external_id":"SYNTHETIC:PERSON:1", "short_answer":"Cas fictif de vérification logicielle.",
        "main_caveat":"Aucune revue réelle ni publication.", "reasoning":"Vérifier la signature de cette révision fictive.",
        "result_code":"inconclusive", "method_version":"DV-M0.1", "source_external_ids":["SYNTHETIC:SRC:1"],
        "scrutin_external_ids":["SYNTHETIC:SCRUTIN:1"], "reviewed_at":"2026-09-13T00:00:00Z", "public_status":"draft"}}
key = Ed25519PrivateKey.generate()
digest = site_record_digest(record)
record["review"] = {"algorithm":"ed25519", "key_id":"test-ephemeral-vector", "snapshot_sha256":digest,
    "signature":base64.b64encode(key.sign(digest.encode("ascii"))).decode()}
vector = {"fixture_only":True, "real_human_review":False, "private_key_persisted":False,
    "public_keys":{"test-ephemeral-vector":base64.b64encode(key.public_key().public_bytes_raw()).decode()},
    "record":record, "canonical_rule":"DV_Security::record_digest; excludes review/sequence; SHA256 ASCII hex signed"}
target.write_text(json.dumps(vector, ensure_ascii=False, indent=2), encoding="utf-8")
print("synthetic_site_vector_created_no_private_key_saved")
