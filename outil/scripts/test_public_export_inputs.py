"""La préparation accepte un corpus choisi, lié à ses octets, sans données privées."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from prepare_notices_rec01 import load_exports


class PublicExportInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "runtime").mkdir()

    def export(self, folder, revision, count=1):
        directory = self.root / "runtime" / folder
        directory.mkdir()
        records = [
            {
                "record_type": "person",
                "record_id": f"SYNTH:{index}",
                "revision": revision,
                "is_test": False,
                "publication_circuit": "factual",
                "data": {"display_name": f"Personne fictive {index}"},
            }
            for index in range(count)
        ]
        # Fixture synthétique du format d'export réel ; aucune publication.
        raw = json.dumps({"records": records}).encode()
        (directory / "batch.json").write_bytes(raw)
        manifest = {
            "schema": "dv-public-export-v1",
            "counts": {"person": count},
            "batches": [{
                "type": "person", "file": "batch.json", "records": count,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }],
        }
        (directory / "MANIFEST.json").write_text(json.dumps(manifest))
        return f"runtime/{folder}/MANIFEST.json"

    def select(self, paths):
        (self.root / "runtime/export-inputs.json").write_text(json.dumps(paths))

    def test_arbitrary_corpus_size_and_ordered_revisions(self):
        first = self.export("initial", "r1", count=3)
        second = self.export("correction", "r2", count=1)
        self.select([first, second])
        records, provenance, manifests = load_exports(self.root)
        self.assertEqual(len(records), 3)
        self.assertEqual(records["person", "SYNTH:0"]["revision"], "r2")
        self.assertEqual(records["person", "SYNTH:1"]["revision"], "r1")
        self.assertEqual(provenance["person", "SYNTH:0"]["record_revision"], "r2")
        self.assertEqual([entry["file"] for entry in manifests], [first, second])

    def test_changed_batch_cannot_keep_an_old_receipt(self):
        selected = self.export("initial", "r1")
        self.select([selected])
        (self.root / "runtime/initial/batch.json").write_text('{"records": []}')
        with self.assertRaises(AssertionError):
            load_exports(self.root)

    def test_selection_cannot_read_another_directory(self):
        self.select(["../private/MANIFEST.json"])
        with self.assertRaisesRegex(ValueError, "outside_root"):
            load_exports(self.root)

    def test_manifest_cannot_load_a_batch_outside_its_directory(self):
        selected = self.export("initial", "r1")
        self.select([selected])
        path = self.root / selected
        manifest = json.loads(path.read_text())
        manifest["batches"][0]["file"] = "../outside.json"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "outside_manifest_directory"):
            load_exports(self.root)


if __name__ == "__main__":
    unittest.main()
