"""Recette reproductible : réseau refusé, bases temporaires, sorties sur disque."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile

try:
    from scripts.evidence_bytes import write_evidence
except ModuleNotFoundError:
    from evidence_bytes import write_evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--step-timeout", type=int, default=180,
        help="Délai maximal par étape, en secondes (défaut : 180).")
    parser.add_argument("--allow-loopback", action="store_true",
        help="Autorise uniquement les adresses IP loopback littérales pour le banc local.")
    parser.add_argument("--wordpress-integration", action="store_true",
        help="Inclut les tests du vrai banc WordPress Local B, à provisionner séparément.")
    parser.add_argument("--php-contract-integration", action="store_true",
        help="Inclut les six tests de contrat PHP historique avec CLI et scripts reçus.")
    parser.add_argument("--evidence-root", type=Path,
        help="Racine des pièces documentaires déjà reçues, lues sans modification.")
    parser.add_argument("--deployment-compose", type=Path,
        help="Artefact compose explicite à vérifier pour la collecte des fichiers statiques.")
    args = parser.parse_args()
    if args.step_timeout <= 0:
        parser.error("--step-timeout doit être strictement positif")
    if args.evidence_root and not args.evidence_root.is_dir():
        parser.error("--evidence-root doit désigner un dossier reçu existant")
    if args.deployment_compose and not args.deployment_compose.is_file():
        parser.error("--deployment-compose doit désigner un fichier existant")
    php_contract = args.php_contract_integration or args.wordpress_integration
    if php_contract:
        executable = os.environ.get("DV_RACC001_PHP_EXECUTABLE", "")
        scripts_root = os.environ.get("DV_RACC001_SITE_TESTS_ROOT", "")
        if not executable or not Path(executable).is_file():
            parser.error("DV_RACC001_PHP_EXECUTABLE doit désigner le CLI PHP reçu")
        if not scripts_root or not all((Path(scripts_root) / name).is_file() for name in
                ("racc001-contract-test.php", "racc001-event-schema-bilateral-test.php")):
            parser.error("DV_RACC001_SITE_TESTS_ROOT doit contenir les scripts PHP reçus")
    if args.wordpress_integration:
        if not args.allow_loopback:
            parser.error("--wordpress-integration requiert --allow-loopback")
        required = ("DV_LOCAL_B_TEST_KEYS_JSON", "DV_LOCAL_B_WP_USER", "DV_LOCAL_B_WP_PASSWORD",
                    "DV_EVENT_TEST_EVENT_SECRET_BASE64", "DV_RACC001_SITE_TESTS_ROOT")
        absent = [name for name in required if not os.environ.get(name)]
        if absent:
            parser.error("préconditions WordPress absentes : " + ", ".join(absent))
        if not Path(os.environ["DV_LOCAL_B_TEST_KEYS_JSON"]).is_file():
            parser.error("fichier de clés du banc SYNTH WordPress absent")
    root, output = args.project_root.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dv-cr002-tests-") as temp:
        environment = os.environ.copy()
        environment.update(DV_ENV="test", DV_NETWORK_MODE="disabled", PYTHONDONTWRITEBYTECODE="1",
            PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
            DV_AUDIT_ALLOW_LOOPBACK="1" if args.allow_loopback else "0",
            DV_RACC001_LIVE_WORDPRESS="1" if args.wordpress_integration else "0",
            DV_RACC001_PHP_CONTRACT="1" if php_contract else "0",
            DV_DATABASE_PATH=str(Path(temp)/"isolated.sqlite3"), DV_RUNTIME_DIR=str(Path(temp)/"runtime"),
            PYTHONPATH=str(root/"scripts/offline_guard") + os.pathsep + str(root))
        if args.evidence_root:
            environment["DV_TEST_EVIDENCE_ROOT"] = str(args.evidence_root.resolve())
        if args.deployment_compose:
            environment["DV_TEST_DEPLOY_COMPOSE"] = str(args.deployment_compose.resolve())
        steps = [
            ("network-guard", ["-c", "import socket\ntry:\n socket.getaddrinfo('example.org',443)\nexcept RuntimeError as e:\n if str(e) != 'AUDIT_REAL_NETWORK_DISABLED:socket.getaddrinfo': raise\n print('network_guard_dns_blocked_before_network=true')\nelse:\n raise SystemExit('network_guard_missing')"]),
            ("django-check", ["manage.py", "check"]),
            ("migration-drift", ["manage.py", "makemigrations", "--check", "--dry-run"]),
            ("migrate-fresh", ["manage.py", "migrate", "--noinput"]),
            ("application-and-independent", ["manage.py", "test", "atelier.tests", "review_regressions", "review_followup", "--noinput", "--verbosity", "2"]),
            ("reference", ["-m", "unittest", "discover", "-s", "tests", "-v"]),
        ]
        results = []
        for name, command in steps:
            timed_out = False
            try:
                result = subprocess.run([sys.executable, *command], cwd=root, env=environment,
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=args.step_timeout)
                exit_code, stdout, stderr = result.returncode, result.stdout, result.stderr
            except subprocess.TimeoutExpired as error:
                timed_out = True
                exit_code = 124
                stdout, stderr = error.stdout or "", error.stderr or ""
                if isinstance(stdout, bytes):
                    stdout = stdout.decode("utf-8", errors="replace")
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
            log = f"step={name}\nexit_code={exit_code}\ntimed_out={timed_out}\n" + stdout + stderr
            log_sha256 = write_evidence(output/(name+".txt"), log)
            results.append({"step":name, "exit_code":exit_code, "timed_out":timed_out,
                "skipped_tests":re.findall(r"^(.+?) \.\.\. skipped (.+)$", stdout + stderr, re.MULTILINE),
                "log_sha256":log_sha256})
            print(f"{name}: exit={exit_code}", flush=True)
            if name == "network-guard" and exit_code != 0:
                # Fail closed: no test is allowed to run without the audit guard.
                break
        summary = {"network_guard_active":results[0]["exit_code"] == 0,
            "step_timeout_seconds":args.step_timeout,
            "loopback_literal_only":args.allow_loopback,
            "wordpress_integration_requested":args.wordpress_integration,
            "php_contract_integration_requested":php_contract,
            "php_contract_qualified":False if not php_contract else None,
            "qualification_scope":"application_isolated_and_requested_integrations_only",
            "wordpress_qualified":False if not args.wordpress_integration else None,
            "deployment_compose_supplied":bool(args.deployment_compose),
            "historical_database_touched":False, "temporary_database_only":True,
            "steps":results, "passed":all(r["exit_code"] == 0 for r in results)}
        write_evidence(output/"resultats.json", json.dumps(summary, indent=2))
        if not summary["passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
