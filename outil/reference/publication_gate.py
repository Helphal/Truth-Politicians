"""Spécification exécutable de certaines règles DV, bibliothèque standard.

PAS un service de production. Les attestations, revues et versions courantes sont
fournies par une frontière de confiance SIMULÉE dans les tests. Ce module ne sait
pas authentifier un humain, vérifier une source, résoudre le DNS ou publier.
Un administrateur pouvant modifier le code et ces entrées peut modifier le résultat.
Le condensat Python n'est pas déclaré conforme à RFC 8785 ni interlangage.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import ipaddress
import json
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

SCHEMA = "dv.content/0.4.0-draft.1"
POLICY = "DV-PUB-1"
KINDS = frozenset({"official_vote", "vote_comparison", "speech_vote"})
ROOT = frozenset({"schema_version", "record_id", "record_revision", "content_kind",
                  "policy_version", "method_version", "source_versions", "data_scope", "public"})
COMMON_CHECKS = frozenset({"source_fidelity", "identity", "procedure", "coverage",
                          "public_sources", "reuse", "privacy"})
REQUIRED_CHECKS = {
    "official_vote": COMMON_CHECKS | {"counts"},
    "vote_comparison": COMMON_CHECKS | {"calculation"},
    "speech_vote": COMMON_CHECKS | {"comparability", "citations", "counterevidence"},
}
VOTES = frozenset({"for", "against", "abstention", "non_voting", "not_recorded"})
RESULTS = frozenset({"convergence", "divergence", "evolution", "mixed",
                     "not_comparable", "inconclusive"})

class InvalidCandidate(ValueError):
    """Structure ou valeur interdite ; aucun détail sensible dans le message."""


def _closed(obj: Any, keys: set[str] | frozenset[str]) -> None:
    if not isinstance(obj, dict) or set(obj) != keys:
        raise InvalidCandidate("unexpected_or_missing_fields")


def _text(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidCandidate("text_required")


def _positive(value: Any) -> None:
    if type(value) is not int or value < 1:
        raise InvalidCandidate("positive_integer_required")


def _date(value: Any) -> None:
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError
        date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidCandidate("date_required") from exc


def _url(value: Any) -> None:
    """Filtrage lexical seulement. Un vrai client doit vérifier DNS/redirections."""
    _text(value)
    if any(ord(c) < 33 for c in value) or "\\" in value:
        raise InvalidCandidate("invalid_public_url")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)):
            raise ValueError
        # Volontairement plus strict que le web : aucune IP littérale exportée.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        if ("." not in host or host.endswith((".local", ".internal", ".localhost",
                                               ".ts.net", ".test", ".invalid"))
                or host == "localhost" or host.endswith(".")):
            raise ValueError
        host.encode("idna")
    except (ValueError, UnicodeError) as exc:
        raise InvalidCandidate("invalid_public_url") from exc


def digest(candidate: Mapping[str, Any]) -> str:
    """Empreinte des entrées de ce démonstrateur Python, pas signature d'autorité."""
    data = json.dumps(candidate, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def pair_counts(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Règle candidate : accord parmi les paires pour/contre/abstention connues."""
    admitted = {"for", "against", "abstention"}
    comparable = [o for o in observations if o["left"] in admitted and o["right"] in admitted]
    return {
        "same_position": sum(o["left"] == o["right"] for o in comparable),
        "denominator": len(comparable),
        "excluded": len(observations) - len(comparable),
        # Pas de score fabriqué lorsqu'aucune paire n'est comparable.
        "agreement_fraction": None if not comparable else {
            "numerator": sum(o["left"] == o["right"] for o in comparable),
            "denominator": len(comparable)},
    }


def validate_candidate(c: Any) -> None:
    _closed(c, ROOT)
    if c["schema_version"] != SCHEMA or c["policy_version"] != POLICY:
        raise InvalidCandidate("unsupported_version")
    if c["data_scope"] not in {"synthetic", "real"}:
        raise InvalidCandidate("unknown_data_scope")
    _text(c["record_id"])
    _positive(c["record_revision"])
    kind = c["content_kind"]
    if not isinstance(kind, str) or kind not in KINDS:
        raise InvalidCandidate("unsupported_kind")
    if c["method_version"] is not None:
        _text(c["method_version"])
    if kind == "speech_vote" and c["method_version"] is None:
        raise InvalidCandidate("method_required")
    sources = c["source_versions"]
    if not isinstance(sources, list) or not sources:
        raise InvalidCandidate("sources_required")
    ids: set[str] = set()
    source_urls: set[str] = set()
    for s in sources:
        _closed(s, {"source_id", "version", "public_url"})
        _text(s["source_id"]); _text(s["version"]); _url(s["public_url"])
        if s["source_id"] in ids:
            raise InvalidCandidate("duplicate_source")
        ids.add(s["source_id"]); source_urls.add(s["public_url"])
    p = c["public"]
    linked: list[str] = []
    if kind == "official_vote":
        _closed(p, {"institution", "official_title", "scrutin_id", "occurred_on",
                    "stage", "official_text_url", "official_vote_url", "text_reference_role",
                    "nominative_status", "counts"})
        for key in ("institution", "official_title", "scrutin_id", "stage"):
            _text(p[key])
        _date(p["occurred_on"])
        if p["text_reference_role"] not in {"submitted", "result_only", "unresolved"}:
            raise InvalidCandidate("unknown_text_relation")
        if p["nominative_status"] not in {"available", "partial", "not_available"}:
            raise InvalidCandidate("unknown_nominative_status")
        _closed(p["counts"], {"for", "against", "abstention"})
        for n in p["counts"].values():
            if n is not None and (type(n) is not int or n < 0):
                raise InvalidCandidate("invalid_count")
        linked += [p["official_text_url"], p["official_vote_url"]]
    elif kind == "vote_comparison":
        _closed(p, {"rule_id", "scope", "observations"})
        if p["rule_id"] != "PAIR_EQUAL_KNOWN_V1":
            raise InvalidCandidate("unsupported_rule")
        _text(p["scope"])
        if not isinstance(p["observations"], list) or not p["observations"]:
            raise InvalidCandidate("observations_required")
        scrutin_ids: set[str] = set()
        for o in p["observations"]:
            _closed(o, {"scrutin_id", "left", "right", "official_text_url", "official_vote_url"})
            _text(o["scrutin_id"])
            if o["scrutin_id"] in scrutin_ids:
                raise InvalidCandidate("duplicate_scrutin")
            scrutin_ids.add(o["scrutin_id"])
            for k in ("left", "right"):
                if o[k] is not None and (not isinstance(o[k], str) or o[k] not in VOTES):
                    raise InvalidCandidate("unknown_vote")
            linked += [o["official_text_url"], o["official_vote_url"]]
    else:
        _closed(p, {"title", "short_answer", "main_caveat", "reasoning", "result",
                    "declaration_quote", "declaration_source_url", "official_text_url",
                    "official_vote_url"})
        for k in ("title", "short_answer", "main_caveat", "reasoning", "declaration_quote"):
            _text(p[k])
        if p["result"] not in RESULTS:
            raise InvalidCandidate("unsupported_result")
        linked += [p["declaration_source_url"], p["official_text_url"], p["official_vote_url"]]
    for u in linked:
        _url(u)
        if u not in source_urls:
            raise InvalidCandidate("unregistered_source_link")


@dataclass(frozen=True)
class ControlAttestation:
    """Entrée du service de contrôle de confiance. Le JSON candidat n'en fournit pas."""
    snapshot_digest: str
    passed: frozenset[str]
    failed: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrustedReviewEvent:
    """Événement interne simulé. M05 doit apporter authentification et persistance."""
    record_id: str
    revision: int
    snapshot_digest: str
    reviewer_id: str
    decision: str
    environment: str
    revoked: bool = False


@dataclass(frozen=True)
class Eligibility:
    allowed: bool
    reason: str
    route: str | None = None


def evaluate(candidate: Mapping[str, Any], controls: ControlAttestation | None,
             current_source_versions: Mapping[str, str],
             trusted_reviews: Sequence[TrustedReviewEvent] = (),
             registered_reviewers: frozenset[str] = frozenset(),
             environment: str = "test", suspended: bool = False,
             established_kind: str | None = None) -> Eligibility:
    """Éligibilité de démonstration uniquement ; aucune fonction de publication."""
    try:
        validate_candidate(candidate)
        d = digest(candidate)
    except (InvalidCandidate, ValueError, TypeError, KeyError):
        return Eligibility(False, "invalid_candidate")
    kind = candidate["content_kind"]
    if environment not in {"test", "production"}:
        return Eligibility(False, "unknown_environment")
    if environment == "production" and candidate["data_scope"] != "real":
        return Eligibility(False, "synthetic_content_in_production")
    if suspended:
        return Eligibility(False, "suspended")
    if established_kind is not None and established_kind != kind:
        return Eligibility(False, "kind_changed")
    if controls is None or controls.snapshot_digest != d:
        return Eligibility(False, "controls_missing_or_stale")
    if controls.failed or not REQUIRED_CHECKS[kind] <= controls.passed:
        return Eligibility(False, "controls_failed_or_incomplete")
    for s in candidate["source_versions"]:
        if current_source_versions.get(s["source_id"]) != s["version"]:
            return Eligibility(False, "source_changed_or_unknown")
    if kind == "official_vote" and candidate["public"]["text_reference_role"] != "submitted":
        return Eligibility(False, "voted_text_unresolved")
    if kind != "speech_vote":
        return Eligibility(True, "eligible_not_published", "automatic_checked")
    for r in reversed(trusted_reviews):
        if r.record_id != candidate["record_id"] or r.environment != environment:
            continue
        # L'événement le plus récent pour ce dossier fait autorité dans ce modèle.
        if (not r.revoked and r.decision == "approve" and r.revision == candidate["record_revision"]
                and r.snapshot_digest == d and r.reviewer_id in registered_reviewers):
            return Eligibility(True, "eligible_not_published", "human_reviewed")
        return Eligibility(False, "review_missing_or_stale")
    return Eligibility(False, "review_missing_or_stale")


@dataclass
class PublicationCursor:
    """Récepteur simulé en mémoire. Ni authentification, ni persistance, ni réseau."""
    record_id: str
    revision: int = 0
    sequence: int = 0
    snapshot_digest: str | None = None
    suspended: bool = False
    last_event: tuple[Any, ...] | None = None

    def apply(self, record_id: str, revision: int, sequence: int, snapshot_digest: str,
              operation: str, authorized: bool, resume_authorized: bool = False) -> str:
        # Les booléens de ce simulateur représentent une décision SERVEUR déjà prise.
        # Ne jamais les lire tels quels dans un corps de requête de production.
        if record_id != self.record_id:
            return "wrong_object"
        if not authorized:
            return "unauthorized"
        if type(revision) is not int or type(sequence) is not int or revision < 1 or sequence < 1:
            return "invalid_event"
        if operation not in {"upsert", "suspend"} or not isinstance(snapshot_digest, str) or not snapshot_digest:
            return "invalid_event"
        event = (record_id, revision, sequence, snapshot_digest, operation)
        if sequence == self.sequence:
            return "duplicate" if event == self.last_event else "sequence_conflict"
        if sequence < self.sequence:
            return "stale_event"
        if operation == "upsert":
            if revision < self.revision:
                return "stale_revision"
            if revision == self.revision and self.snapshot_digest != snapshot_digest:
                return "revision_conflict"
            if self.suspended and not resume_authorized:
                return "suspension_latched"
            self.revision = revision
            self.snapshot_digest = snapshot_digest
            self.suspended = False
        else:
            # Ne jamais perdre l'ordre de suspension, même avec l'ancienne révision.
            self.suspended = True
        self.sequence = sequence
        self.last_event = event
        return "applied_in_memory_only"
