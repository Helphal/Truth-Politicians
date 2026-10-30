"""Filtre d'inclusion des archives release (aucun secret, pas de sondes runtime)."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


def _load_build_release():
    root = Path(__file__).resolve().parents[1]
    module_path = root / "scripts" / "build_release.py"
    spec = importlib.util.spec_from_file_location("build_release", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["build_release"] = module
    spec.loader.exec_module(module)
    return module


br = _load_build_release()


class ReleasePathFilterTests(unittest.TestCase):
    def test_excludes_runtime_probes_and_pem(self):
        self.assertTrue(br.release_relative_path_excluded(Path("deploy/nas/runtime-probes/x.json")))
        self.assertTrue(br.release_relative_path_excluded(Path("deploy/nas/runtime-probes/ca.pem")))
        self.assertTrue(br.release_relative_path_excluded(Path("deploy/nas/staging-build003-private-admin/runtime-staging/a.pem")))

    def test_allows_examples_and_app_files(self):
        self.assertFalse(br.release_relative_path_excluded(Path(".env.example")))
        self.assertFalse(br.release_relative_path_excluded(Path("deploy/nas/.env.caddy.example")))
        self.assertFalse(br.release_relative_path_excluded(Path("deploy/nas/Caddyfile")))
        self.assertFalse(br.release_relative_path_excluded(Path("deploy/nas/compose.yaml")))

    def test_excludes_secret_like_names(self):
        self.assertTrue(br.release_relative_path_excluded(Path("deploy/nas/.env.private")))
        self.assertTrue(br.release_relative_path_excluded(Path("runtime/nas/atelier.sqlite3")))
        self.assertTrue(br.release_relative_path_excluded(Path("deploy/nas/staging-private-listener-probe.json")))

    def test_excludes_secret_suffixes_case_insensitive(self):
        self.assertTrue(br.release_relative_path_excluded(Path("deploy/nas/runtime-probes/CA.PEM")))
        self.assertTrue(br.release_relative_path_excluded(Path("deploy/nas/backup.KEY")))
        self.assertTrue(br.release_relative_path_excluded(Path("deploy/nas/token.AGE")))

    def test_build_omits_probe_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "release-include.txt").write_text("deploy\n", encoding="utf-8")
            nas = root / "deploy" / "nas"
            nas.mkdir(parents=True)
            (nas / "Caddyfile").write_text("# ok\n", encoding="utf-8")
            probes = nas / "runtime-probes"
            probes.mkdir()
            (probes / "production-caddy-ca.pem").write_bytes(b"fake-pem-not-secret")
            (probes / "production-private-listener-probe.json").write_text("{}", encoding="utf-8")

            out = root / "runtime" / "releases" / "test.zip"
            out.parent.mkdir(parents=True)

            def patched_included_files(project_root: Path):
                entries = ["deploy"]
                result: dict[str, Path] = {}
                for entry in entries:
                    candidate = (project_root / entry).resolve()
                    paths = sorted(p for p in candidate.rglob("*") if p.is_file())
                    for path in paths:
                        relative = path.relative_to(project_root)
                        if br.release_relative_path_excluded(relative):
                            continue
                        result[relative.as_posix()] = path
                return [(path, name) for name, path in sorted(result.items())]

            files = patched_included_files(root)
            names = {name for _, name in files}
            self.assertIn("deploy/nas/Caddyfile", names)
            self.assertFalse(any("runtime-probes" in n for n in names))
            self.assertFalse(any(n.endswith(".pem") for n in names))
            self.assertFalse(any("probe.json" in n for n in names))


class ReleaseArchiveIntegrationTests(unittest.TestCase):
    def test_real_build_has_no_probe_or_secret_entries(self):
        root = Path(__file__).resolve().parents[1]
        out = root / "runtime" / "releases" / "_test_build_release_filter.zip"
        if out.exists():
            out.unlink()
        try:
            br.build(out)
            with zipfile.ZipFile(out, "r") as archive:
                names = archive.namelist()
            forbidden = [
                n
                for n in names
                if "runtime-probes" in n
                or "runtime-staging" in n
                or n.lower().endswith(".pem")
                or "/.env.private" in n
                or n.endswith("production-private-listener-probe.json")
            ]
            self.assertEqual(forbidden, [], msg=f"forbidden entries: {forbidden}")
        finally:
            if out.exists():
                out.unlink()
