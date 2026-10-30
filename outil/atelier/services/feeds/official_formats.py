"""Parseurs de formats officiels live (BUILD-002-R2).

Part des octets capturés (DocumentVersion / ZIP / JSON / XML / HTML), pas des
fixtures métier maison. Les erreurs de membre sont isolées ; parser_version est
tracée. Aucun format « ressemblant » synthétique n'est présenté comme preuve live.
"""
from __future__ import annotations

import html
import io
import json
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from atelier.services.parsers.assembly import (
    PARSER_VERSION as ASSEMBLY_PARSER_VERSION,
    normalize_scrutiny,
)
from atelier.services.parsers.senate import (
    PARSER_VERSION as SENATE_PARSER_VERSION,
    parse_scrutiny_html,
)


PARSER_VERSION = "official-formats/v2"
PROBE_ITEM_LIMIT = 200  # recettes / probes uniquement — pas la collecte permanente
PRODUCTION_CHUNK_SIZE = 500
DEFAULT_ITEM_LIMIT = PROBE_ITEM_LIMIT  # compat tests R2 existants
MAX_MEMBER_BYTES = 10 * 1024 * 1024
MAX_ZIP_MEMBERS_PER_CHUNK = 50  # membres XML/JSON traités par chunk multi-fichiers

# Adaptateurs pour lesquels un parseur de format live officiel est implémenté.
LIVE_FORMAT_PARSERS = frozenset(
    {
        "an_amo",
        "an_votes",
        "an_amendements",
        "an_dossiers",
        "an_questions",
        "an_cr",
        "senat_acteurs",
        "senat_dosleg",
        "senat_ameli",
        "senat_akn",
        "senat_cr",
        "senat_questions",
        "senat_scrutins",
        "elysee_speech",
        "gouv_speech",
        "press_rss",
        "youtube_meta",
        "podcast_rss",
        "bluesky_public",
    }
)


def live_format_parser_implemented(parser_id: str) -> bool:
    return parser_id in LIVE_FORMAT_PARSERS


def _text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        if "#text" in value:
            return str(value.get("#text") or "")
        for key in ("text", "libelle", "nom", "prenom", "uid"):
            if key in value:
                return _text_value(value.get(key))
        return ""
    return str(value)


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _strip_html(value: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return " ".join(text.split())


def _parse_date_loose(value: str | None) -> str:
    if not value:
        return ""
    value = str(value).strip()
    for fmt, size in (("%Y-%m-%d", 10), ("%Y/%m/%d", 10), ("%d/%m/%Y", 10)):
        try:
            return datetime.strptime(value[:size], fmt).date().isoformat()
        except ValueError:
            continue
    # Syceron: 20241106140000000
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", value)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _iter_zip_json_payloads(raw: bytes, *, limit: int | None = None):
    """Itère les objets JSON des membres d'une archive ZIP officielle."""
    count = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = sorted(archive.infolist(), key=lambda i: i.filename)
        for info in infos:
            if info.is_dir() or not info.filename.lower().endswith(".json"):
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                continue
            with archive.open(info) as member:
                data = member.read(MAX_MEMBER_BYTES + 1)
            if len(data) > MAX_MEMBER_BYTES:
                continue
            try:
                payload = json.loads(data.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            yield info.filename, payload
            count += 1
            if limit is not None and count >= limit:
                return


def _iter_zip_xml_payloads(raw: bytes, *, limit: int | None = None):
    count = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for info in sorted(archive.infolist(), key=lambda i: i.filename):
            if info.is_dir() or not info.filename.lower().endswith(".xml"):
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                continue
            with archive.open(info) as member:
                data = member.read(MAX_MEMBER_BYTES + 1)
            if len(data) > MAX_MEMBER_BYTES:
                continue
            yield info.filename, data
            count += 1
            if limit is not None and count >= limit:
                return


def _normalize_acteur_item(record: dict) -> dict | None:
    acteur = record.get("acteur") if isinstance(record.get("acteur"), dict) else record
    if not isinstance(acteur, dict):
        return None
    uid = _text_value(acteur.get("uid"))
    if not uid:
        return None
    ident = ((acteur.get("etatCivil") or {}).get("ident") or {}) if isinstance(acteur.get("etatCivil"), dict) else {}
    prenom = _text_value(ident.get("prenom"))
    nom = _text_value(ident.get("nom"))
    organes = []
    mandats = acteur.get("mandats") or {}
    mandat_list = _as_list(mandats.get("mandat") if isinstance(mandats, dict) else mandats)
    for mandat in mandat_list:
        if not isinstance(mandat, dict):
            continue
        org_refs = mandat.get("organes") or {}
        ref = _text_value(org_refs.get("organeRef") if isinstance(org_refs, dict) else org_refs)
        label = _text_value((mandat.get("infosQualite") or {}).get("libQualite")) or ref
        if ref or label:
            organes.append(
                {
                    "libelle": label or ref,
                    "organeRef": ref,
                    "typeOrgane": _text_value(mandat.get("typeOrgane")),
                }
            )
    return {
        "uid": uid,
        "prenom": prenom,
        "nom": nom,
        "display_name": " ".join(filter(None, [prenom, nom])) or uid,
        "organes": organes,
        "mandats": mandat_list[:5],
        "format": "amo10_acteur",
    }


def _normalize_scrutin_item(record: dict) -> dict | None:
    from atelier.services.parsers.assembly import _unwrap

    try:
        normalized = normalize_scrutiny(_unwrap(record) if isinstance(record, dict) else record)
    except ValueError:
        return None
    raw = record.get("scrutin") if isinstance(record.get("scrutin"), dict) else record
    return {
        "uid": normalized.get("source_uid") or normalized.get("source_identifier"),
        "numero": normalized.get("source_identifier"),
        "titre": normalized.get("official_title"),
        "dateScrutin": normalized.get("occurred_on"),
        "syntheseVote": {
            "nombreVotants": normalized.get("voters"),
            "suffragesExprimes": normalized.get("expressed"),
            "decompte": {
                "pour": normalized.get("count_for"),
                "contre": normalized.get("count_against"),
                "abstentions": normalized.get("count_abstention"),
            },
        },
        "sort": {"code": normalized.get("result_status")},
        "ventilationVotes": (raw or {}).get("ventilationVotes") if isinstance(raw, dict) else {},
        "title": normalized.get("official_title"),
        "date": normalized.get("occurred_on"),
        "counts": {
            "for": normalized.get("count_for"),
            "against": normalized.get("count_against"),
            "abstention": normalized.get("count_abstention"),
        },
        "official_normalized": normalized,
        "format": "assemblee_scrutin_json",
        "parser_version": ASSEMBLY_PARSER_VERSION,
    }


def _normalize_amendement_item(record: dict) -> dict | None:
    amd = record.get("amendement") if isinstance(record.get("amendement"), dict) else record
    if not isinstance(amd, dict):
        return None
    uid = _text_value(amd.get("uid"))
    if not uid:
        return None
    corps = amd.get("corps") or {}
    contenu = corps.get("contenuAuteur") if isinstance(corps, dict) else {}
    if not isinstance(contenu, dict):
        contenu = {}
    objet = _strip_html(_text_value(contenu.get("exposeSommaire")) or _text_value(contenu.get("dispositif")))
    auteurs = []
    signataires = amd.get("signataires") or {}
    auteur = signataires.get("auteur") if isinstance(signataires, dict) else {}
    if isinstance(auteur, dict) and auteur.get("acteurRef"):
        auteurs.append(_text_value(auteur.get("acteurRef")))
    cycle = amd.get("cycleDeVie") or {}
    sort = ""
    if isinstance(cycle, dict):
        sort = _text_value((cycle.get("sort") or {}).get("code") if isinstance(cycle.get("sort"), dict) else cycle.get("sort"))
    sort = sort or _text_value(amd.get("sortEnSeance") or amd.get("etat"))
    return {
        "uid": uid,
        "numero": _text_value((amd.get("identification") or {}).get("numero") if isinstance(amd.get("identification"), dict) else amd.get("numero")),
        "objet": objet or uid,
        "texte": objet,
        "auteurs": auteurs,
        "sort": sort,
        "dossier": _text_value(amd.get("texteLegislatifRef")),
        "examenRef": _text_value(amd.get("examenRef")),
        "format": "assemblee_amendement_json",
    }


def _normalize_dossier_item(record: dict) -> dict | None:
    dossier = (
        record.get("dossierParlementaire")
        if isinstance(record.get("dossierParlementaire"), dict)
        else record.get("dossier")
        if isinstance(record.get("dossier"), dict)
        else record
    )
    if not isinstance(dossier, dict):
        return None
    uid = _text_value(dossier.get("uid"))
    if not uid:
        return None
    titre = dossier.get("titreDossier") or {}
    title = _text_value(titre.get("titre") if isinstance(titre, dict) else titre) or uid
    etapes = []
    if isinstance(dossier.get("etapes"), list):
        for etape in dossier["etapes"]:
            if isinstance(etape, dict):
                etapes.append(_text_value(etape.get("codeActe") or etape.get("uid") or etape))
            else:
                etapes.append(str(etape))
    else:
        actes = dossier.get("actesLegislatifs") or {}
        acte_list = _as_list(actes.get("acteLegislatif") if isinstance(actes, dict) else actes)
        for acte in acte_list[:20]:
            if isinstance(acte, dict):
                etapes.append(_text_value(acte.get("codeActe") or acte.get("uid")))
    return {
        "dossier_id": uid,
        "uid": uid,
        "titre": title,
        "title": title,
        "etapes": [e for e in etapes if e],
        "status": _text_value((dossier.get("procedureParlementaire") or {}).get("libelle") if isinstance(dossier.get("procedureParlementaire"), dict) else ""),
        "format": "assemblee_dossier_json",
    }


def _normalize_question_item(record: dict) -> dict | None:
    question = record.get("question") if isinstance(record.get("question"), dict) else record
    if not isinstance(question, dict):
        return None
    uid = _text_value(question.get("uid"))
    if not uid:
        return None
    textes = question.get("textesQuestion") or {}
    tq = textes.get("texteQuestion") if isinstance(textes, dict) else None
    tq = tq[0] if isinstance(tq, list) and tq else tq
    quote = ""
    occurred = ""
    if isinstance(tq, dict):
        quote = _strip_html(_text_value(tq.get("texte")))
        info = tq.get("infoJO") or {}
        occurred = _parse_date_loose(_text_value(info.get("dateJO")))
    auteur = question.get("auteur") or {}
    speaker = _text_value(((auteur.get("identite") or {}).get("acteurRef"))) if isinstance(auteur, dict) else ""
    qtype = _text_value(question.get("type")) or "question"
    if not quote:
        return None
    return {
        "stable_id": f"decl:an-q:{uid}",
        "uid": uid,
        "speaker_label": speaker or "Auteur question AN",
        "auteur": speaker,
        "capacity": qtype,
        "exact_quote": quote[:5000],
        "occurred_on": occurred,
        "date": occurred,
        "source_url": f"https://www.assemblee-nationale.fr/dyn/opendata/{uid}",
        "locator": f"question:{uid}",
        "context": _text_value(((question.get("indexationAN") or {}).get("analyses") or {}).get("analyse"))
        if isinstance(question.get("indexationAN"), dict)
        else qtype,
        "source_role": "original",
        "format": "assemblee_question_json",
    }


def _parse_syceron_xml(data: bytes, *, limit: int | None = DEFAULT_ITEM_LIMIT) -> list[dict]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        # Fragment éventuellement tronqué : tenter un enrobage minimal
        try:
            root = ET.fromstring(data + b"</compteRendu>")
        except ET.ParseError:
            return []
    seance_uid = ""
    occurred = ""
    for child in list(root):
        if _local(child.tag) == "uid":
            seance_uid = (child.text or "").strip()
        if _local(child.tag) == "metadonnees":
            for meta in child:
                if _local(meta.tag) == "dateSeance":
                    occurred = _parse_date_loose(meta.text or "")
    items: list[dict] = []
    for para in root.iter():
        if _local(para.tag) != "paragraphe":
            continue
        orateur = ""
        quote = ""
        for child in list(para):
            loc = _local(child.tag)
            if loc == "orateurs":
                for ora in child.iter():
                    if _local(ora.tag) == "nom" and (ora.text or "").strip():
                        orateur = (ora.text or "").strip()
                        break
            elif loc == "texte":
                quote = _strip_html("".join(child.itertext()))
        if not quote or len(quote) < 40:
            continue
        # Ignorer les formules de présidence trop courtes / purement procédurales
        if quote.lower().startswith("la séance est ouverte") and len(quote) < 80:
            continue
        actor = para.attrib.get("id_acteur") or ""
        sid = para.attrib.get("id_syceron") or para.attrib.get("ordre_absolu_seance") or str(len(items))
        items.append(
            {
                "item_kind": "speech",
                "stable_id": f"decl:an-cr:{seance_uid or 'seance'}:{sid}",
                "speaker_label": orateur or "Orateur AN",
                "person_stable_id": f"person:{actor}" if actor else None,
                "capacity": "député" if actor else "orateur",
                "exact_quote": quote[:5000],
                "occurred_on": occurred,
                "source_url": f"https://www.assemblee-nationale.fr/dyn/opendata/{seance_uid}" if seance_uid else "",
                "locator": f"syceron:{seance_uid}:{sid}",
                "context": f"Compte rendu {seance_uid}".strip(),
                "source_role": "original",
                "organization_label": "Assemblée nationale",
                "format": "assemblee_syceron_xml",
            }
        )
        if limit is not None and len(items) >= limit:
            break
    return items


def _parse_odsen(payload: dict) -> list[dict]:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    items = []
    for row in results:
        if not isinstance(row, dict):
            continue
        uid = str(row.get("Matricule") or "").strip()
        if not uid:
            continue
        prenom = str(row.get("Prenom_usuel") or "").strip()
        nom = str(row.get("Nom_usuel") or "").strip()
        groupes = []
        if row.get("Groupe_politique"):
            groupes.append({"libelle": str(row["Groupe_politique"]), "name": str(row["Groupe_politique"])})
        if row.get("Commission_permanente"):
            groupes.append({"libelle": str(row["Commission_permanente"]), "name": str(row["Commission_permanente"])})
        items.append(
            {
                "uid": uid,
                "matricule": uid,
                "prenom": prenom,
                "nom": nom,
                "display_name": " ".join(filter(None, [prenom, nom])) or uid,
                "organes": groupes,
                "groupes": groupes,
                "etat": row.get("Etat"),
                "format": "senat_odsen_general",
            }
        )
    return items


def _parse_senat_scrutin_html(data: bytes, source_url: str = "") -> list[dict]:
    text = data.decode("utf-8", errors="replace")
    # Page détail
    if re.search(r"Scrutin\s+n", text, re.I) and ("page-lead" in text or "sultat du scrutin" in text):
        try:
            normalized = parse_scrutiny_html(data, source_url or "https://www.senat.fr/scrutin-public/")
        except ValueError:
            normalized = None
        if normalized:
            return [
                {
                    "uid": normalized.get("source_identifier"),
                    "numero": normalized.get("source_identifier"),
                    "titre": normalized.get("official_title"),
                    "title": normalized.get("official_title"),
                    "dateScrutin": normalized.get("occurred_on"),
                    "date": normalized.get("occurred_on"),
                    "syntheseVote": {
                        "nombreVotants": normalized.get("voters"),
                        "suffragesExprimes": normalized.get("expressed"),
                        "decompte": {
                            "pour": normalized.get("count_for"),
                            "contre": normalized.get("count_against"),
                            "abstentions": normalized.get("count_abstention"),
                        },
                    },
                    "counts": {
                        "for": normalized.get("count_for"),
                        "against": normalized.get("count_against"),
                        "abstention": normalized.get("count_abstention"),
                    },
                    "official_normalized": normalized,
                    "format": "senat_scrutin_html",
                    "parser_version": SENATE_PARSER_VERSION,
                }
            ]
    # Index : liens vers détails
    items = []
    for href in re.findall(r'href="([^"]*scr20\d{2}-\d+\.html)"', text, re.I):
        num = re.search(r"scr20\d{2}-(\d+)", href, re.I)
        items.append(
            {
                "uid": num.group(1) if num else href,
                "numero": num.group(1) if num else href,
                "titre": f"Scrutin public {href}",
                "title": f"Scrutin public {href}",
                "dateScrutin": "",
                "source_url": href if href.startswith("http") else f"https://www.senat.fr/scrutin-public/{href.lstrip('/')}",
                "format": "senat_scrutin_index_link",
                "syntheseVote": {},
            }
        )
        if len(items) >= DEFAULT_ITEM_LIMIT:
            break
    return items


def _parse_senat_cr_html(data: bytes, source_url: str = "") -> list[dict]:
    """Index séances : discovery seulement — jamais de Declaration depuis un libellé."""
    text = data.decode("utf-8", errors="replace")
    # Page détail avec orateur + citation ?
    detail = _parse_senat_cr_detail_html(data, source_url=source_url)
    if detail:
        return detail
    items = []
    for href, label in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.S | re.I):
        lab = _strip_html(label)
        if not lab or len(lab) < 12:
            continue
        if not any(k in href.lower() for k in ("seance", "intervention", "compte", "cr/", "s20")):
            if not any(k in lab.lower() for k in ("séance", "seance", "intervention", "compte rendu")):
                continue
        url = href if href.startswith("http") else f"https://www.senat.fr/{href.lstrip('/')}"
        items.append(
            {
                "item_kind": "discovery",
                "discovery_only": True,
                "title": lab[:500],
                "source_url": url,
                "locator": f"senat-cr-discovery:{url}",
                "context": "Index séances Sénat",
                "organization_label": "Sénat",
                "format": "senat_cr_html_index",
            }
        )
        if len(items) >= 5000:
            break
    return items


def _parse_senat_cr_detail_html(data: bytes, source_url: str = "") -> list[dict]:
    """Détail séance : extrait interventions réelles si structure présente."""
    text = data.decode("utf-8", errors="replace")
    # Heuristique : blocs orateur + paragraphe suffisamment longs
    blocks = re.findall(
        r'(?:<p[^>]*class="[^"]*orateur[^"]*"[^>]*>|<strong[^>]*>)\s*([^<]{3,120})\s*</(?:p|strong)>'
        r'.*?<p[^>]*>(.*?)</p>',
        text,
        re.S | re.I,
    )
    if not blocks:
        # Marqueurs data-testid / fixtures offline
        blocks = re.findall(
            r'data-orateur="([^"]+)"[^>]*>.*?<p[^>]*class="intervention"[^>]*>(.*?)</p>',
            text,
            re.S | re.I,
        )
    if not blocks:
        return []
    occurred = ""
    dm = re.search(r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})', text)
    if dm:
        months = {
            "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
            "juillet": 7, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
        }
        month = months.get(dm.group(2).lower())
        if month:
            occurred = f"{int(dm.group(3)):04d}-{month:02d}-{int(dm.group(1)):02d}"
    items = []
    for idx, (speaker, body) in enumerate(blocks):
        quote = _strip_html(body)
        speaker = _strip_html(speaker)
        if len(quote) < 40 or not speaker:
            continue
        items.append(
            {
                "item_kind": "speech",
                "stable_id": f"decl:senat-cr-detail:{abs(hash(source_url + speaker + quote[:80])) & 0xFFFFFFFF:x}",
                "speaker_label": speaker[:250],
                "capacity": "sénateur",
                "exact_quote": quote[:5000],
                "occurred_on": occurred,
                "source_url": source_url,
                "locator": f"senat-cr-detail:{idx}",
                "context": "Compte rendu Sénat (détail)",
                "source_role": "original",
                "organization_label": "Sénat",
                "format": "senat_cr_html_detail",
            }
        )
    return items


def _parse_dosleg_html(data: bytes) -> list[dict]:
    text = data.decode("utf-8", errors="replace")
    items = []
    for href, label in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.S | re.I):
        lab = _strip_html(label)
        if not lab or len(lab) < 8:
            continue
        if not any(k in href.lower() for k in ("dossier", "leg/", "texte", "pjl", "ppl")):
            if not any(k in lab.lower() for k in ("projet", "proposition", "texte", "dossier")):
                continue
        uid = re.sub(r"\W+", "-", href)[-80:]
        items.append(
            {
                "dossier_id": uid,
                "uid": uid,
                "titre": lab[:500],
                "title": lab[:500],
                "url": href if href.startswith("http") else f"https://www.senat.fr/{href.lstrip('/')}",
                "etapes": ["index_public"],
                "format": "senat_dosleg_html",
            }
        )
        if len(items) >= 5000:
            break
    return items


def _parse_ameli_or_questions_html(data: bytes, *, kind: str) -> list[dict]:
    text = data.decode("utf-8", errors="replace")
    items = []
    for href, label in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.S | re.I):
        lab = _strip_html(label)
        if not lab or len(lab) < 5:
            continue
        if kind == "ameli":
            if not any(k in (href + lab).lower() for k in ("ameli", "amendement", "dataset", "json", "csv")):
                continue
            items.append(
                {
                    "uid": lab[:80],
                    "numero": lab[:40],
                    "objet": lab[:500],
                    "title": lab[:500],
                    "source_url": href,
                    "format": "senat_ameli_category_html",
                }
            )
        else:
            if not any(k in (href + lab).lower() for k in ("question", "basile", "dataset", "json", "csv")):
                continue
            url = href if href.startswith("http") else f"https://data.senat.fr/{href.lstrip('/')}"
            # Index open-data : discovery only, jamais une citation inventée.
            items.append(
                {
                    "item_kind": "discovery",
                    "discovery_only": True,
                    "title": lab[:500],
                    "source_url": url,
                    "locator": f"senat-q-discovery:{url}",
                    "context": "Catégorie open-data questions Sénat",
                    "format": "senat_questions_category_html",
                }
            )
        if len(items) >= 5000:
            break
    return items


def _parse_akn_xml(data: bytes) -> list[dict]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    items = []
    for text_node in root.iter():
        if _local(text_node.tag) != "text":
            continue
        fields = {_local(c.tag): (c.text or "").strip() for c in list(text_node)}
        url = fields.get("url") or ""
        title = fields.get("title") or fields.get("titre") or url
        uid = fields.get("id") or fields.get("uid") or url or title
        if not uid:
            continue
        items.append(
            {
                "dossier_id": uid[:160],
                "uid": uid[:160],
                "titre": title[:500],
                "title": title[:500],
                "url": url,
                "etapes": ["akn_depot"],
                "format": "senat_akn_xml",
            }
        )
        if len(items) >= 5000:
            break
    return items


def _parse_elysee_html(data: bytes, source_url: str = "") -> list[dict]:
    """Index Élysée = discovery ; détail avec citation réelle si page article."""
    text = data.decode("utf-8", errors="replace")
    detail = _parse_elysee_detail_html(data, source_url=source_url)
    if detail:
        return detail
    items = []
    for href, label in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.S | re.I):
        lab = _strip_html(label)
        if not lab or len(lab) < 12:
            continue
        blob = (href + " " + lab).lower()
        if not any(k in blob for k in ("discours", "allocution", "interview", "déclaration", "declaration", "conférence")):
            continue
        url = href if href.startswith("http") else f"https://www.elysee.fr/{href.lstrip('/')}"
        items.append(
            {
                "item_kind": "discovery",
                "discovery_only": True,
                "title": lab[:500],
                "source_url": url,
                "locator": f"elysee-discovery:{url}",
                "context": "Index Élysée",
                "organization_label": "Élysée",
                "format": "elysee_html_index",
            }
        )
        if len(items) >= 5000:
            break
    return items


def _parse_elysee_detail_html(data: bytes, source_url: str = "") -> list[dict]:
    text = data.decode("utf-8", errors="replace")
    # Article : titre + paragraphes corps
    title_m = re.search(r'<h1[^>]*>(.*?)</h1>', text, re.S | re.I)
    paras = re.findall(r'<p[^>]*class="[^"]*paragraph[^"]*"[^>]*>(.*?)</p>', text, re.S | re.I)
    if not paras:
        paras = re.findall(r'<p[^>]*data-speech="1"[^>]*>(.*?)</p>', text, re.S | re.I)
    if not paras:
        return []
    quote = _strip_html(" ".join(paras))
    if len(quote) < 40:
        return []
    title = _strip_html(title_m.group(1)) if title_m else "Discours"
    date_m = re.search(r'datetime="(\d{4}-\d{2}-\d{2})"', text) or re.search(
        r'(\d{4})-(\d{2})-(\d{2})', text
    )
    occurred = ""
    if date_m:
        occurred = date_m.group(1) if date_m.lastindex == 1 else f"{date_m.group(1)}-{date_m.group(2)}-{date_m.group(3)}"
    speaker_m = re.search(r'data-author="([^"]+)"', text)
    speaker = speaker_m.group(1) if speaker_m else ""
    if not speaker:
        # Pas d'auteur démontré → incomplet, pas de déclaration fabriquée
        return [
            {
                "item_kind": "metadata_only",
                "incomplete": True,
                "incomplete_reason": "speaker_missing",
                "title": title[:500],
                "source_url": source_url,
                "locator": f"elysee-incomplete:{source_url}",
                "format": "elysee_html_detail_incomplete",
            }
        ]
    return [
        {
            "item_kind": "speech",
            "stable_id": f"decl:elysee:{abs(hash(source_url + quote[:80])) & 0xFFFFFFFF:x}",
            "speaker_label": speaker[:250],
            "capacity": "président",
            "exact_quote": quote[:5000],
            "occurred_on": occurred,
            "occurred_on_state": "known" if occurred else "unknown",
            "source_url": source_url,
            "locator": f"elysee-detail:{source_url}",
            "context": title[:300],
            "source_role": "original",
            "organization_label": "Élysée",
            "format": "elysee_html_detail",
        }
    ]

def _slice_items(items: list[dict], *, offset: int = 0, limit: int | None = None) -> tuple[list[dict], int, bool]:
    total = len(items)
    if offset < 0:
        offset = 0
    chunk = items[offset:] if limit is None else items[offset : offset + limit]
    next_offset = offset + len(chunk)
    return chunk, total, next_offset >= total


def extract_items_from_bytes(
    raw: bytes,
    parser_id: str,
    *,
    media_type: str = "",
    source_url: str = "",
    limit: int | None = DEFAULT_ITEM_LIMIT,
    offset: int = 0,
    mode: str = "probe",
) -> dict[str, Any]:
    """Extrait des items. mode=production : chunk via offset/limit, pas de troncature silencieuse."""
    errors: list[dict[str, str]] = []
    all_items: list[dict] = []
    format_name = "unknown"
    media = (media_type or "").lower()
    is_zip = raw[:2] == b"PK" or "zip" in media
    if mode == "production" and (limit is None or limit == DEFAULT_ITEM_LIMIT):
        # En production, DEFAULT_ITEM_LIMIT ne doit pas tronquer : chunk explicite.
        if limit == DEFAULT_ITEM_LIMIT:
            limit = PRODUCTION_CHUNK_SIZE

    try:
        if parser_id == "an_amo":
            format_name = "amo10_json_zip" if is_zip else "amo10_json"
            payloads = _iter_zip_json_payloads(raw, limit=None) if is_zip else [("", json.loads(raw.decode("utf-8-sig")))]
            for name, payload in payloads:
                try:
                    item = _normalize_acteur_item(payload if isinstance(payload, dict) else {})
                    if item:
                        all_items.append(item)
                except Exception as exc:  # noqa: BLE001
                    errors.append({"member": name, "error": type(exc).__name__})

        elif parser_id == "an_votes":
            format_name = "scrutins_json_zip" if is_zip else "scrutin_json"
            if is_zip:
                for name, payload in _iter_zip_json_payloads(raw, limit=None):
                    try:
                        item = _normalize_scrutin_item(payload if isinstance(payload, dict) else {})
                        if item:
                            all_items.append(item)
                    except Exception as exc:  # noqa: BLE001
                        errors.append({"member": name, "error": type(exc).__name__})
            else:
                payload = json.loads(raw.decode("utf-8-sig"))
                item = _normalize_scrutin_item(payload if isinstance(payload, dict) else {})
                if item:
                    all_items.append(item)

        elif parser_id == "an_amendements":
            format_name = "amendements_json_zip" if is_zip else "amendement_json"
            payloads = _iter_zip_json_payloads(raw, limit=None) if is_zip else [("", json.loads(raw.decode("utf-8-sig")))]
            for name, payload in payloads:
                try:
                    item = _normalize_amendement_item(payload if isinstance(payload, dict) else {})
                    if item:
                        all_items.append(item)
                except Exception as exc:  # noqa: BLE001
                    errors.append({"member": name, "error": type(exc).__name__})

        elif parser_id == "an_dossiers":
            format_name = "dossiers_json_zip" if is_zip else "dossier_json"
            payloads = _iter_zip_json_payloads(raw, limit=None) if is_zip else [("", json.loads(raw.decode("utf-8-sig")))]
            for name, payload in payloads:
                try:
                    item = _normalize_dossier_item(payload if isinstance(payload, dict) else {})
                    if item:
                        all_items.append(item)
                except Exception as exc:  # noqa: BLE001
                    errors.append({"member": name, "error": type(exc).__name__})

        elif parser_id == "an_questions":
            format_name = "questions_json_zip" if is_zip else "question_json"
            payloads = _iter_zip_json_payloads(raw, limit=None) if is_zip else [("", json.loads(raw.decode("utf-8-sig")))]
            for name, payload in payloads:
                try:
                    item = _normalize_question_item(payload if isinstance(payload, dict) else {})
                    if item:
                        all_items.append(item)
                except Exception as exc:  # noqa: BLE001
                    errors.append({"member": name, "error": type(exc).__name__})

        elif parser_id == "an_cr":
            format_name = "syceron_xml_zip" if is_zip else "syceron_xml"
            if is_zip:
                for name, xml_bytes in _iter_zip_xml_payloads(raw, limit=None):
                    try:
                        all_items.extend(_parse_syceron_xml(xml_bytes, limit=None))
                    except Exception as exc:  # noqa: BLE001
                        errors.append({"member": name, "error": type(exc).__name__})
            else:
                all_items = _parse_syceron_xml(raw, limit=None)

        elif parser_id == "senat_acteurs":
            format_name = "odsen_general_json"
            payload = json.loads(raw.decode("utf-8-sig"))
            all_items = _parse_odsen(payload if isinstance(payload, dict) else {})

        elif parser_id == "senat_scrutins":
            format_name = "senat_scrutin_html"
            all_items = _parse_senat_scrutin_html(raw, source_url=source_url)

        elif parser_id == "senat_cr":
            format_name = "senat_cr_html"
            all_items = _parse_senat_cr_html(raw, source_url=source_url)

        elif parser_id == "senat_dosleg":
            format_name = "senat_dosleg_html"
            all_items = _parse_dosleg_html(raw)

        elif parser_id in {"senat_ameli"}:
            format_name = "senat_ameli_html"
            all_items = _parse_ameli_or_questions_html(raw, kind="ameli")

        elif parser_id == "senat_questions":
            format_name = "senat_questions_html"
            all_items = _parse_ameli_or_questions_html(raw, kind="questions")

        elif parser_id == "senat_akn":
            format_name = "senat_akn_xml"
            all_items = _parse_akn_xml(raw)

        elif parser_id == "elysee_speech":
            format_name = "elysee_html"
            all_items = _parse_elysee_html(raw, source_url=source_url)

        elif parser_id == "gouv_speech":
            format_name = "gouv_blocked"
            raise RuntimeError("provider_unavailable_or_waf")

        elif parser_id in {"press_rss", "podcast_rss", "youtube_meta", "bluesky_public"}:
            format_name = f"{parser_id}_json_snapshot"
            payload = json.loads(raw.decode("utf-8"))
            all_items = list((payload.get("items") if isinstance(payload, dict) else []) or [])

        else:
            if raw[:1] in (b"{", b"["):
                payload = json.loads(raw.decode("utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("items"), list):
                    format_name = "generic_items_json"
                    all_items = list(payload["items"])
                else:
                    raise ValueError(f"no_official_parser_for:{parser_id}")
            else:
                raise ValueError(f"no_official_parser_for:{parser_id}")

    except Exception as exc:  # noqa: BLE001
        return {
            "items": [],
            "parser_version": PARSER_VERSION,
            "format": format_name,
            "parse_status": "error",
            "error_code": type(exc).__name__,
            "error_message": str(exc)[:400],
            "errors": errors,
            "live_format_parser": live_format_parser_implemented(parser_id),
            "total_seen": 0,
            "processed": 0,
            "complete": False,
            "next_offset": offset,
        }

    if mode == "probe" and limit is not None and offset == 0 and len(all_items) > limit:
        chunk = all_items[:limit]
        return {
            "items": chunk,
            "parser_version": f"{PARSER_VERSION}+{ASSEMBLY_PARSER_VERSION}+{SENATE_PARSER_VERSION}",
            "format": format_name,
            "parse_status": "ok",
            "errors": errors,
            "item_count": len(chunk),
            "total_seen": len(all_items),
            "processed": len(chunk),
            "complete": False,
            "truncated_for_probe": True,
            "next_offset": len(chunk),
            "live_format_parser": True,
            "byte_size": len(raw),
            "mode": mode,
        }

    chunk, total, complete = _slice_items(all_items, offset=offset, limit=limit)
    return {
        "items": chunk,
        "parser_version": f"{PARSER_VERSION}+{ASSEMBLY_PARSER_VERSION}+{SENATE_PARSER_VERSION}",
        "format": format_name,
        "parse_status": "ok",
        "errors": errors,
        "item_count": len(chunk),
        "total_seen": total,
        "processed": offset + len(chunk),
        "remaining": max(0, total - (offset + len(chunk))),
        "complete": complete,
        "next_offset": offset + len(chunk),
        "live_format_parser": True,
        "byte_size": len(raw),
        "mode": mode,
    }


def extract_items_from_path(
    path: Path,
    parser_id: str,
    *,
    media_type: str = "",
    source_url: str = "",
    limit: int | None = DEFAULT_ITEM_LIMIT,
    offset: int = 0,
    mode: str = "probe",
) -> dict[str, Any]:
    raw = path.read_bytes()
    result = extract_items_from_bytes(
        raw,
        parser_id,
        media_type=media_type,
        source_url=source_url,
        limit=limit,
        offset=offset,
        mode=mode,
    )
    result["path"] = str(path)
    return result


def extract_items_from_document(
    document,
    parser_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
    mode: str = "production",
) -> dict[str, Any]:
    """Charge les octets d'un DocumentVersion et applique le parseur officiel."""
    from django.conf import settings

    if document is None:
        return {"items": [], "parse_status": "error", "error_code": "document_missing", "complete": False}
    path = document.original_path
    if not path:
        return {"items": [], "parse_status": "error", "error_code": "original_path_missing", "complete": False}
    full = (Path(settings.RUNTIME_DIR) / path).resolve()
    if not full.is_file() or Path(settings.RUNTIME_DIR).resolve() not in full.parents:
        return {"items": [], "parse_status": "error", "error_code": "snapshot_missing", "complete": False}
    source_url = ""
    if getattr(document, "source", None) is not None:
        source_url = document.source.document_url or document.source.discovery_url or ""
    if limit is None and mode == "production":
        limit = PRODUCTION_CHUNK_SIZE
    return extract_items_from_path(
        full,
        parser_id,
        media_type=document.media_type or "",
        source_url=source_url,
        limit=limit,
        offset=offset,
        mode=mode,
    )
