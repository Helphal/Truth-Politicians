"""Adaptateur strict vers un service d'analyse local explicitement configuré."""
from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from ipaddress import ip_address
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from django.conf import settings


class AnalysisProviderUnavailable(RuntimeError):
    pass


class _RejectRedirects(HTTPRedirectHandler):
    """Une requête cognitive privée ne transmet jamais son Bearer par redirection."""

    handler_order = 100

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class AnalysisProviderResult:
    proposal: dict
    model: str
    provider: str
    usage: dict
    cost_eur: Decimal | None


_REQUIRED_PROPOSAL_KEYS = {
    "result",
    "short_answer",
    "main_reservation",
    "reasoning",
    "included_acts",
    "excluded_acts",
}


def _endpoint() -> str:
    base = settings.DV_ANALYSIS_PROVIDER_ENDPOINT.rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
    ):
        raise AnalysisProviderUnavailable("analysis_provider_endpoint_invalid")
    if parsed.scheme == "http":
        try:
            private = ip_address(parsed.hostname).is_private or ip_address(parsed.hostname).is_loopback
        except ValueError:
            private = parsed.hostname == "localhost"
        if not private:
            raise AnalysisProviderUnavailable("analysis_provider_https_required")
    # L'administrateur fournit l'URL complète du service compatible ; aucun
    # chemin propre à une infrastructure n'est ajouté implicitement.
    return base


def _parse_cost(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        cost = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise AnalysisProviderUnavailable("analysis_provider_cost_invalid") from None
    if cost < 0:
        raise AnalysisProviderUnavailable("analysis_provider_cost_invalid")
    return cost


def _proposal_from_content(content: object, *, candidate: dict | None = None) -> dict:
    if isinstance(content, str):
        text = content.strip()
        if text.startswith("```json") and text.endswith("```"):
            text = text[7:-3].strip()
        try:
            content = json.loads(text)
        except json.JSONDecodeError:
            raise AnalysisProviderUnavailable("analysis_provider_response_invalid") from None
    if not isinstance(content, dict) or set(content) != _REQUIRED_PROPOSAL_KEYS:
        raise AnalysisProviderUnavailable("analysis_provider_proposal_invalid")
    if content["result"] not in {
        "CONFIRMED", "CONTRADICTED", "MIXED", "INCONCLUSIVE",
        "confirme", "contredit", "mitige", "non_concluant",
    }:
        raise AnalysisProviderUnavailable("analysis_provider_proposal_invalid")
    for name, limit in {
        "short_answer": 2_000,
        "main_reservation": 2_000,
        "reasoning": 12_000,
    }.items():
        value = content[name]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise AnalysisProviderUnavailable("analysis_provider_proposal_invalid")
    if not isinstance(content["included_acts"], list) or not isinstance(content["excluded_acts"], list):
        raise AnalysisProviderUnavailable("analysis_provider_proposal_invalid")
    if len(content["included_acts"]) > 100 or len(content["excluded_acts"]) > 100:
        raise AnalysisProviderUnavailable("analysis_provider_proposal_invalid")
    for collection in (content["included_acts"], content["excluded_acts"]):
        for item in collection:
            if not isinstance(item, dict) or not item or len(item) > 24:
                raise AnalysisProviderUnavailable("analysis_provider_proposal_invalid")
            if any(not isinstance(key, str) or len(key) > 80 for key in item):
                raise AnalysisProviderUnavailable("analysis_provider_proposal_invalid")
            if any(
                not isinstance(value, (str, int, float, bool, type(None)))
                or (isinstance(value, str) and len(value) > 4_000)
                for value in item.values()
            ):
                raise AnalysisProviderUnavailable("analysis_provider_proposal_invalid")
    included_ids = []
    for item in content["included_acts"]:
        stable_id = item.get("scrutiny_id")
        if not isinstance(stable_id, str) or not stable_id:
            raise AnalysisProviderUnavailable("analysis_provider_proposal_invalid")
        included_ids.append(stable_id)
    if len(included_ids) != len(set(included_ids)):
        raise AnalysisProviderUnavailable("analysis_provider_proposal_invalid")
    if candidate is not None:
        candidate_acts = candidate.get("included_acts")
        if not isinstance(candidate_acts, list):
            raise AnalysisProviderUnavailable("analysis_provider_candidate_acts_invalid")
        candidate_ids = {
            item.get("scrutiny_id")
            for item in candidate_acts
            if isinstance(item, dict) and isinstance(item.get("scrutiny_id"), str)
        }
        if not set(included_ids).issubset(candidate_ids):
            raise AnalysisProviderUnavailable("analysis_provider_proposal_act_reference_invalid")
    return content


def prepare(candidate: dict) -> AnalysisProviderResult:
    if not isinstance(candidate, dict) or not candidate:
        raise ValueError("analysis_provider_candidate_invalid")
    token = settings.DV_ANALYSIS_PROVIDER_TOKEN
    if not token:
        raise AnalysisProviderUnavailable("analysis_provider_auth_missing")
    encoded_candidate = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
    if len(encoded_candidate.encode("utf-8")) > settings.DV_ANALYSIS_PROVIDER_MAX_INPUT_BYTES:
        raise ValueError("analysis_provider_candidate_too_large")

    payload = json.dumps(
        {
            "task": "ANALYZE_SIGNAL",
            "input": {
                "candidate": candidate,
                "instruction": (
                    "Prépare une proposition documentaire factuelle. N'approuve rien, n'invente aucune "
                    "source et conserve les références. Retourne uniquement l'objet demandé."
                ),
            },
            "output_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(_REQUIRED_PROPOSAL_KEYS),
                "properties": {
                    "result": {"type": "string"},
                    "short_answer": {"type": "string"},
                    "main_reservation": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "included_acts": {"type": "array", "items": {"type": "object"}},
                    "excluded_acts": {"type": "array", "items": {"type": "object"}},
                },
            },
            "data_classification": "GREEN",
            "routing": {"mode": "local_only", "fallback": "none"},
            "timeout_seconds": settings.DV_ANALYSIS_PROVIDER_TIMEOUT_SECONDS,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        _endpoint(),
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()))
    # Défense ajoutée sur l'instance elle-même : elle reste active même si la
    # fabrique d'opener est substituée lors d'une recette.
    redirect_guard = _RejectRedirects()
    opener.add_handler(redirect_guard)
    # Un opener injecté peut déjà contenir le redirecteur standard. Remplacer
    # explicitement ses chaînes 3xx empêche que ce second gestionnaire suive la
    # réponse après notre refus.
    for code in (301, 302, 303, 307, 308):
        opener.handle_error.setdefault("http", {})[code] = [redirect_guard]
    try:
        with opener.open(request, timeout=settings.DV_ANALYSIS_PROVIDER_TIMEOUT_SECONDS) as response:
            raw = response.read(settings.DV_ANALYSIS_PROVIDER_MAX_OUTPUT_BYTES + 1)
    except HTTPError as exc:
        raise AnalysisProviderUnavailable(f"analysis_provider_http_{exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise AnalysisProviderUnavailable("analysis_provider_unreachable") from None
    if len(raw) > settings.DV_ANALYSIS_PROVIDER_MAX_OUTPUT_BYTES:
        raise AnalysisProviderUnavailable("analysis_provider_response_too_large")
    try:
        envelope = json.loads(raw)
        content = envelope.get("result", envelope.get("content"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError):
        raise AnalysisProviderUnavailable("analysis_provider_response_invalid") from None
    if envelope.get("provider") != "local" or envelope.get("local_only") is not True:
        raise AnalysisProviderUnavailable("analysis_provider_route_contract_invalid")
    proposal = _proposal_from_content(content, candidate=candidate)
    usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    return AnalysisProviderResult(
        proposal=proposal,
        model=str(envelope.get("model", "unknown")),
        provider=str(envelope.get("provider", "analysis_provider")),
        usage=usage,
        cost_eur=_parse_cost(envelope.get("cost_eur")),
    )
