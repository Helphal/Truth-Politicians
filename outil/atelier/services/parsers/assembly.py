from __future__ import annotations

import json
from pathlib import Path
import zipfile

from atelier.services.collector import inspect_zip


PARSER_VERSION = "assemblee-json-v2"

NOMINATIVE_CATEGORY_MAP = {
    "pours": "for",
    "contres": "against",
    "abstentions": "abstention",
    "nonVotants": "non_voting",
    "nonVotantsVolontaires": "non_voting",
}


def _unwrap(record):
    while isinstance(record, dict) and len(record) == 1 and next(iter(record)) in {"scrutin", "scrutins"}:
        record = next(iter(record.values()))
    return record


def iter_archive_records(path: Path, *, limit: int | None = None):
    count = 0
    infos = sorted(inspect_zip(path), key=lambda item: item.filename)
    with zipfile.ZipFile(path) as archive:
        for info in infos:
            if info.is_dir() or not info.filename.lower().endswith(".json"):
                continue
            if info.file_size > 10 * 1024 * 1024:
                raise ValueError("assembly_member_too_large")
            with archive.open(info) as member:
                data = member.read(10 * 1024 * 1024 + 1)
            if len(data) > 10 * 1024 * 1024:
                raise ValueError("assembly_member_too_large")
            payload = json.loads(data.decode("utf-8-sig"))
            payload = _unwrap(payload)
            records = payload if isinstance(payload, list) else [payload]
            for record in records:
                record = _unwrap(record)
                if isinstance(record, dict):
                    yield record
                    count += 1
                    if limit is not None and count >= limit:
                        return


def _nested(data, *paths, default=None):
    for path in paths:
        current = data
        try:
            for part in path.split("."):
                current = current[part]
            if current is not None:
                return current
        except (KeyError, TypeError):
            continue
    return default


def normalize_scrutiny(record: dict) -> dict:
    uid = str(_nested(record, "uid", "scrutin.uid", default=""))
    number = str(_nested(record, "numero", "numScrutin", default=""))
    date_value = str(_nested(record, "dateScrutin", "date", default=""))[:10]
    title = str(_nested(record, "titre", "objet.libelle", "objet", default=""))
    legislature = str(_nested(record, "legislature", default=""))
    synth = _nested(record, "syntheseVote", default={}) or {}
    counts = _nested(synth, "decompte", default={}) or {}
    for_count = _nested(counts, "pour", "pours", default=None)
    against = _nested(counts, "contre", "contres", default=None)
    abstention = _nested(counts, "abstentions", "abstention", default=None)
    if not number and uid:
        number = "".join(character for character in uid if character.isdigit())
    if not (number and date_value and title):
        raise ValueError("assembly_required_fields_missing")
    def integer(value):
        return None if value in {None, ""} else int(value)

    return {
        "source_identifier": number,
        "source_uid": uid,
        "institution": "Congrès du Parlement" if uid.startswith("VTCG") else "Assemblée nationale",
        "legislature": legislature,
        "occurred_on": date_value,
        "official_title": title,
        "official_vote_url": f"https://www.assemblee-nationale.fr/dyn/{legislature}/scrutins/{number}",
        "result_status": str(_nested(record, "sort.code", "sort.libelle", default="unknown")),
        "voters": integer(_nested(synth, "nombreVotants", default=None)),
        "expressed": integer(_nested(synth, "suffragesExprimes", default=None)),
        "count_for": integer(for_count),
        "count_against": integer(against),
        "count_abstention": integer(abstention),
        "nominative_status": "available",
        "raw": record,
        "parser_version": PARSER_VERSION,
    }


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, dict) and "votant" in value:
        value = value["votant"]
    return value if isinstance(value, list) else [value]


def iter_vote_observations(record: dict):
    """Produit seulement les positions nominativement publiées par la source.

    Une personne absente de ces rubriques n'est jamais reconstruite depuis la
    position majoritaire de son groupe.
    """
    groups = _nested(record, "ventilationVotes.organe.groupes.groupe", default=[]) or []
    groups = groups if isinstance(groups, list) else [groups]
    for group in groups:
        group_ref = str(group.get("organeRef") or "")
        nominative = _nested(group, "vote.decompteNominatif", default={}) or {}
        for raw_category, normalized in NOMINATIVE_CATEGORY_MAP.items():
            for voter in _as_list(nominative.get(raw_category)):
                if not isinstance(voter, dict) or not voter.get("acteurRef"):
                    continue
                yield {
                    "actor_ref": str(voter["acteurRef"]),
                    "mandate_ref": str(voter.get("mandatRef") or ""),
                    "group_ref": group_ref,
                    "source_category": raw_category,
                    "normalized_category": normalized,
                }


def find_vote_observation(record: dict, actor_ref: str) -> dict | None:
    matches = [item for item in iter_vote_observations(record) if item["actor_ref"] == actor_ref]
    if len(matches) > 1:
        raise ValueError("assembly_duplicate_actor_observation")
    return matches[0] if matches else None
