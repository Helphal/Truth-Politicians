"""Invocation PHP locale DV-RACC-001 (banc Lightning, racine Site configurable)."""
from __future__ import annotations

import os
from pathlib import Path

PHP_EXECUTABLE = Path(
    os.environ.get("DV_RACC001_PHP_EXECUTABLE",
        r"C:\Users\EXAMPLE_USER\AppData\Roaming\Local\lightning-services\php-8.3.23+0\bin\win64\php.exe")
)
PHP_EXTENSION_DIR = PHP_EXECUTABLE.parent / "ext"

_DEFAULT_SITE_TESTS = Path(
    r"C:\Users\EXAMPLE_USER\Desktop\Truth Politicians\worktrees\dv-racc-001-site\site\tests"
)

RACC001_PRESS_PUBLIC_URL = (
    "https://www.assemblee-nationale.fr/dyn/16/comptes-rendus/racc001-corpus-press"
)


def site_tests_root() -> Path:
    override = os.environ.get("DV_RACC001_SITE_TESTS_ROOT", "").strip()
    if os.environ.get("DV_RACC001_SITE_TESTS_ROOT_REQUIRED", "").strip() == "1" and not override:
        raise RuntimeError("site_tests_root_required")
    if override:
        root = Path(override).resolve()
        bound = os.environ.get("DV_RACC001_SITE_TESTS_ROOT_BOUND", "").strip()
        if bound:
            bound_path = Path(bound).resolve()
            try:
                root.relative_to(bound_path)
            except ValueError:
                raise RuntimeError(f"site_tests_root_outside_bound:{root}!={bound_path}") from None
        contract = root / "racc001-contract-test.php"
        if not contract.is_file():
            raise RuntimeError(f"site_tests_root_invalid:{root}")
        return root
    if os.environ.get("DV_RACC001_SITE_TESTS_ROOT_FORBID_FALLBACK", "").strip() == "1":
        raise RuntimeError("site_tests_root_fallback_forbidden")
    return _DEFAULT_SITE_TESTS.resolve()


def site_test_script(name: str) -> Path:
    return site_tests_root() / name


def php_invocation_proof() -> str:
    root = site_tests_root()
    return f"php_site_tests_root={root.resolve()}"


SITE_RACC001_CONTRACT_TEST = site_test_script("racc001-contract-test.php")
SITE_RACC001_EVENT_SIG_VERIFY = site_test_script("racc001-event-sig-verify.php")
SITE_RACC001_REVIEW_BUNDLE_VALIDATE = site_test_script("racc001-review-bundle-validate.php")
SITE_RACC001_SITE_CONFIRMATION_MATERIAL = site_test_script("racc001-site-confirmation-material-test.php")
SITE_RACC001_RECOVERY_BOUNDED = site_test_script("racc001-recovery-anchor-bounded-test.php")
SITE_RACC001_EVENT_SCHEMA_BILATERAL = site_test_script("racc001-event-schema-bilateral-test.php")


def refresh_site_test_paths() -> None:
    """Réévalue les chemins après changement de DV_RACC001_SITE_TESTS_ROOT."""
    global SITE_RACC001_CONTRACT_TEST, SITE_RACC001_EVENT_SIG_VERIFY
    global SITE_RACC001_REVIEW_BUNDLE_VALIDATE, SITE_RACC001_SITE_CONFIRMATION_MATERIAL
    global SITE_RACC001_RECOVERY_BOUNDED, SITE_RACC001_EVENT_SCHEMA_BILATERAL
    SITE_RACC001_CONTRACT_TEST = site_test_script("racc001-contract-test.php")
    SITE_RACC001_EVENT_SIG_VERIFY = site_test_script("racc001-event-sig-verify.php")
    SITE_RACC001_REVIEW_BUNDLE_VALIDATE = site_test_script("racc001-review-bundle-validate.php")
    SITE_RACC001_SITE_CONFIRMATION_MATERIAL = site_test_script("racc001-site-confirmation-material-test.php")
    SITE_RACC001_RECOVERY_BOUNDED = site_test_script("racc001-recovery-anchor-bounded-test.php")
    SITE_RACC001_EVENT_SCHEMA_BILATERAL = site_test_script("racc001-event-schema-bilateral-test.php")


def php_argv(*args: str) -> list[str]:
    if not PHP_EXECUTABLE.is_file():
        raise FileNotFoundError(f"php_missing:{PHP_EXECUTABLE}")
    if os.name != "nt":
        return [str(PHP_EXECUTABLE), *args]
    if not PHP_EXTENSION_DIR.is_dir():
        raise FileNotFoundError(f"php_extension_dir_missing:{PHP_EXTENSION_DIR}")
    return [
        str(PHP_EXECUTABLE),
        f"-dextension_dir={PHP_EXTENSION_DIR}",
        "-dextension=mbstring",
        "-dextension=sodium",
        *args,
    ]
